#!/usr/bin/env python3
"""
Daily Instrument Scanner
========================

Implements "Daily Instrument Scanner Addendum v0.1" (October 2, 2026), which extends
"Trading Algorithm Specification v1.0" (September 30, 2026).

What it does
    Twice a day it scores 34 instruments (28 FX pairs, 3 CFDs, 3 futures) in both directions
    against the v1.0 confluences C1 to C6, adds a COT overlay (+/-2) and a headline sentiment
    overlay (+/-2), and ranks the top 5 setups (qualified first, then developing).

What it does NOT do
    It places no orders, sizes no positions and changes no v1.0 trade rule. Overlays rank,
    they never qualify (Addendum 6, test default).

Usage
    python scanner.py --demo                      # offline run on synthetic data
    python scanner.py --run evening               # live run (TradingView + CFTC + Forex Factory)
    python scanner.py --run preny
    python scanner.py --daemon                    # stay running, fire at 00:05 and 12:05 UTC
    python scanner.py --source csv --asof 2026-10-02T12:05Z

Rule labels
    Comments tagged [v1.0 ...] or [Add ...] cite the governing section. Comments tagged
    [IMPL] mark implementation choices the documents leave open; they are test defaults.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

SCANNER_CODE_VERSION = "0.1.0"
UTC = timezone.utc
ET_TZ = ZoneInfo("America/New_York")
LOG = logging.getLogger("scanner")

TF_1H, TF_2H, TF_4H, TF_D = "1H", "2H", "4H", "D"
TF_DELTA = {TF_1H: pd.Timedelta(hours=1), TF_2H: pd.Timedelta(hours=2),
            TF_4H: pd.Timedelta(hours=4), TF_D: pd.Timedelta(days=1)}
LONG, SHORT = "long", "short"


# =============================================================================
# Config and instruments
# =============================================================================

@dataclass
class Instrument:
    symbol: str
    group: str                    # FX major | FX cross | CFD | Futures
    asset: str                    # fx | gold | oil | spx   (sentiment column)
    tick: float
    grid_major: float
    grid_mid: float
    tv_symbol: str
    tv_exchange: str
    fut_contract: Optional[int] = None
    base: Optional[str] = None    # FX base currency
    quote: Optional[str] = None   # FX quote currency
    underlying: Optional[str] = None
    cot: Optional[str] = None     # COT market key for non FX
    short_test: bool = False      # gold and S&P: carry v1.0 short experiment flag
    roll: Optional[str] = None

    @property
    def calendar_currencies(self) -> list[str]:
        # [Add 6.4] USD events for gold, S&P and oil
        if self.asset == "fx":
            return [self.base, self.quote]
        return ["USD"]

    @property
    def sentiment_legs(self) -> list[tuple[str, int]]:
        # [Add 7] FX is a contest between economies: base strength minus quote strength.
        # [IMPL] XAUUSD and GC are treated like a pair against USD; oil and S&P stand alone.
        if self.asset == "fx":
            return [(self.base, +1), (self.quote, -1)]
        if self.asset == "gold":
            return [("GOLD", +1), ("USD", -1)]
        if self.asset == "oil":
            return [("OIL", +1)]
        return [("SPX", +1)]


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    for key in ("config_version", "addendum_version", "features", "grids", "fx", "cot", "sentiment"):
        if key not in cfg:
            raise ValueError(f"config missing '{key}'")
    return cfg


def build_universe(cfg: dict) -> list[Instrument]:
    out: list[Instrument] = []
    grids = cfg["grids"]
    for grp, key in (("FX major", "majors"), ("FX cross", "crosses")):
        for sym in cfg["fx"][key]:
            base, quote = sym[:3], sym[3:]
            jpy = "JPY" in (base, quote)
            g = grids["jpy" if jpy else "fx"]
            out.append(Instrument(symbol=sym, group=grp, asset="fx", tick=0.001 if jpy else 0.00001,
                                  grid_major=float(g["major"]), grid_mid=float(g["mid"]),
                                  tv_symbol=sym, tv_exchange=cfg["fx"].get("tv_exchange", "OANDA"),
                                  base=base, quote=quote))
    for it in cfg.get("other_instruments", []):
        g = grids[it["grid"]]
        out.append(Instrument(symbol=it["symbol"], group=it["group"], asset=it["asset"], tick=float(it["tick"]),
                              grid_major=float(g["major"]), grid_mid=float(g["mid"]),
                              tv_symbol=it["tv_symbol"], tv_exchange=it["tv_exchange"],
                              fut_contract=it.get("fut_contract"), underlying=it.get("underlying"),
                              cot=it.get("cot"), short_test=bool(it.get("short_test", False)),
                              roll=it.get("roll"), base=it.get("base"), quote=it.get("quote")))
    syms = [i.symbol for i in out]
    if len(set(syms)) != len(syms):
        raise ValueError("duplicate instrument symbols in config")
    return out


# =============================================================================
# Bar utilities  [v1.0 Section 2]
# =============================================================================

def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Return a frame indexed by tz-aware UTC bar open time with float open/high/low/close."""
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if "time" in df.columns:
        df = df.set_index("time")
    idx = pd.to_datetime(df.index, utc=True)
    df.index = idx
    df = df[["open", "high", "low", "close"]].astype(float)
    return df.sort_index()


def resample_utc(df1h: pd.DataFrame, tf: str) -> pd.DataFrame:
    """[v1.0 2] Build 2H or 4H bars on a 00:00 UTC grid from 1H bars. No synthetic weekend bars."""
    rule = {"2H": "2h", "4H": "4h"}[tf]
    g = df1h.resample(rule, origin="epoch", label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                        "low": g["low"].min(), "close": g["close"].last()})
    return out.dropna(subset=["open", "close"])


def completed(df: pd.DataFrame, tf: str, asof: pd.Timestamp) -> pd.DataFrame:
    """[v1.0 2] A bar becomes available only at its closing timestamp."""
    if df.empty:
        return df
    end = df.index + TF_DELTA[tf]
    return df[end <= asof]


