#!/usr/bin/env python3
"""
Futures Scalp Scanner
=====================

Scans ES, NQ, CL and GC on 5 minute and 1 minute bars for the seven setups in the
WSGTA Futures Rule Book (updated October 2026). It finds trades; it places no orders.

Setups (rulebook name -> function)
    The MOMO / Base Trade            momo_base       5m base, stop market N ticks beyond it
    The TREND Trade                  trend_trade     9/15 EMA within N ticks, limit at the 9 then 15 EMA
    Regular Moving Average (RMA)     rma_trade       extended N ticks / 2 ATR from 30/65/200 EMA, pivot, VWAP;
                                                     limit back at the level
    Far From Moving Average (FFMA)   ffma_trade      5m RSI > 80 / < 20 far from the EMAs, market on the 1m turn
    Double Bottom / Double Top       dbdt_trade      after an N-point move, limit at the second test
    Open Range Breakout (ORB)        orb_trades      MOMO: stop market on the ORB break; Trend: limit back at
                                                     the ORB edge after the break extends N ticks
    1-Min Range Break (Ken Sico)     range_break     5m + 1m trend, 3+ bar flat top/bottom, stop limit 1 tick out

Statuses
    TRIGGERED  the entry condition happened on the latest completed bar (act now or it is gone)
    AT LEVEL   price is touching the limit level on the latest completed bar
    ARMED      the setup is valid; the order can be staged at the listed price
    WATCH      FFMA only: the 5m extreme is in, waiting for the 1m turn

Usage
    python futures_scalp_scanner.py --demo                 # offline, synthetic bars
    python futures_scalp_scanner.py                        # one live scan (TradingView)
    python futures_scalp_scanner.py --loop                 # rescan every minute, alert on new signals
    python futures_scalp_scanner.py --symbols ES NQ --setups orb momo
    python futures_scalp_scanner.py --source csv --asof 2026-10-08T14:10Z

Rule labels
    [RB] the WSGTA rulebook says this. [IMPL] an implementation choice the rulebook leaves open; each one
    has a config key in scalp_config.yaml so it can be tuned.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

from scanner import atr, ema, windows_toast

SCALP_CODE_VERSION = "0.1.0"
UTC = timezone.utc
LOG = logging.getLogger("scalp")
LONG, SHORT = "long", "short"
TF_1M, TF_5M = "1m", "5m"
TF_DELTA = {TF_1M: pd.Timedelta(minutes=1), TF_5M: pd.Timedelta(minutes=5)}
STATUS_RANK = {"TRIGGERED": 0, "AT LEVEL": 1, "ARMED": 2, "WATCH": 3}
SETUP_KEYS = ["momo", "trend", "rma", "ffma", "dbdt", "orb", "range_break"]


# =============================================================================
# Config, instruments, signal record
# =============================================================================

@dataclass
class Instrument:
    symbol: str
    tv_symbol: str
    tv_exchange: str
    tick: float
    tick_value: float

    @property
    def decimals(self) -> int:
        s = f"{self.tick:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    def rt(self, px: float) -> float:
        """Round to the tick grid."""
        return round(round(px / self.tick) * self.tick, self.decimals)

    def ticks(self, dist: float) -> float:
        return dist / self.tick


@dataclass
class Signal:
    symbol: str
    setup: str
    side: str
    status: str
    entry_type: str
    entry: float
    stop: Optional[float]
    targets: list[float]
    risk_ticks: Optional[float]
    risk_usd: Optional[float]
    bar_time: str               # UTC open time of the bar that produced the status
    note: str = ""
    entry2: Optional[float] = None   # Trend trade: second limit at the 15 EMA

    def key(self) -> tuple:
        return (self.symbol, self.setup, self.side, self.status, self.bar_time)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def build_instruments(cfg: dict) -> dict[str, Instrument]:
    return {s: Instrument(s, d["tv_symbol"], d["tv_exchange"], float(d["tick"]), float(d["tick_value"]))
            for s, d in cfg["instruments"].items()}


def make_signal(inst: Instrument, setup: str, side: str, status: str, entry_type: str, entry: float,
                stop: Optional[float], targets_ticks: list[float], bar_time, note: str = "",
                entry2: Optional[float] = None) -> Signal:
    sgn = 1 if side == LONG else -1
    entry = inst.rt(entry)
    stop = inst.rt(stop) if stop is not None else None
    targets = [inst.rt(entry + sgn * t * inst.tick) for t in targets_ticks]
    risk = round(abs(entry - stop) / inst.tick, 1) if stop is not None else None
    usd = round(risk * inst.tick_value, 2) if risk is not None else None
    return Signal(inst.symbol, setup, side, status, entry_type, entry, stop, targets, risk, usd,
                  pd.Timestamp(bar_time).strftime("%Y-%m-%d %H:%M"), note,
                  inst.rt(entry2) if entry2 is not None else None)


# =============================================================================
# Bars
# =============================================================================

def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """UTC-indexed float OHLC, plus volume when the feed has it (VWAP needs it)."""
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if "time" in df.columns:
        df = df.set_index("time")
    df.index = pd.to_datetime(df.index, utc=True)
    cols = ["open", "high", "low", "close"] + (["volume"] if "volume" in df.columns else [])
    df = df[cols].astype(float)
    df = df[~df.index.duplicated(keep="last")]
    return df.sort_index()


def completed(df: pd.DataFrame, tf: str, asof: pd.Timestamp) -> pd.DataFrame:
    """A bar is usable only once it has closed. Drops the forming bar."""
    if df.empty:
        return df
    return df[df.index + TF_DELTA[tf] <= asof]


def resample_5m(df1: pd.DataFrame) -> pd.DataFrame:
    g = df1.resample("5min", origin="epoch", label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last()})
    if "volume" in df1.columns:
        out["volume"] = g["volume"].sum()
    return out.dropna(subset=["open", "close"])


class BarSource:
    name = "base"

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError


class TradingViewSource(BarSource):
    """TradingView via the unofficial tvDatafeed library (same access route as scanner.py).
    Without TV_USERNAME / TV_PASSWORD the data may be delayed; check before trading off it."""
    name = "tradingview"

    def __init__(self, cfg: dict):
        try:
            from tvDatafeed import Interval, TvDatafeed
        except ImportError as exc:
            raise SystemExit("tvDatafeed not installed: pip install --upgrade "
                             "git+https://github.com/rongardF/tvdatafeed.git") from exc
        self.Interval = Interval
        user, pw = os.environ.get("TV_USERNAME"), os.environ.get("TV_PASSWORD")
        self.tv = TvDatafeed(user, pw) if user and pw else TvDatafeed()
        self.cfg = cfg["bars"]

    @staticmethod
    def _to_utc(index) -> pd.DatetimeIndex:
        # tvDatafeed returns naive LOCAL datetimes built with datetime.fromtimestamp(); mktime reverses that.
        return pd.DatetimeIndex([datetime.fromtimestamp(time.mktime(d.timetuple()), tz=UTC)
                                 for d in pd.to_datetime(index).to_pydatetime()])

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        iv = {TF_1M: self.Interval.in_1_minute, TF_5M: self.Interval.in_5_minute}[tf]
        n = self.cfg["n_bars_1m"] if tf == TF_1M else self.cfg["n_bars_5m"]
        last_exc: Optional[Exception] = None
        for attempt in range(int(self.cfg.get("retries", 3))):
            try:
                df = self.tv.get_hist(symbol=inst.tv_symbol, exchange=inst.tv_exchange, interval=iv,
                                      n_bars=int(n), fut_contract=1)
                time.sleep(float(self.cfg.get("request_pause_sec", 1.0)))
                if df is None or df.empty:
                    raise RuntimeError("empty response")
                df = df.copy()
                df.index = self._to_utc(df.index)
                return normalize_bars(df)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"TradingView fetch failed for {inst.symbol} {tf}: {last_exc}")


class CsvSource(BarSource):
    """Reads <csv_dir>/<SYMBOL>_1m.csv and <SYMBOL>_5m.csv (5m is built from 1m when missing)."""
    name = "csv"

    def __init__(self, cfg: dict, base: Path):
        self.dir = (base / cfg["bars"]["csv_dir"]).resolve()

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        p = self.dir / f"{inst.symbol}_{tf}.csv"
        if p.exists():
            return normalize_bars(pd.read_csv(p))
        if tf == TF_5M:
            return resample_5m(self.get(inst, TF_1M))
        raise FileNotFoundError(str(p))


class DemoSource(BarSource):
    """Synthetic 1m random walk with trending regimes. Offline testing only, never for decisions."""
    name = "demo"
    START = {"ES": 6700.0, "NQ": 24500.0, "CL": 62.0, "GC": 4000.0}   # placeholder levels, not market data

    def __init__(self, asof: pd.Timestamp, seed: int = 11):
        self.asof, self.seed = asof, seed
        self._cache: dict[str, pd.DataFrame] = {}

    def _base_1m(self, inst: Instrument) -> pd.DataFrame:
        if inst.symbol not in self._cache:
            rng = np.random.default_rng(zlib.crc32(f"{inst.symbol}:{self.seed}".encode()))
            idx = pd.date_range(end=self.asof.floor("min") - pd.Timedelta(minutes=1), periods=60 * 24 * 4,
                                freq="min", tz=UTC)
            n, px0 = len(idx), self.START.get(inst.symbol, 100.0)
            vol = 0.00012
            regime = np.repeat(rng.normal(0, 1, n // 90 + 1), 90)[:n] * vol * 0.25
            close = px0 * np.exp(np.cumsum(regime + rng.normal(0, vol, n)))
            close = np.round(close / inst.tick) * inst.tick
            open_ = np.r_[px0, close[:-1]]
            wick = np.round(np.abs(rng.normal(0, vol * 0.5, n)) * close / inst.tick) * inst.tick
            self._cache[inst.symbol] = pd.DataFrame(
                {"open": open_, "high": np.maximum(open_, close) + wick, "low": np.minimum(open_, close) - wick,
                 "close": close, "volume": rng.integers(50, 2000, n).astype(float)}, index=idx)
        return self._cache[inst.symbol]

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        b = self._base_1m(inst)
        return b if tf == TF_1M else resample_5m(b)


# =============================================================================
# Indicators
# =============================================================================

def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder RSI, seeded with the simple average of the first n changes."""
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    d = np.diff(close)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for t in range(n, len(close)):
        if t > n:
            au = ((n - 1) * au + up[t - 1]) / n
            ad = ((n - 1) * ad + dn[t - 1]) / n
        out[t] = 100.0 if ad == 0 else 100 - 100 / (1 + au / ad)
    return out


def _hm(s: str) -> pd.Timedelta:
    h, m = s.split(":")
    return pd.Timedelta(hours=int(h), minutes=int(m))


def session_keys(index: pd.DatetimeIndex, tz: str, anchor: str) -> pd.Index:
    """Trading session date per bar: bars from the anchor time (ET) onward belong to that day's session."""
    return pd.Index((index.tz_convert(tz).tz_localize(None) - _hm(anchor)).date)


def session_vwap(df: pd.DataFrame, tz: str, anchor: str) -> np.ndarray:
    if "volume" not in df.columns or df["volume"].sum() <= 0:
        return np.full(len(df), np.nan)
    tp = (df["high"] + df["low"] + df["close"]) / 3
    key = session_keys(df.index, tz, anchor)
    pv = (tp * df["volume"]).groupby(key).cumsum()
    v = df["volume"].groupby(key).cumsum()
    return (pv / v.replace(0, np.nan)).values


def prior_session_pivot(df: pd.DataFrame, tz: str, anchor: str) -> Optional[float]:
    """Classic floor pivot (H + L + C) / 3 of the last complete session before the current one."""
    key = session_keys(df.index, tz, anchor)
    sessions = sorted(set(key))
    if len(sessions) < 2:
        return None
    prev = df[key == sessions[-2]]
    return float((prev["high"].max() + prev["low"].min() + prev["close"].iloc[-1]) / 3)