def check_bars(df: pd.DataFrame, min_bars: int) -> list[str]:
    """[v1.0 2] 250 completed bars, no duplicate timestamps, no impossible OHLC values."""
    errs = []
    if len(df) < min_bars:
        errs.append(f"only {len(df)} completed bars (need {min_bars})")
    if df.index.duplicated().any():
        errs.append("duplicate timestamps")
    bad = ((df["high"] < df[["open", "close"]].max(axis=1)) | (df["low"] > df[["open", "close"]].min(axis=1))
           | (df["low"] > df["high"]) | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
           | df[["open", "high", "low", "close"]].isna().any(axis=1))
    if bad.any():
        errs.append(f"{int(bad.sum())} impossible OHLC rows")
    return errs


# =============================================================================
# Bar sources
# =============================================================================

class BarSource:
    name = "base"

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def provider(self, inst: Instrument) -> str:
        return f"{inst.tv_exchange}:{inst.tv_symbol}"


class TradingViewSource(BarSource):
    """[Add 4] TradingView via the unofficial tvDatafeed library. Owner accepts the access risk."""
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
        # tvDatafeed builds naive LOCAL datetimes with datetime.fromtimestamp(); mktime reverses that
        # exactly, including daylight saving, except in the repeated hour at the autumn change.
        return pd.DatetimeIndex([datetime.fromtimestamp(time.mktime(d.timetuple()), tz=UTC)
                                 for d in pd.to_datetime(index).to_pydatetime()])

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        iv = {TF_1H: self.Interval.in_1_hour, TF_2H: self.Interval.in_2_hour,
              TF_4H: self.Interval.in_4_hour, TF_D: self.Interval.in_daily}[tf]
        n = {TF_1H: self.cfg["n_bars_1h"], TF_D: self.cfg["n_bars_daily"]}.get(tf, self.cfg["n_bars_native"])
        last_exc: Optional[Exception] = None
        for attempt in range(int(self.cfg.get("retries", 3))):
            try:
                df = self.tv.get_hist(symbol=inst.tv_symbol, exchange=inst.tv_exchange, interval=iv,
                                      n_bars=int(n), fut_contract=inst.fut_contract)
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
    """Reads <csv_dir>/<SYMBOL>_<TF>.csv with columns time (UTC), open, high, low, close."""
    name = "csv"

    def __init__(self, cfg: dict, base: Path):
        self.dir = (base / cfg["bars"]["csv_dir"]).resolve()

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        p = self.dir / f"{inst.symbol}_{tf}.csv"
        if not p.exists():
            raise FileNotFoundError(str(p))
        return normalize_bars(pd.read_csv(p))

    def provider(self, inst: Instrument) -> str:
        return f"csv:{inst.symbol}"


class DemoSource(BarSource):
    """Synthetic random walk with trending regimes. For offline testing only, never for decisions."""
    name = "demo"

    def __init__(self, asof: pd.Timestamp, seed: int = 7):
        self.asof = asof
        self.seed = seed
        self._cache: dict[str, pd.DataFrame] = {}

    def _base_1h(self, inst: Instrument) -> pd.DataFrame:
        if inst.symbol in self._cache:
            return self._cache[inst.symbol]
        rng = np.random.default_rng(zlib.crc32(f"{inst.symbol}:{self.seed}".encode()))
        start_px = {"fx": 1.0 + rng.random(), "gold": 4000.0, "spx": 6500.0, "oil": 70.0}[inst.asset]
        if inst.asset == "fx" and "JPY" in (inst.base, inst.quote):
            start_px = 100 + 80 * rng.random()
        end = self.asof.floor("h")
        idx = pd.date_range(end=end, periods=24 * 420, freq="h", tz=UTC)
        idx = idx[idx.dayofweek < 5]  # no weekend bars
        n = len(idx)
        vol = 0.0012 if inst.asset == "fx" else 0.0025
        regime = np.repeat(rng.normal(0, 1, n // 300 + 1), 300)[:n] * vol * 0.12
        rets = regime + rng.normal(0, vol, n) * 0.6
        close = start_px * np.exp(np.cumsum(rets))
        open_ = np.r_[start_px, close[:-1]]
        spread = np.abs(rng.normal(0, vol * 0.6, n)) * close
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread
        df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)
        self._cache[inst.symbol] = df
        return df

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        h = self._base_1h(inst)
        if tf == TF_1H:
            return h
        if tf == TF_D:
            g = h.resample("1D")
            return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                                 "close": g["close"].last()}).dropna()
        return resample_utc(h, tf)

    def provider(self, inst: Instrument) -> str:
        return f"demo:{inst.symbol}"


def load_instrument_bars(src: BarSource, inst: Instrument, cfg: dict, asof: pd.Timestamp,
                         daily_cutoff: pd.Timestamp) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Return completed Daily, 4H and 2H bars plus data check errors."""
    mode = cfg["bars"].get("intraday_mode", "resample_1h")
    bars: dict[str, pd.DataFrame] = {}
    errs: list[str] = []
    if mode == "resample_1h":
        h1 = completed(src.get(inst, TF_1H), TF_1H, asof)
        for tf in (TF_2H, TF_4H):
            r = resample_utc(h1, tf)
            r = r[(r.index + TF_DELTA[tf]) <= asof]
            bars[tf] = r
    else:
        for tf in (TF_2H, TF_4H):
            bars[tf] = completed(src.get(inst, tf), tf, asof)
    # [Add 4] pre NY: Daily bias stays frozen from the evening run -> cut Daily at the evening run time
    bars[TF_D] = completed(src.get(inst, TF_D), TF_D, daily_cutoff)
    mb = int(cfg["bars"]["min_completed_bars"])
    for tf in (TF_D, TF_4H, TF_2H):
        e = check_bars(bars[tf], mb)
        errs += [f"{tf}: {x}" for x in e]
    return bars, errs


# =============================================================================
# Indicators  [v1.0 Section 2]
# =============================================================================

def ema(values: np.ndarray, n: int) -> np.ndarray:
    """alpha = 2/(n+1); seeded with the arithmetic mean of the first n closes."""
    out = np.full(len(values), np.nan)
    if len(values) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = float(np.mean(values[:n]))
    for t in range(n, len(values)):
        out[t] = a * values[t] + (1 - a) * out[t - 1]
    return out


def atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    """Wilder ATR, seeded with the mean of the first n valid true ranges (TR needs a previous close)."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    out = np.full(len(c), np.nan)
    if len(c) < n + 1:
        return out
    pc = c[:-1]
    tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    out[n] = float(np.mean(tr[:n]))
    for t in range(n + 1, len(c)):
        out[t] = ((n - 1) * out[t - 1] + tr[t - 1]) / n
    return out