@dataclass
class Frame:
    """Completed bars of one timeframe with the indicators the setups use."""
    df: pd.DataFrame
    atr: np.ndarray
    rsi: np.ndarray
    emas: dict[int, np.ndarray] = field(default_factory=dict)
    vwap: Optional[np.ndarray] = None

    @property
    def last(self) -> int:
        return len(self.df) - 1

    def col(self, c: str) -> np.ndarray:
        return self.df[c].values


def build_frame(df: pd.DataFrame, cfg: dict, emas=(9, 15, 30, 65, 200), with_vwap: bool = False) -> Frame:
    ind = cfg["indicators"]
    c = df["close"].values
    f = Frame(df, atr(df, int(ind["atr_len"])), rsi(c, int(ind["rsi_len"])), {n: ema(c, n) for n in emas})
    if with_vwap:
        f.vwap = session_vwap(df, cfg["timezone"], cfg["session_anchor"])
    return f


def _per(d: dict, sym: str, default=None):
    return d.get(sym, default) if isinstance(d, dict) else d


# =============================================================================
# Setups
# =============================================================================

def _longest_base(h: np.ndarray, l: np.ndarray, end: int, min_n: int, max_n: int, max_height: float) -> int:
    """Longest run of bars ending at `end` whose total high-low is within max_height. 0 when none."""
    best = 0
    for n in range(min_n, max_n + 1):
        s = end - n + 1
        if s < 0 or h[s:end + 1].max() - l[s:end + 1].min() > max_height:
            break
        best = n
    return best


def momo_base(f5: Frame, inst: Instrument, p: dict, cfg: dict) -> list[Signal]:
    """[RB] MOMO / Base: 5m consolidation, stop market ES 2 / NQ 4 / CL 2 / GC 2 ticks above / below,
    stop beyond the other side of the base."""
    if inst.symbol not in p.get("enabled", [inst.symbol]) or f5.last < 20:
        return []
    h, l, t = f5.col("high"), f5.col("low"), f5.df.index
    a = f5.atr[f5.last]
    if not np.isfinite(a):
        return []
    off = p["entry_ticks"][inst.symbol] * inst.tick
    tg = p["targets_ticks"][inst.symbol]
    trend = LONG if f5.emas[9][f5.last] > f5.emas[15][f5.last] else SHORT
    max_h = float(p["max_height_atr"]) * a                                    # [IMPL]
    mn, mx = int(p["min_bars"]), int(p["max_bars"])
    out = []

    # Base that ended on the previous bar and broke out on the latest one -> TRIGGERED
    e = f5.last - 1
    n = _longest_base(h, l, e, mn, mx, max_h)
    if n:
        top, bot = h[e - n + 1:e + 1].max(), l[e - n + 1:e + 1].min()
        for side, hit, entry, stop in ((LONG, h[f5.last] >= top + off, top + off, bot - inst.tick),
                                       (SHORT, l[f5.last] <= bot - off, bot - off, top + inst.tick)):
            if hit:
                out.append(make_signal(inst, "MOMO/Base", side, "TRIGGERED", "Stop Market", entry, stop, tg,
                                       t[f5.last], f"{n}-bar base {inst.rt(bot)}-{inst.rt(top)}; "
                                       f"{'with' if side == trend else 'against'} 9/15 EMA"))
        if out:
            return out

    # Base still intact through the latest bar -> ARMED both ways
    e = f5.last
    n = _longest_base(h, l, e, mn, mx, max_h)
    if n:
        top, bot = h[e - n + 1:e + 1].max(), l[e - n + 1:e + 1].min()
        for side, entry, stop in ((LONG, top + off, bot - inst.tick), (SHORT, bot - off, top + inst.tick)):
            out.append(make_signal(inst, "MOMO/Base", side, "ARMED", "Stop Market", entry, stop, tg, t[e],
                                   f"{n}-bar base {inst.rt(bot)}-{inst.rt(top)}; "
                                   f"{'with' if side == trend else 'against'} 9/15 EMA"))
    return out


def trend_trade(f5: Frame, inst: Instrument, p: dict, cfg: dict) -> list[Signal]:
    """[RB] TREND: 9 and 15 EMA no more than N ticks apart; limit at the 9 EMA (1 contract, no stop,
    held until the trend breaks) and at the 15 EMA with the rest. 1-2 ATR risk rules apply."""
    k, sb = f5.last, int(p["slope_bars"])
    if k < 200 + sb:
        return []
    e9, e15 = f5.emas[9], f5.emas[15]
    c, h, l = f5.col("close"), f5.col("high"), f5.col("low")
    a = f5.atr[k]
    gap = inst.ticks(e9[k] - e15[k])
    max_gap = p["max_gap_ticks"][inst.symbol]
    if not np.isfinite(a) or abs(gap) > max_gap or gap == 0:
        return []
    if gap > 0 and e9[k] > e9[k - sb] and e15[k] > e15[k - sb] and c[k] > e15[k]:
        side, touched = LONG, l[k] <= e9[k]
    elif gap < 0 and e9[k] < e9[k - sb] and e15[k] < e15[k - sb] and c[k] < e15[k]:
        side, touched = SHORT, h[k] >= e9[k]
    else:
        return []
    sgn = 1 if side == LONG else -1
    stop = e15[k] - sgn * float(cfg["atr_stop_mult"]) * a                    # [IMPL] ATR stop off the 15 EMA
    return [make_signal(inst, "Trend", side, "AT LEVEL" if touched else "ARMED", "Limit", e9[k], stop,
                        p["targets_ticks"][inst.symbol], f5.df.index[k],
                        f"9/15 gap {abs(gap):.0f} ticks (max {max_gap}); 9 EMA lot has no stop and trails; "
                        f"stop shown is for the 15 EMA lot", entry2=e15[k])]


def _levels(f5: Frame, cfg: dict, names: list[str]) -> list[tuple[str, float]]:
    k, out = f5.last, []
    for nm in names:
        if nm.startswith("ema"):
            v = f5.emas[int(nm[3:])][k]
        elif nm == "vwap":
            v = f5.vwap[k] if f5.vwap is not None else np.nan
        elif nm == "pivot":
            v = prior_session_pivot(f5.df, cfg["timezone"], cfg["session_anchor"])
        else:
            raise ValueError(f"unknown RMA level {nm}")
        if v is not None and np.isfinite(v):
            out.append((nm.upper().replace("EMA", "EMA "), float(v)))
    return out


def rma_trade(f5: Frame, inst: Instrument, p: dict, cfg: dict) -> list[Signal]:
    """[RB] RMA: price extended ES 40 / NQ 160-200 / CL 25-30 / GC 20 ticks or 2 ATR from the 30, 65 or 200
    EMA, the pivot point or VWAP; limit order back at that level."""
    k = f5.last
    a = f5.atr[k]
    if k < 210 or not np.isfinite(a):
        return []
    h, l, c = f5.col("high"), f5.col("low"), f5.col("close")
    tick_thr = p["min_ext_ticks"][inst.symbol] * inst.tick
    atr_thr = float(p["ext_atr_mult"]) * a
    thr = min(tick_thr, atr_thr) if p.get("ext_mode", "either") == "either" else max(tick_thr, atr_thr)
    lb = int(p["lookback_bars"])

    # Merge levels that sit on top of each other into one row (confluence)
    lv = sorted(_levels(f5, cfg, p["levels"]), key=lambda x: x[1])
    clusters: list[list[tuple[str, float]]] = []
    for item in lv:
        if clusters and item[1] - clusters[-1][-1][1] <= float(p["confluence_atr"]) * a:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    out = []
    for cl in clusters:
        names = " + ".join(n for n, _ in cl)
        L = float(np.mean([v for _, v in cl]))
        side = LONG if c[k] > L else SHORT
        sgn = 1 if side == LONG else -1
        away = (l > L) if side == LONG else (h < L)          # bar entirely on the trade side of the level
        j = k - 1
        while j >= max(0, k - lb) and away[j]:
            j -= 1
        seg = range(j + 1, k)                                # bars of the run away from the level
        if len(seg) == 0 or j < max(0, k - lb):              # no run, or no touch inside the lookback
            continue
        ext = (h[seg].max() - L) if side == LONG else (L - l[seg].min())
        if ext < thr:
            continue
        touched = not away[k]
        stop = L - sgn * float(cfg["atr_stop_mult"]) * a
        out.append(make_signal(inst, "RMA", side, "AT LEVEL" if touched else "ARMED", "Limit", L, stop,
                               p["targets_ticks"][inst.symbol], f5.df.index[k],
                               f"limit at {names}; extended {inst.ticks(ext):.0f} ticks "
                               f"(need {inst.ticks(thr):.0f})"))
    return out