@dataclass
class Pivot:
    k: int          # bar index of the pivot
    kind: str       # "H" or "L"
    price: float
    confirm: int    # bar index at whose close the pivot becomes usable (k + width)


def find_pivots(df: pd.DataFrame, width: int = 2) -> list[Pivot]:
    """[v1.0 2] Strict swing highs/lows vs the two bars each side; ties fail; both-type bars are discarded."""
    h, l = df["high"].values, df["low"].values
    out: list[Pivot] = []
    for k in range(width, len(h) - width):
        nb = [k - j for j in range(1, width + 1)] + [k + j for j in range(1, width + 1)]
        is_h = all(h[k] > h[j] for j in nb)
        is_l = all(l[k] < l[j] for j in nb)
        if is_h and is_l:
            continue
        if is_h:
            out.append(Pivot(k, "H", float(h[k]), k + width))
        elif is_l:
            out.append(Pivot(k, "L", float(l[k]), k + width))
    return out


def add_alternating(seq: list[Pivot], p: Pivot) -> None:
    """[v1.0 2] Same type in a row keeps the more extreme one; equal prices keep the earlier."""
    if seq and seq[-1].kind == p.kind:
        last = seq[-1]
        if (p.kind == "H" and p.price > last.price) or (p.kind == "L" and p.price < last.price):
            seq[-1] = p
        return
    seq.append(p)


def alternating_upto(pivots: list[Pivot], t: int) -> list[Pivot]:
    seq: list[Pivot] = []
    for p in pivots:
        if p.confirm <= t:
            add_alternating(seq, p)
    return seq