def ffma_trade(f5: Frame, f1: Frame, inst: Instrument, p: dict, cfg: dict) -> list[Signal]:
    """[RB] FFMA: on 5m, price far from the EMAs with RSI > 80 (short) or < 20 (long); switch to 1m and
    enter at market as soon as price starts back toward the EMAs. Stop beyond the RSI candle."""
    k = f5.last
    if k < 30 or f1.last < 5:
        return []
    h5, l5, c5 = f5.col("high"), f5.col("low"), f5.col("close")
    ref = f5.emas[int(p["ref_ema"])]
    thr = p["min_dist_ticks"][inst.symbol] * inst.tick
    sig = None
    for s in range(k, max(k - int(p["signal_bars"]), 0), -1):     # most recent qualifying 5m bar
        if f5.rsi[s] >= p["rsi_short"] and c5[s] - ref[s] >= thr:
            sig = (s, SHORT)
            break
        if f5.rsi[s] <= p["rsi_long"] and ref[s] - c5[s] >= thr:
            sig = (s, LONG)
            break
    if sig is None:
        return []
    s, side = sig
    sgn = 1 if side == LONG else -1
    stop = (l5[s] - inst.tick) if side == LONG else (h5[s] + inst.tick)
    sig_end = f5.df.index[s] + TF_DELTA[TF_5M]
    t1 = f1.df.index
    o1, c1 = f1.col("open"), f1.col("close")
    after = np.where(t1 >= sig_end)[0]
    note = f"5m RSI {f5.rsi[s]:.0f}, {inst.ticks(abs(c5[s] - ref[s])):.0f} ticks from {p['ref_ema']} EMA"
    # Invalid once price has already reached the EMA
    if side == SHORT and (l5[s + 1:k + 1] <= ref[s + 1:k + 1]).any():
        return []
    if side == LONG and (h5[s + 1:k + 1] >= ref[s + 1:k + 1]).any():
        return []
    for i in after:
        if i == 0:
            continue
        turned = (c1[i] < c1[i - 1] and c1[i] < o1[i]) if side == SHORT else (c1[i] > c1[i - 1] and c1[i] > o1[i])
        if turned:                                                  # [IMPL] first 1m bar closing back toward
            if i < f1.last - int(p["fresh_1m_bars"]) + 1:
                return []                                           # turn already passed: missed
            return [make_signal(inst, "FFMA", side, "TRIGGERED", "Market", c1[i], stop,
                                p["targets_ticks"][inst.symbol], t1[i], note + "; 1m turned")]
    return [make_signal(inst, "FFMA", side, "WATCH", "Market", c5[k], stop, p["targets_ticks"][inst.symbol],
                        f5.df.index[s], note + "; wait for the 1m turn toward the EMAs")]


def find_pivots(h: np.ndarray, l: np.ndarray, w: int) -> tuple[list[int], list[int]]:
    """Swing highs / lows confirmed by w bars on each side."""
    hi, lo = [], []
    for k in range(w, len(h) - w):
        if h[k] == h[k - w:k + w + 1].max() and h[k] > h[k - w:k].max():
            hi.append(k)
        if l[k] == l[k - w:k + w + 1].min() and l[k] < l[k - w:k].min():
            lo.append(k)
    return hi, lo


def dbdt_trade(f5: Frame, inst: Instrument, p: dict, cfg: dict) -> list[Signal]:
    """[RB] DB / DT: after a move of ES 10+ / NQ 30+ points, CL 20+ / GC 30+ ticks from the prior high/low,
    buy the second bottom / sell the second top with a limit. ATR risk managed exits or less."""
    k = f5.last
    a = f5.atr[k]
    if k < 40 or not np.isfinite(a):
        return []
    h, l, c = f5.col("high"), f5.col("low"), f5.col("close")
    w, lb, plb = int(p["pivot_width"]), int(p["lookback_bars"]), int(p["prior_lookback_bars"])
    move = p["min_move_ticks"][inst.symbol] * inst.tick
    tol = float(p["tolerance_atr"]) * a
    bounce = float(p["min_bounce_atr"]) * a
    his, los = find_pivots(h, l, w)
    out = []
    for side, piv in ((LONG, los), (SHORT, his)):
        cand = [q for q in piv if q >= k - lb]
        if not cand:
            continue
        k1 = cand[-1]                                        # latest confirmed first bottom / top
        lvl = l[k1] if side == LONG else h[k1]
        pre = slice(max(0, k1 - plb), k1)
        prior_move = (h[pre].max() - lvl) if side == LONG else (lvl - l[pre].min())
        if prior_move < move:
            continue
        mid = slice(k1 + 1, k)                               # bars between the first test and now
        if k1 + 1 >= k:
            continue
        bounced = (h[mid].max() - lvl) if side == LONG else (lvl - l[mid].min())
        broken = (c[k1 + 1:k + 1] < lvl - tol).any() if side == LONG else (c[k1 + 1:k + 1] > lvl + tol).any()
        if bounced < bounce or broken:
            continue
        at = (l[k] <= lvl + tol) if side == LONG else (h[k] >= lvl - tol)
        sgn = 1 if side == LONG else -1
        stop = lvl - sgn * float(cfg["atr_stop_mult"]) * a
        name = "Double Bottom" if side == LONG else "Double Top"
        out.append(make_signal(inst, "DB/DT", side, "AT LEVEL" if at else "ARMED", "Limit", lvl, stop,
                               p["targets_ticks"][inst.symbol], f5.df.index[k],
                               f"{name}: first test {f5.df.index[k1]:%H:%M}Z after a {inst.ticks(prior_move):.0f}-tick "
                               f"move; bounce {inst.ticks(bounced):.0f} ticks"))
    return out


def orb_trades(f5: Frame, inst: Instrument, p: dict, cfg: dict, asof: pd.Timestamp) -> list[Signal]:
    """[RB] ORB, RTH only. 5m ORB until 09:45 ET, then the 15m ORB (09:30-09:45) until 16:00.
    MOMO: stop market on the break of the ORB high / low. Trend: once the break has run ES 40 ticks
    (or ~2 ATR, whichever is larger) / NQ 160 / CL 25 / GC 20 ticks, limit back at the ORB edge."""
    tz = cfg["timezone"]
    now = asof.tz_convert(tz)
    day = now.normalize().tz_localize(None)
    rth_open = (day + _hm(cfg["rth"]["open"])).tz_localize(tz)
    rth_close = (day + _hm(cfg["rth"]["close"])).tz_localize(tz)
    switch = (day + _hm(p["switch_to_15m"])).tz_localize(tz)
    if not rth_open < now <= rth_close:
        return []
    orb_end = switch if now >= switch else rth_open + pd.Timedelta(minutes=5)
    if now < orb_end:
        return []
    df = f5.df
    t = df.index.tz_convert(tz)
    rng = df[(t >= rth_open) & (t < orb_end)]
    if rng.empty or rng.index[-1] + TF_DELTA[TF_5M] < orb_end.tz_convert(UTC):
        return []
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    mid, height = (hi + lo) / 2, hi - lo
    label = "15m" if orb_end == switch else "5m"
    post = df[(t >= orb_end) & (t <= now)]
    a = f5.atr[f5.last]
    sym, out = inst.symbol, []
    mtg = p["momo_targets_ticks"][sym]
    orb_txt = f"{label} ORB {inst.rt(lo)}-{inst.rt(hi)}"

    brk = None
    for i, (ts, r) in enumerate(post.iterrows()):
        if r["high"] > hi:
            brk = (i, LONG)
            break
        if r["low"] < lo:
            brk = (i, SHORT)
            break
    if brk is None:
        for side, entry, opp in ((LONG, hi + inst.tick, lo), (SHORT, lo - inst.tick, hi)):
            out.append(make_signal(inst, "ORB MOMO", side, "ARMED", "Stop Market", entry, mid, mtg, df.index[-1],
                                   f"{orb_txt}; stop options: entry candle, mid {inst.rt(mid)}, "
                                   f"opposite {inst.rt(opp)}"))
        return out

    i, side = brk
    sgn = 1 if side == LONG else -1
    edge = hi if side == LONG else lo
    bar = post.iloc[i]
    stops = {"entry_candle": bar["low"] - inst.tick if side == LONG else bar["high"] + inst.tick,
             "orb_mid": mid, "opposite": lo if side == LONG else hi}
    if i == len(post) - 1:
        out.append(make_signal(inst, "ORB MOMO", side, "TRIGGERED", "Stop Market", edge + sgn * inst.tick,
                               stops[p["momo_stop"]], mtg, post.index[i],
                               f"{orb_txt}; alt stops: mid {inst.rt(mid)}, opposite {inst.rt(stops['opposite'])}"))
        return out

    # ORB Trend: the break must extend far enough, then price comes back to the ORB edge
    need = p["trend_ext_ticks"][sym] * inst.tick
    if sym in p.get("trend_ext_atr_mult", {}) and np.isfinite(a):
        need = max(need, float(p["trend_ext_atr_mult"][sym]) * a)
    after = post.iloc[i:]
    far = (after["high"] - edge) if side == LONG else (edge - after["low"])
    reached = np.where(far.values >= need)[0]
    if len(reached) == 0:
        return out
    j0 = reached[0]
    rest = after.iloc[j0 + 1:]
    touch = (rest["low"] <= edge) if side == LONG else (rest["high"] >= edge)
    if touch.iloc[:-1].any() if len(rest) > 1 else False:
        return out                                            # already filled earlier
    at = bool(touch.iloc[-1]) if len(rest) else False
    st_ticks = min(p["trend_stop_ticks"][sym], inst.ticks(0.5 * height))
    stop = edge - sgn * st_ticks * inst.tick
    out.append(make_signal(inst, "ORB Trend", side, "AT LEVEL" if at else "ARMED", "Limit", edge, stop,
                           p["trend_targets_ticks"][sym], df.index[-1],
                           f"{orb_txt}; break ran {inst.ticks(far.max()):.0f} ticks (need {inst.ticks(need):.0f}); "
                           f"stop {st_ticks:.0f} ticks (rule ticks or 50% ORB, smaller)"))
    return out