def structure(seq: list[Pivot]) -> Optional[str]:
    """Latest two confirmed highs and lows both rising -> long; both falling -> short."""
    hs = [p.price for p in seq if p.kind == "H"][-2:]
    ls = [p.price for p in seq if p.kind == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2:
        return None
    if hs[1] > hs[0] and ls[1] > ls[0]:
        return LONG
    if hs[1] < hs[0] and ls[1] < ls[0]:
        return SHORT
    return None


@dataclass
class BiasState:
    bias: Optional[str]
    invalidation: Optional[float]
    invalidated_on_last_bar: bool
    since: Optional[str]


def daily_bias(df: pd.DataFrame, pivots: list[Pivot]) -> BiasState:
    """[v1.0 D01] Stateful Daily bias walk. Wicks never invalidate; only a close beyond the level."""
    closes = df["close"].values
    by_confirm: dict[int, list[Pivot]] = {}
    for p in pivots:
        by_confirm.setdefault(p.confirm, []).append(p)
    seq: list[Pivot] = []
    bias: Optional[str] = None
    inv: Optional[float] = None
    since = None
    inv_last = False
    for t in range(len(closes)):
        inv_now = False
        if bias == LONG and closes[t] < inv:
            bias, inv, inv_now = None, None, True
        elif bias == SHORT and closes[t] > inv:
            bias, inv, inv_now = None, None, True
        for p in by_confirm.get(t, []):
            add_alternating(seq, p)
        lows = [p for p in seq if p.kind == "L"]
        highs = [p for p in seq if p.kind == "H"]
        if bias == LONG and lows and lows[-1].price > inv:
            inv = lows[-1].price
        elif bias == SHORT and highs and highs[-1].price < inv:
            inv = highs[-1].price
        if bias is None:
            # [IMPL] A bias cannot initialize while the close already sits beyond its own invalidation
            # level; otherwise a reassessment right after invalidation would re-arm the broken bias.
            s = structure(seq)
            if s == LONG and closes[t] >= lows[-1].price:
                bias, inv, since = LONG, lows[-1].price, str(df.index[t])
            elif s == SHORT and closes[t] <= highs[-1].price:
                bias, inv, since = SHORT, highs[-1].price, str(df.index[t])
        if t == len(closes) - 1:
            inv_last = inv_now
    return BiasState(bias, inv, inv_last, since)


# =============================================================================
# Impulse, zones, Fibonacci  [v1.0 Sections 3-5]
# =============================================================================

@dataclass
class Impulse:
    direction: str
    a_price: float
    b_price: float
    a_time: str
    b_time: str
    b_confirm_idx: int
    span: int
    efficiency: float
    atr_at_b: float
    atr_at_recognition: float
    a_idx: int
    b_idx: int

    def fib(self, r: float) -> float:
        return self.b_price - r * (self.b_price - self.a_price)


def select_impulse(df4: pd.DataFrame, pivots4: list[Pivot], atr4: np.ndarray, direction: str,
                   fcfg: dict) -> tuple[Optional[Impulse], str]:
    """[v1.0 I01] Latest qualifying 4H impulse whose B was confirmed within the last six completed bars."""
    icfg = fcfg["impulse"]
    T = len(df4) - 1
    seq = alternating_upto(pivots4, T)
    closes = df4["close"].values
    a_kind, b_kind = ("L", "H") if direction == LONG else ("H", "L")
    best: Optional[Impulse] = None
    reason = "no A/B pair confirmed recently"
    for i in range(1, len(seq)):
        A, B = seq[i - 1], seq[i]
        if A.kind != a_kind or B.kind != b_kind:
            continue
        if B.confirm < T - (int(icfg["max_age_4h_bars"]) - 1):
            continue
        span = B.k - A.k
        if not (int(icfg["min_span_bars"]) <= span <= int(icfg["max_span_bars"])):
            reason = f"span {span} bars outside limits"
            continue
        a_b = atr4[B.k]
        if not np.isfinite(a_b) or a_b <= 0:
            reason = "ATR unavailable at B"
            continue
        if abs(B.price - A.price) < float(icfg["min_atr_mult"]) * a_b:
            reason = "impulse smaller than 2 x ATR"
            continue
        path = np.abs(np.diff(closes[A.k:B.k + 1])).sum()
        if path <= 0:
            reason = "zero path length"
            continue
        eff = abs(closes[B.k] - closes[A.k]) / path
        if eff < float(icfg["min_efficiency"]):
            reason = f"efficiency {eff:.2f} below minimum"
            continue
        prev_same = [p for p in seq[:i - 1] if p.kind == b_kind]
        if not prev_same:
            reason = "no preceding swing to break"
            continue
        if direction == LONG and not B.price > prev_same[-1].price:
            reason = "B does not exceed preceding swing high"
            continue
        if direction == SHORT and not B.price < prev_same[-1].price:
            reason = "B does not break preceding swing low"
            continue
        if icfg.get("void_beyond_origin", True):
            after = closes[B.k + 1:T + 1]
            if (direction == LONG and (after < A.price).any()) or (direction == SHORT and (after > A.price).any()):
                reason = "price closed beyond impulse origin"
                continue
        a_rec = atr4[min(B.confirm, T)]
        cand = Impulse(direction, A.price, B.price, str(df4.index[A.k]), str(df4.index[B.k]), B.confirm,
                       span, float(eff), float(a_b), float(a_rec), A.k, B.k)
        if best is None or cand.b_confirm_idx >= best.b_confirm_idx:
            best = cand
    return best, ("" if best else reason)


def zone_half_width(inst: Instrument, atr4_value: float, fcfg: dict) -> float:
    """[v1.0 Z01] max(two ticks, 0.10 x 4H ATR14 at setup recognition)."""
    a = atr4_value if np.isfinite(atr4_value) else 0.0
    return max(int(fcfg["zone_min_ticks"]) * inst.tick, float(fcfg["zone_atr_fraction"]) * a)


def nearest_level(inst: Instrument, price: float) -> tuple[float, str]:
    """Nearest integer multiple of the midpoint spacing; majors are the even multiples."""
    lvl = round(price / inst.grid_mid) * inst.grid_mid
    ratio = lvl / inst.grid_major
    kind = "major" if abs(ratio - round(ratio)) < 1e-9 else "mid"
    return lvl, kind


def in_psych_zone(inst: Instrument, price: float, hw: float) -> tuple[bool, float, str]:
    lvl, kind = nearest_level(inst, price)
    return abs(price - lvl) <= hw + 1e-12, lvl, kind


# =============================================================================
# Confluences  [v1.0 Section 4]
# =============================================================================

def candle_signal(df2: pd.DataFrame, atr2: np.ndarray, inst: Instrument, direction: str, ccfg: dict) -> str:
    """[v1.0 C4] Directional engulfing or rejection on the just-completed 2H candle. Returns '' if none."""
    if len(df2) < 2:
        return ""
    o, h, l, c = (df2[x].values for x in ("open", "high", "low", "close"))
    t = len(df2) - 1
    a = atr2[t]
    if not np.isfinite(a) or a <= 0:
        return ""
    body = abs(c[t] - o[t])
    rng = h[t] - l[t]
    if direction == LONG:
        if (c[t - 1] < o[t - 1] and c[t] > o[t] and o[t] <= c[t - 1] and c[t] >= o[t - 1]
                and body >= float(ccfg["engulf_body_atr"]) * a):
            return "bullish engulfing"
        if rng > 0 and c[t] - o[t] >= inst.tick:
            lower = min(o[t], c[t]) - l[t]
            upper = h[t] - max(o[t], c[t])
            if (lower >= 2 * body and upper <= body and (c[t] - l[t]) / rng >= 1 - float(ccfg["rejection_close_top"])
                    and rng >= float(ccfg["rejection_range_atr"]) * a):
                return "bullish rejection"
    else:
        if (c[t - 1] > o[t - 1] and c[t] < o[t] and o[t] >= c[t - 1] and c[t] <= o[t - 1]
                and body >= float(ccfg["engulf_body_atr"]) * a):
            return "bearish engulfing"
        if rng > 0 and o[t] - c[t] >= inst.tick:
            upper = h[t] - max(o[t], c[t])
            lower = min(o[t], c[t]) - l[t]
            if (upper >= 2 * body and lower <= body and (h[t] - c[t]) / rng >= 1 - float(ccfg["rejection_close_top"])
                    and rng >= float(ccfg["rejection_range_atr"]) * a):
                return "bearish rejection"
    return ""


def trendline_signal(df4: pd.DataFrame, pivots4: list[Pivot], df2: pd.DataFrame, direction: str,
                     hw: float) -> tuple[bool, str]:
    """[v1.0 C6] Line through the last two rising 4H lows (long) or falling 4H highs (short),
    extrapolated by timestamp to the 2H close. Broken if any 2H close since the second anchor became
    known closed beyond the line by more than the zone width. Current candle must touch line +/- width
    and close on the trend side."""
    seq = alternating_upto(pivots4, len(df4) - 1)
    kind = "L" if direction == LONG else "H"
    pts = [p for p in seq if p.kind == kind][-2:]
    if len(pts) < 2:
        return False, "fewer than two 4H swings"
    p1, p2 = pts
    if direction == LONG and not p2.price > p1.price:
        return False, "last two 4H lows not rising"
    if direction == SHORT and not p2.price < p1.price:
        return False, "last two 4H highs not falling"
    t1 = df4.index[p1.k].value / 1e9
    t2 = df4.index[p2.k].value / 1e9
    if t2 <= t1:
        return False, "bad anchors"
    slope = (p2.price - p1.price) / (t2 - t1)
    known = df4.index[p2.confirm] + TF_DELTA[TF_4H]
    close_t = (df2.index + TF_DELTA[TF_2H])
    sel = close_t >= known
    if not sel.any():
        return False, "no 2H bar since second anchor known"
    sub = df2[sel]
    tt = np.array([x.value / 1e9 for x in close_t[sel]])
    line = p1.price + slope * (tt - t1)
    closes = sub["close"].values
    if direction == LONG and (closes < line - hw).any():
        return False, "trend line broken"
    if direction == SHORT and (closes > line + hw).any():
        return False, "trend line broken"
    lv = line[-1]
    hi, lo, cl = sub["high"].values[-1], sub["low"].values[-1], closes[-1]
    touches = lo <= lv + hw and hi >= lv - hw
    side = cl >= lv if direction == LONG else cl <= lv
    if touches and side:
        return True, f"touch and recovery at {lv:.5g}"
    return False, "no touch and recovery on latest 2H bar"


# =============================================================================
# COT overlay  [Add 6.2, v1.0 Section 10]
# =============================================================================

@dataclass
class CotSeries:
    market: str
    family: str
    frame: pd.DataFrame          # columns: report_date, publication_ts, net, open_interest
    market_name: str = ""


def cot_publication_ts(report_date: pd.Timestamp, cfg_cot: dict) -> pd.Timestamp:
    """[IMPL] Assumed release: position date + 3 days at 15:30 Eastern. Holiday delays are not modelled."""
    hh, mm = (int(x) for x in str(cfg_cot.get("publication_time_et", "15:30")).split(":"))
    d = (report_date + pd.Timedelta(days=int(cfg_cot.get("publication_offset_days", 3)))).date()
    return pd.Timestamp(datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET_TZ)).tz_convert(UTC)


class CotSource:
    def __init__(self, cfg: dict, base: Path):
        self.cfg = cfg["cot"]
        self.base = base

    def load(self) -> dict[str, CotSeries]:
        src = self.cfg.get("source", "cftc_api")
        if src == "off":
            return {}
        if src == "csv":
            return self._load_csv()
        return self._load_api()

    def _load_api(self) -> dict[str, CotSeries]:
        import requests
        out: dict[str, CotSeries] = {}
        since = (pd.Timestamp.now(tz=UTC) - pd.Timedelta(weeks=int(self.cfg.get("fetch_weeks", 70)))).strftime("%Y-%m-%d")
        for mkt, m in self.cfg["markets"].items():
            ds = self.cfg["datasets"][m["family"]]
            url = f"{self.cfg['api_base']}/{ds}.json"
            params = {
                "$select": f"report_date_as_yyyy_mm_dd,market_and_exchange_names,futonly_or_combined,"
                           f"open_interest_all,{m['long']},{m['short']}",
                "$where": f"cftc_contract_market_code='{m['code']}' AND report_date_as_yyyy_mm_dd >= '{since}'",
                "$order": "report_date_as_yyyy_mm_dd ASC",
                "$limit": 500,
            }
            try:
                r = requests.get(url, params=params, timeout=30)
                r.raise_for_status()
                rows = r.json()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("COT fetch failed for %s: %s", mkt, exc)
                continue
            if not rows:
                LOG.warning("COT: no rows for %s (code %s)", mkt, m["code"])
                continue
            df = pd.DataFrame(rows)
            df = df[df.get("futonly_or_combined", "FutOnly") == "FutOnly"] if "futonly_or_combined" in df else df
            df["report_date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"]).dt.tz_localize(None)
            df["net"] = df[m["long"]].astype(float) - df[m["short"]].astype(float)
            df["open_interest"] = df["open_interest_all"].astype(float)
            df["publication_ts"] = [cot_publication_ts(pd.Timestamp(d), self.cfg) for d in df["report_date"]]
            name = str(df["market_and_exchange_names"].iloc[-1]) if "market_and_exchange_names" in df else ""
            LOG.info("COT %s -> %s (%d weeks)", mkt, name, len(df))
            out[mkt] = CotSeries(mkt, m["family"], df[["report_date", "publication_ts", "net", "open_interest"]]
                                 .drop_duplicates("report_date").reset_index(drop=True), name)
        cache = self.base / "data" / "cot"
        cache.mkdir(parents=True, exist_ok=True)
        try:
            rows = []
            for s in out.values():
                for _, r in s.frame.iterrows():
                    rows.append({"market": s.market, "family": s.family, "report_date": r.report_date.date(),
                                 "publication_ts": r.publication_ts.isoformat(), "net": r.net,
                                 "open_interest": r.open_interest})
            pd.DataFrame(rows).to_csv(cache / "cot_latest_pull.csv", index=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("COT cache write failed: %s", exc)
        return out

    def _load_csv(self) -> dict[str, CotSeries]:
        p = (self.base / self.cfg["csv_path"]).resolve()
        df = pd.read_csv(p)
        out = {}
        for mkt, g in df.groupby("market"):
            g = g.copy()
            g["report_date"] = pd.to_datetime(g["report_date"]).dt.tz_localize(None)
            g["net"] = g["long"].astype(float) - g["short"].astype(float)
            g["open_interest"] = g["open_interest"].astype(float)
            if "publication_ts" in g and g["publication_ts"].notna().all():
                g["publication_ts"] = pd.to_datetime(g["publication_ts"], utc=True)
            else:
                g["publication_ts"] = [cot_publication_ts(pd.Timestamp(d), self.cfg) for d in g["report_date"]]
            fam = str(g["report_family"].iloc[0]) if "report_family" in g else ""
            out[str(mkt)] = CotSeries(str(mkt), fam, g.sort_values("report_date")[
                ["report_date", "publication_ts", "net", "open_interest"]].reset_index(drop=True))
        return out


@dataclass
class CotReading:
    index: Optional[float]          # 0-100, already mapped to the instrument (long side)
    prev_index: Optional[float]
    weekly_change: Optional[float]  # >0 rising, for the instrument's long side
    publication: Optional[str]
    report_date: Optional[str]
    family: str
    status: str                     # ok | unavailable | stale


def cot_index_at(series: CotSeries, asof: pd.Timestamp, weeks: int, offset: int = 0) -> tuple[Optional[float], Optional[pd.Series]]:
    """[v1.0 10] COTIndex = 100 x (Net - Min52) / (Max52 - Min52) over 52 published weeks incl. the latest.
    offset=1 returns the previous week's index."""
    f = series.frame[series.frame["publication_ts"] <= asof]
    if offset:
        f = f.iloc[:-offset] if len(f) > offset else f.iloc[0:0]
    if len(f) < weeks:
        return None, (f.iloc[-1] if len(f) else None)
    win = f.iloc[-weeks:]
    last = win.iloc[-1]
    if last["open_interest"] <= 0:
        return None, last
    lo, hi = win["net"].min(), win["net"].max()
    if hi == lo:
        return None, last
    return float(100 * (last["net"] - lo) / (hi - lo)), last


def cot_for_instrument(inst: Instrument, cot: dict[str, CotSeries], asof: pd.Timestamp, cfg_cot: dict) -> CotReading:
    weeks = int(cfg_cot.get("index_weeks", 52))
    stale_days = int(cfg_cot.get("stale_days", 10))

    def single(mkt: str):
        s = cot.get(mkt)
        if s is None:
            return None
        idx, last = cot_index_at(s, asof, weeks)
        pidx, _ = cot_index_at(s, asof, weeks, offset=1)
        f = s.frame[s.frame["publication_ts"] <= asof]
        chg = float(f["net"].iloc[-1] - f["net"].iloc[-2]) if len(f) >= 2 else None
        return idx, pidx, chg, last, s.family

    def fmt(last):
        if last is None:
            return None, None
        return pd.Timestamp(last["publication_ts"]).isoformat(), str(pd.Timestamp(last["report_date"]).date())

    if inst.asset == "fx":
        legs = {}
        for ccy in (inst.base, inst.quote):
            if ccy != "USD":
                legs[ccy] = single(ccy)
        if any(v is None for v in legs.values()):
            return CotReading(None, None, None, None, None, "TFF", "unavailable")
        if inst.quote == "USD":
            idx, pidx, chg, last, fam = legs[inst.base]
        elif inst.base == "USD":
            i, p, c, last, fam = legs[inst.quote]
            # [Add 6.2] USDJPY, USDCHF, USDCAD invert the sign: index read as 100 minus index
            idx = None if i is None else 100 - i
            pidx = None if p is None else 100 - p
            chg = None if c is None else -c
        else:
            bi, bp, _, blast, fam = legs[inst.base]
            qi, qp, _, qlast, _ = legs[inst.quote]
            # [Add 6.2] crosses: 50 + (base index - quote index) / 2; [IMPL] weekly change = index change
            idx = None if bi is None or qi is None else 50 + (bi - qi) / 2
            pidx = None if bp is None or qp is None else 50 + (bp - qp) / 2
            chg = None if idx is None or pidx is None else idx - pidx
            last = blast if blast is not None and (qlast is None or blast["publication_ts"] <= qlast["publication_ts"]) else qlast
    else:
        r = single(inst.cot) if inst.cot else None
        if r is None:
            return CotReading(None, None, None, None, None, "", "unavailable")
        idx, pidx, chg, last, fam = r
    pub, rdate = fmt(last)
    if idx is None or chg is None:
        return CotReading(idx, pidx, chg, pub, rdate, fam, "unavailable")
    age = (asof - pd.Timestamp(pub)).total_seconds() / 86400
    if age > stale_days:
        return CotReading(idx, pidx, chg, pub, rdate, fam, "stale")
    return CotReading(idx, pidx, chg, pub, rdate, fam, "ok")


def cot_points(reading: CotReading, direction: str, cfg_cot: dict) -> int:
    """[Add 6.2] Points for a long setup; shorts mirror (index -> 100 - index, change -> -change)."""
    if reading.status != "ok" or reading.index is None or reading.weekly_change is None:
        return 0
    idx, chg = reading.index, reading.weekly_change
    if direction == SHORT:
        idx, chg = 100 - idx, -chg
    cl, el = float(cfg_cot["crowded_long"]), float(cfg_cot["elevated_long"])
    nl, cs = float(cfg_cot["neutral_low"]), float(cfg_cot["crowded_short"])
    if idx >= cl:
        return -2
    if idx >= el:
        return -1
    if idx >= nl:
        return 1 if chg > 0 else 0
    if idx > cs:
        return 1 if chg > 0 else 0
    return 2 if chg > 0 else 0


# =============================================================================
# Calendar  [Add 6.4]
# =============================================================================

def load_calendar(cfg: dict, base: Path, asof: pd.Timestamp, demo: bool) -> tuple[list[dict], str]:
    ccfg = cfg.get("calendar", {})
    if demo:
        evs = [{"title": "Demo CPI", "country": "USD", "date": (asof + pd.Timedelta(hours=9)).isoformat(), "impact": "High"},
               {"title": "Demo ECB rate decision", "country": "EUR", "date": (asof + pd.Timedelta(hours=40)).isoformat(), "impact": "High"},
               {"title": "Demo BOJ outlook", "country": "JPY", "date": (asof + pd.Timedelta(hours=20)).isoformat(), "impact": "High"}]
        return evs, "demo"
    if ccfg.get("source", "forexfactory") == "off":
        return [], "off"
    import requests
    try:
        r = requests.get(ccfg["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0 scanner"})
        r.raise_for_status()
        evs = r.json()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("calendar fetch failed: %s", exc)
        return [], f"failed: {exc}"
    if ccfg.get("snapshot", True):
        d = base / cfg["paths"]["data_dir"] / "calendar_snapshots"
        d.mkdir(parents=True, exist_ok=True)
        yr, wk, _ = asof.isocalendar()
        with open(d / f"ff_{yr}-W{wk:02d}_{asof.strftime('%Y%m%dT%H%MZ')}.json", "w", encoding="utf-8") as fh:
            json.dump(evs, fh)
    return evs, f"fetched {pd.Timestamp.now(tz=UTC).strftime('%Y-%m-%d %H:%M')} UTC"


def next_event(evs: list[dict], ccys: list[str], asof: pd.Timestamp, ccfg: dict) -> dict:
    impact = ccfg.get("impact", "High")
    best = None
    for e in evs:
        if e.get("impact") != impact or e.get("country") not in ccys:
            continue
        try:
            t = pd.Timestamp(e["date"]).tz_convert(UTC)
        except Exception:  # noqa: BLE001
            continue
        if t <= asof:
            continue
        if best is None or t < best[0]:
            best = (t, e)
    if best is None:
        return {"event": "", "currency": "", "time_utc": "", "hours": None, "event_risk": False}
    hours = (best[0] - asof).total_seconds() / 3600
    return {"event": best[1].get("title", ""), "currency": best[1].get("country", ""),
            "time_utc": best[0].strftime("%Y-%m-%d %H:%M"), "hours": round(hours, 1),
            "event_risk": hours <= float(ccfg.get("event_risk_hours", 24))}


# =============================================================================
# Headlines and sentiment  [Add 6.3 and 7]
# =============================================================================

@dataclass
class Headline:
    title: str
    source: str
    published: str          # ISO UTC
    link: str = ""
    tone: Optional[float] = None
    theme: Optional[str] = None


def _kw_regex(words: list[str]) -> list[re.Pattern]:
    pats = []
    for w in words:
        w = str(w)
        flags = 0 if (w.isupper() and len(w.replace(".", "")) <= 4) else re.IGNORECASE
        pats.append(re.compile(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", flags))
    return pats


def _hits(pats: list[re.Pattern], text: str) -> int:
    return sum(1 for p in pats if p.search(text))


def parse_feed(xml_text: str, source: str) -> list[Headline]:
    """Minimal RSS 2.0 / Atom parser (stdlib only)."""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    ns_atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall(".//item") or root.findall(f".//{ns_atom}entry")
    for it in items:
        title = (it.findtext("title") or it.findtext(f"{ns_atom}title") or "").strip()
        link = it.findtext("link") or ""
        if not link:
            le = it.find(f"{ns_atom}link")
            link = le.get("href", "") if le is not None else ""
        ds = (it.findtext("pubDate") or it.findtext(f"{ns_atom}published") or it.findtext(f"{ns_atom}updated")
              or it.findtext("{http://purl.org/dc/elements/1.1/}date") or "")
        try:
            ts = pd.Timestamp(pd.to_datetime(ds, utc=True))
        except Exception:  # noqa: BLE001
            continue
        if title and not pd.isna(ts):
            out.append(Headline(title=title, source=source, published=ts.isoformat(), link=link.strip()))
    return out


def load_headlines(cfg: dict, base: Path, asof: pd.Timestamp, demo: bool) -> tuple[list[Headline], str]:
    scfg = cfg["sentiment"]
    if demo:
        mk = lambda h, t, s: Headline(t, s, (asof - pd.Timedelta(hours=h)).isoformat())  # noqa: E731
        return [mk(2, "Fed officials signal hawkish stance, higher for longer on rates", "demo"),
                mk(5, "ECB's Lagarde hints at rate cut as eurozone growth slows", "demo"),
                mk(8, "Missile attack in Middle East raises fears of escalation", "demo"),
                mk(3, "OPEC+ agrees surprise production cut to support oil prices", "demo"),
                mk(10, "US payrolls beat forecasts as labor market stays strong", "demo"),
                mk(6, "Bank of Japan keeps policy steady, yen weaker", "demo"),
                mk(12, "Wall Street stocks rally to record high on strong earnings", "demo")], "demo"
    feeds = scfg.get("feeds") or []
    if not feeds:
        return [], "no feeds configured"
    import requests
    out: list[Headline] = []
    for f in feeds:
        try:
            r = requests.get(f["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0 scanner"})
            r.raise_for_status()
            out += parse_feed(r.text, f.get("name", f["url"]))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("feed %s failed: %s", f.get("name"), exc)
    # Archive: no historical headline archive is planned, so capture going forward
    arch = base / cfg["paths"]["data_dir"] / "headlines"
    arch.mkdir(parents=True, exist_ok=True)
    seen = set()
    path = arch / f"{asof.strftime('%Y-%m-%d')}.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                seen.add(json.loads(line)["title"])
            except Exception:  # noqa: BLE001
                pass
    with open(path, "a", encoding="utf-8") as fh:
        for h in out:
            if h.title not in seen:
                fh.write(json.dumps(asdict(h)) + "\n")
                seen.add(h.title)
    return out, f"{len(out)} headlines from {len(feeds)} feeds"


class ToneModel:
    """FinBERT tone s = P(positive) - P(negative) in [-1, 1]. Falls back to a crude lexicon if asked."""

    def __init__(self, cfg: dict):
        self.scfg = cfg["sentiment"]
        self.kind = self.scfg.get("model", "finbert")
        self.pipe = None
        self.status = self.kind
        if self.kind == "finbert":
            try:
                from transformers import pipeline
                self.pipe = pipeline("text-classification", model=self.scfg.get("finbert_model", "ProsusAI/finbert"),
                                     top_k=None, truncation=True)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("FinBERT unavailable (%s); sentiment scores 0", exc)
                self.kind, self.status = "off", "FinBERT unavailable"
        self.pos = _kw_regex(self.scfg.get("lexicon_positive", []))
        self.neg = _kw_regex(self.scfg.get("lexicon_negative", []))

    def score(self, texts: list[str]) -> list[Optional[float]]:
        if self.kind == "off" or not texts:
            return [None] * len(texts)
        if self.kind == "lexicon":
            out = []
            for t in texts:
                p, n = _hits(self.pos, t), _hits(self.neg, t)
                out.append(0.0 if p + n == 0 else (p - n) / (p + n))
            return out
        res = self.pipe(texts, batch_size=16)
        out = []
        for r in res:
            d = {x["label"].lower(): x["score"] for x in r}
            out.append(float(d.get("positive", 0) - d.get("negative", 0)))
        return out


class SentimentEngine:
    CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}

    def __init__(self, cfg: dict):
        s = cfg["sentiment"]
        self.s = s
        self.theme_order = list(s["theme_keywords"].keys())
        self.theme_pats = {k: _kw_regex(v) for k, v in s["theme_keywords"].items()}
        self.entity_pats = {k: _kw_regex(v) for k, v in s["entity_keywords"].items()}
        self.region_pats = _kw_regex(s.get("oil_producer_regions", []))
        self.hawk = _kw_regex(s.get("hawk_words", []))
        self.dove = _kw_regex(s.get("dove_words", []))
        self.cut = _kw_regex(s.get("supply_cut_words", []))
        self.inc = _kw_regex(s.get("supply_increase_words", []))
        self.rules = {(r["entity"], r["theme"]): r["mode"] for r in s.get("direction_rules", [])}

    def tag_theme(self, text: str) -> Optional[str]:
        best, best_n = None, 0
        for th in self.theme_order:
            n = _hits(self.theme_pats[th], text)
            if n > best_n:
                best, best_n = th, n
        return best

    def matches(self, entity: str, h: Headline) -> bool:
        if _hits(self.entity_pats.get(entity, []), h.title) > 0:
            return True
        auto = self.s.get("entity_auto_themes", {}).get(entity, [])
        if h.theme in auto:
            return True
        if entity == "OIL" and h.theme == "geopolitical" and _hits(self.region_pats, h.title) > 0:
            return True
        return False

    def directional(self, entity: str, h: Headline) -> float:
        """[Add 7] Tone is not direction: convert tone into price direction for this entity."""
        key = "CURRENCY" if entity in self.CURRENCIES else entity
        mode = self.rules.get((key, h.theme), "tone")
        tone = h.tone or 0.0
        if mode == "tone":
            return tone
        if mode == "invert_tone":
            return -tone
        if mode in ("hawk", "invert_hawk"):
            a, b = _hits(self.hawk, h.title), _hits(self.dove, h.title)
            hv = 0.0 if a + b == 0 else (a - b) / (a + b)
            return hv if mode == "hawk" else -hv
        if mode == "supply":
            a, b = _hits(self.cut, h.title), _hits(self.inc, h.title)
            return 0.0 if a + b == 0 else (a - b) / (a + b)
        return tone

    def weight(self, inst: Instrument, entity: str, theme: str) -> float:
        w = float(self.s["theme_weights"].get(theme, {}).get(inst.asset, 0.0))
        if inst.asset == "fx":
            w = float(self.s.get("currency_overrides", {}).get(entity, {}).get(theme, w))
        return w

    def score(self, inst: Instrument, heads: list[Headline], asof: pd.Timestamp) -> dict:
        """S = sum(s * w * d * r) / sum(w * r), long side, in [-1, 1]."""
        win = float(self.s.get("window_hours", 24))
        hl = float(self.s.get("half_life_hours", 12))
        num = den = wsum = 0.0
        drivers = []
        for h in heads:
            if h.tone is None or h.theme is None:
                continue
            age = (asof - pd.Timestamp(h.published)).total_seconds() / 3600
            if age < 0 or age > win:
                continue
            r = 0.5 ** (age / hl)
            for entity, d in inst.sentiment_legs:
                if not self.matches(entity, h):
                    continue
                w = self.weight(inst, entity, h.theme)
                if w <= 0:
                    continue
                s_eff = self.directional(entity, h)
                num += s_eff * w * d * r
                den += w * r
                wsum += w
                drivers.append((abs(s_eff * w * r), h, entity, s_eff * d))
        S = num / den if den > 0 else None
        drivers.sort(key=lambda x: -x[0])
        top, seen = [], set()
        for _, h, ent, sd in drivers:
            if h.title in seen:
                continue
            seen.add(h.title)
            top.append({"title": h.title, "theme": h.theme, "source": h.source, "entity": ent,
                        "direction": round(sd, 2), "link": h.link})
            if len(top) == 3:
                break
        thin = wsum < float(self.s.get("min_weight_sum", 1.0))
        return {"S": None if S is None else round(S, 3), "weight_sum": round(wsum, 2), "thin": thin, "drivers": top}

    def points(self, S: Optional[float], thin: bool, direction: str) -> int:
        if S is None or thin:
            return 0
        v = S if direction == LONG else -S
        p = self.s["points"]
        if v >= p["plus2"]:
            return 2
        if v >= p["plus1"]:
            return 1
        if v > p["minus1"]:
            return 0
        if v > p["minus2"]:
            return -1
        return -2


# =============================================================================
# Futures roll windows  [Add 3]
# =============================================================================

def _third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def in_roll_window(day: date, rule_key: str, rules: dict) -> bool:
    r = rules.get(rule_key)
    if not r:
        return False
    if r["rule"] == "third_friday_minus":
        if day.month not in r["months"]:
            return False
        tf = _third_friday(day.year, day.month)
        return tf - timedelta(days=int(r["days_before"])) <= day <= tf
    if r["rule"] == "prior_month_from_day":
        nxt = 1 if day.month == 12 else day.month + 1
        return nxt in r["months"] and day.day >= int(r["from_day"])
    # --- PASTE TRUNCATED HERE (chat 50,000 character limit). Part 2 continues from this line. ---