def range_break(f5: Frame, f1: Frame, inst: Instrument, p: dict, cfg: dict) -> list[Signal]:
    """[RB] 1-Min Range Break (Ken Sico). 5m: price trending vs the 9/15 EMA (65/200 add confluence), RSI > 50
    and rising (long). 1m: above the 9/15 EMA, RSI > 50. Setup: 3+ 1m bars with a flat top (1-2 tick variation),
    depth within the limit. Buy stop limit 1 tick above, stop 1 tick below the range (reverse for shorts)."""
    k5, k1 = f5.last, f1.last
    sb = int(p["rsi_slope_bars"])
    if k5 < 20 or k1 < 20:
        return []
    c5, c1 = f5.col("close")[k5], f1.col("close")
    e = f5.emas
    r5, r5p = f5.rsi[k5], f5.rsi[k5 - sb]
    e9_1, e15_1 = f1.emas[9], f1.emas[15]
    h1, l1 = f1.col("high"), f1.col("low")
    flat = int(p["flat_ticks"]) * inst.tick + 1e-9
    depth = float(p["max_depth"][inst.symbol])
    a1 = f1.atr[k1]
    out = []
    for side in (LONG, SHORT):
        sgn = 1 if side == LONG else -1
        macro = (sgn * (c5 - e[9][k5]) > 0 and sgn * (c5 - e[15][k5]) > 0 and sgn * (e[9][k5] - e[15][k5]) > 0
                 and sgn * (r5 - 50) > 0 and sgn * (r5 - r5p) > 0)
        if not macro:
            continue
        conf = [str(n) for n in (65, 200) if np.isfinite(e[n][k5]) and sgn * (c5 - e[n][k5]) > 0]

        def micro(i: int) -> bool:
            return sgn * (c1[i] - e9_1[i]) > 0 and sgn * (c1[i] - e15_1[i]) > 0 and sgn * (f1.rsi[i] - 50) > 0

        def coil(end: int) -> int:
            lvl = h1 if side == LONG else l1
            best = 0
            for n in range(int(p["min_bars"]), int(p["max_bars"]) + 1):
                s = end - n + 1
                if s < 0:
                    break
                w = slice(s, end + 1)
                if lvl[w].max() - lvl[w].min() > flat or h1[w].max() - l1[w].min() > depth:
                    break
                best = n
            return best

        for end, status in ((k1 - 1, "TRIGGERED"), (k1, "ARMED")):
            n = coil(end)
            if not n:
                continue
            w = slice(end - n + 1, end + 1)
            top, bot = h1[w].max(), l1[w].min()
            trigger = top + inst.tick if side == LONG else bot - inst.tick
            stop = bot - inst.tick if side == LONG else top + inst.tick
            if status == "TRIGGERED":
                if not (h1[k1] >= trigger if side == LONG else l1[k1] <= trigger) or not micro(end):
                    continue
            elif not micro(k1):
                continue
            risk_ticks = inst.ticks(abs(trigger - stop))
            near = abs((bot if side == LONG else top) - e9_1[end])
            near_txt = "near EMAs" if np.isfinite(a1) and near <= float(p["near_ema_atr"]) * a1 else "away from EMAs"
            out.append(make_signal(inst, "1m Range Break", side, status,
                                   "Buy Stop Limit" if side == LONG else "Sell Stop Limit", trigger, stop,
                                   [r * risk_ticks for r in p["targets_r"]], f1.df.index[k1 if status == "TRIGGERED" else end],
                                   f"{n}-bar 1m flat {'top' if side == LONG else 'bottom'}, depth "
                                   f"{inst.ticks(top - bot):.0f} ticks; {near_txt} "
                                   f"({inst.ticks(near):.0f} ticks to 1m 9 EMA); 5m confluence "
                                   f"{'+'.join(['9', '15'] + conf)} EMA; targets are 1R/2R [IMPL]"))
            break
    return out


# =============================================================================
# Scan
# =============================================================================

def scan_instrument(src: BarSource, inst: Instrument, cfg: dict, asof: pd.Timestamp,
                    setups: list[str]) -> tuple[list[Signal], list[str]]:
    errs: list[str] = []
    try:
        d5 = completed(src.get(inst, TF_5M), TF_5M, asof)
        d1 = completed(src.get(inst, TF_1M), TF_1M, asof) if {"ffma", "range_break"} & set(setups) else None
    except Exception as exc:  # noqa: BLE001
        return [], [f"{inst.symbol}: {exc}"]
    if len(d5) < 220:
        errs.append(f"{inst.symbol}: only {len(d5)} completed 5m bars (200 EMA needs 220+)")
        if len(d5) < 30:
            return [], errs
    stale = asof - (d5.index[-1] + TF_DELTA[TF_5M])
    if stale > pd.Timedelta(minutes=15):
        errs.append(f"{inst.symbol}: last 5m bar closed {stale} ago (market closed or delayed feed)")
    f5 = build_frame(d5, cfg, with_vwap=True)
    f1 = build_frame(d1, cfg, emas=(9, 15)) if d1 is not None and len(d1) else None
    S = cfg["setups"]
    out: list[Signal] = []
    runners = {
        "momo": lambda: momo_base(f5, inst, S["momo"], cfg),
        "trend": lambda: trend_trade(f5, inst, S["trend"], cfg),
        "rma": lambda: rma_trade(f5, inst, S["rma"], cfg),
        "ffma": lambda: ffma_trade(f5, f1, inst, S["ffma"], cfg) if f1 is not None else [],
        "dbdt": lambda: dbdt_trade(f5, inst, S["dbdt"], cfg),
        "orb": lambda: orb_trades(f5, inst, S["orb"], cfg, asof),
        "range_break": lambda: range_break(f5, f1, inst, S["range_break"], cfg) if f1 is not None else [],
    }
    for name in setups:
        try:
            out.extend(runners[name]())
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{inst.symbol} {name}: {exc}")
            LOG.debug("setup failed", exc_info=True)
    return out, errs


def rank(signals: list[Signal]) -> list[Signal]:
    return sorted(signals, key=lambda s: (STATUS_RANK.get(s.status, 9), s.symbol, s.setup, s.side))


def fmt_px(v: Optional[float]) -> str:
    return "" if v is None else f"{v:g}"


def render_table(signals: list[Signal], asof: pd.Timestamp, tz: str) -> str:
    head = f"Futures scalp scan  {asof.tz_convert(tz):%Y-%m-%d %H:%M %Z}  ({len(signals)} signals)"
    if not signals:
        return head + "\n  no setups"
    cols = ["Status", "Sym", "Setup", "Side", "Entry type", "Entry", "Stop", "Targets", "Risk", "Bar (UTC)"]
    rows = [[s.status, s.symbol, s.setup, s.side, s.entry_type,
             fmt_px(s.entry) + (f" / {fmt_px(s.entry2)}" if s.entry2 is not None else ""), fmt_px(s.stop),
             ", ".join(fmt_px(t) for t in s.targets),
             "" if s.risk_ticks is None else f"{s.risk_ticks:g}t ${s.risk_usd:,.0f}", s.bar_time[11:]] for s in signals]
    wid = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
    line = lambda r: "  ".join(v.ljust(wid[i]) for i, v in enumerate(r))  # noqa: E731
    out = [head, line(cols), line(["-" * x for x in wid])]
    for r, s in zip(rows, signals):
        out.append(line(r))
        out.append(" " * 4 + s.note)
    return "\n".join(out)


def write_outputs(signals: list[Signal], errs: list[str], asof: pd.Timestamp, out_dir: Path, meta: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = asof.strftime("%Y%m%dT%H%MZ")
    p = out_dir / f"scalp_{stamp}.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["status", "symbol", "setup", "side", "entry_type", "entry", "entry2", "stop", "targets",
                     "risk_ticks", "risk_usd", "bar_time_utc", "note"])
        for s in signals:
            wr.writerow([s.status, s.symbol, s.setup, s.side, s.entry_type, s.entry, s.entry2, s.stop,
                         " ".join(map(str, s.targets)), s.risk_ticks, s.risk_usd, s.bar_time, s.note])
    (out_dir / "latest.json").write_text(json.dumps(
        {**meta, "asof": asof.isoformat(), "signals": [asdict(s) for s in signals], "errors": errs}, indent=1),
        encoding="utf-8")
    return p


def run_scan(cfg: dict, src: BarSource, insts: list[Instrument], asof: pd.Timestamp,
             setups: list[str]) -> tuple[list[Signal], list[str]]:
    sigs, errs = [], []
    for inst in insts:
        s, e = scan_instrument(src, inst, cfg, asof, setups)
        sigs += s
        errs += e
    return rank(sigs), errs


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scan ES/NQ/CL/GC for the WSGTA futures scalp setups.")
    ap.add_argument("--config", default="scalp_config.yaml")
    ap.add_argument("--source", default=None, choices=["tradingview", "csv"], help="override bars.source")
    ap.add_argument("--asof", default=None, help="scan time, e.g. 2026-10-08T14:10Z (default: now)")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--loop", action="store_true", help="rescan every loop.interval_sec; alert on new signals")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--setups", nargs="*", default=None, choices=SETUP_KEYS)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    base = Path(__file__).resolve().parent
    cfg_path = Path(args.config) if Path(args.config).is_absolute() else base / args.config
    cfg = load_config(cfg_path)
    all_inst = build_instruments(cfg)
    syms = [s.upper() for s in (args.symbols or all_inst)]
    unknown = [s for s in syms if s not in all_inst]
    if unknown:
        ap.error(f"unknown symbol(s): {', '.join(unknown)}")
    insts = [all_inst[s] for s in syms]
    setups = args.setups or SETUP_KEYS
    asof_fixed = pd.Timestamp(args.asof).tz_localize(UTC) if args.asof and pd.Timestamp(args.asof).tzinfo is None \
        else (pd.Timestamp(args.asof).tz_convert(UTC) if args.asof else None)
    out_dir = Path(args.out_dir) if args.out_dir else base / cfg["output"]["dir"]

    if args.demo:
        asof0 = asof_fixed or pd.Timestamp("2026-10-08T15:10Z")
        src: BarSource = DemoSource(asof0)
        if args.loop:
            ap.error("--loop needs live data; drop --demo")
    else:
        kind = args.source or cfg["bars"]["source"]
        src = CsvSource(cfg, base) if kind == "csv" else TradingViewSource(cfg)
    if args.demo:
        LOG.warning("DEMO DATA: synthetic bars, not market prices. Do not trade from this output.")

    seen: set = set()
    while True:
        asof = asof_fixed or (pd.Timestamp("2026-10-08T15:10Z") if args.demo else pd.Timestamp.now(tz=UTC))
        sigs, errs = run_scan(cfg, src, insts, asof, setups)
        for e in errs:
            LOG.warning(e)
        print(render_table(sigs, asof, cfg["timezone"]))
        p = write_outputs(sigs, errs, asof, out_dir, {"code_version": SCALP_CODE_VERSION, "config": cfg["version"],
                                                       "source": src.name})
        LOG.info("wrote %s", p)
        if not args.loop:
            return 0
        fresh = [s for s in sigs if s.status in ("TRIGGERED", "AT LEVEL") and s.key() not in seen]
        seen |= {s.key() for s in sigs}
        if fresh and cfg.get("alerts", {}).get("windows_toast", True):
            lines = [f"{s.symbol} {s.setup} {s.side} {s.status} @ {fmt_px(s.entry)}" for s in fresh]
            windows_toast(f"Scalp: {len(fresh)} new signal{'s' if len(fresh) > 1 else ''}",
                          "\n".join(lines[:4]) + (f"\n+{len(lines) - 4} more" if len(lines) > 4 else ""))
        time.sleep(max(5.0, float(cfg["loop"]["interval_sec"]) - (pd.Timestamp.now(tz=UTC) - asof).total_seconds()))


if __name__ == "__main__":
    sys.exit(main())
