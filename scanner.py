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

Trading method (owner rules, cfg-0.2.0 to cfg-0.4.1)
    Entry sequence the scanner screens for:
        wick forming in a key level zone -> reversal candle CLOSES -> at least 3 confluences.
    The zone is where the market makes its decision; the confirmation close only tells you which
    way it decided, so that close may (and often will) finish outside the zone.

    1. Key levels are zones, not lines.                       psych_zone_half_width, in_psych_zone
       Every major and mid grid level gets a box of fixed half-width (grids.*.zone_half_width):
         market        major / mid level spacing      zone half-width
         FX            500 / 250 pips                 15 pips   (1.3000 -> 1.2985 to 1.3015)
         JPY pairs     5.00 / 2.50                    0.15      (15 pips)
         Gold          $100 / $50                     $20       (3,300 -> 3,280 to 3,320)
         S&P           100 / 50 points                20 points (follows gold for now)
         Oil           $5.00 / $2.50                  $1.00     (gold's 20% of major spacing; placeholder)
       Why: institutions' orders sit spread around a round number, so price reacts across an area.
       Gold gets a wider box because news-driven overshoots of $15-25 are not breaks.
       C2 = latest completed 2H close inside the zone. Fib (C3) and trend line (C6) tolerance still
       use the v1.0 ATR width (zone_half_width).

    2. Wick principle (early alert, never scored).           zone_wick_tests
       Recent 2H candles whose wick reaches into the zone and is rejected in the trade direction.
       Two or more in the last 6 bars adds the flag "zone tested: N wicks". Not an entry.

    3. C4 = reversal confirmation that started AT the zone.   candle_signal, _pattern_at, chart_pattern
       Checked on the latest completed Daily, then 4H, then 2H bar; the highest timeframe wins.
       Candles (any timeframe): hammer, inverted hammer, shooting star, hanging man, engulfing,
       tweezer top/bottom, morning/evening star, and a marubozu closing right after one of those.
       Pins, tweezers and stars need the matching prior trend (hammer vs hanging man).
       Engulfing: the engulfing candle itself must wick into the zone; its close may be beyond it.
       Doji alone is indecision and never counts. Outside a zone, C4 is always false.
       Chart patterns (Daily and 4H): double bottom/top and (inverse) head and shoulders. A bottom
       (or the head) must sit in a key level zone; the confirmation is a candle CLOSE beyond the
       neckline (a wick through it is a fakeout), within the last 3 bars of that timeframe.

    4. Qualification.                                          setup_section, rank
       Qualified  = C1 + C4 + at least 3 of C1-C6. Nothing qualifies without a zone reversal.
       Developing = C1 + C2 (price in the zone) + 2 or more checks, reversal not closed yet.
       Equal totals rank Daily confirmations above 4H, and 4H above 2H.

What it does NOT do
    It places no orders or sizes no positions. The qualification rule is a screening change, not a
    change to v1.0 execution requirements. COT and sentiment overlays rank; they never qualify.

Usage
    python scanner.py --demo                      # offline run on synthetic data
    python scanner.py --run evening               # live run (TradingView + CFTC + Forex Factory)
    python scanner.py --run preny
    python scanner.py --daemon                    # stay running, fire at 00:05 and 12:05 UTC
    python scanner.py --journal                   # rebuild only output/journal.html from MT5

MetaTrader 5 (cfg-0.5.0, read only)
    Currency pairs use Forex.com bars from the running MT5 terminal; CFDs and futures use TradingView.
    The dashboard shows MT5 broker prices and open positions. output/journal.html shows balance,
    a balance snapshot per run, and every MT5 position with the confluences the last scan before the
    entry logged for that instrument and direction. Nothing here sends, changes or closes orders.
    python scanner.py --source csv --asof 2026-10-02T12:05Z

Rule labels
    Comments tagged [v1.0 ...] or [Add ...] cite the governing section. Comments tagged
    [OWNER cfg-x.y.z] are trading-method rules the owner set, with the config version that added
    them (see the changelog in scanner_config.yaml). Comments tagged [IMPL] mark implementation
    choices the documents leave open; they are test defaults.
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

import news as newsmod

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install pyyaml")

SCANNER_CODE_VERSION = "0.6.0"
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
    psych_zone_hw: Optional[float] = None  # fixed zone half-width around grid levels; None = not set

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


def _grid_zone_hw(g: dict) -> Optional[float]:
    v = g.get("zone_half_width")
    if v is None:
        return None
    v = float(v)
    if v <= 0:
        raise ValueError(f"grid zone_half_width must be positive, got {v}")
    return v


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
                                  psych_zone_hw=_grid_zone_hw(g), tv_symbol=sym, tv_exchange=cfg["fx"].get("tv_exchange", "OANDA"),
                                  base=base, quote=quote))
    for it in cfg.get("other_instruments", []):
        g = grids[it["grid"]]
        out.append(Instrument(symbol=it["symbol"], group=it["group"], asset=it["asset"], tick=float(it["tick"]),
                              grid_major=float(g["major"]), grid_mid=float(g["mid"]),
                              psych_zone_hw=_grid_zone_hw(g), tv_symbol=it["tv_symbol"], tv_exchange=it["tv_exchange"],
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


# =============================================================================
# MetaTrader 5  [OWNER cfg-0.5.0]  read only: this module never sends orders
# =============================================================================

def mt5_to_utc(server_secs, server_time: Any = "ny_close") -> pd.DatetimeIndex:
    """MT5 stamps bars, ticks and deals in broker server time written as if it were UTC.
    ny_close: server clock = New York time + 7h (UTC+3 in US summer, UTC+2 in winter), so the daily
    bar opens at the 17:00 New York close. An integer is a fixed server offset in hours."""
    naive = pd.to_datetime(np.asarray(server_secs, dtype="int64"), unit="s")
    if server_time == "ny_close":
        et = pd.DatetimeIndex(naive - pd.Timedelta(hours=7)).tz_localize(ET_TZ, ambiguous=False, nonexistent="shift_forward")
        return et.tz_convert(UTC)
    return pd.DatetimeIndex(naive - pd.Timedelta(hours=float(server_time))).tz_localize(UTC)


def pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol else 0.0001


class Mt5Client:
    """Thin wrapper over the MetaTrader5 package, attached to the terminal already running and logged in."""

    def __init__(self, cfg: dict):
        self.cfg = cfg.get("mt5", {})
        self.server_time = self.cfg.get("server_time", "ny_close")
        self.symbol_map: dict[str, str] = dict(self.cfg.get("symbol_map") or {})
        self.reverse_map = {v: k for k, v in self.symbol_map.items()}
        self.mt5 = None
        self.status = "not connected"
        self.clock_note = ""

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            self.status = "MetaTrader5 package not installed"
            return False
        path = self.cfg.get("terminal_path")
        ok = mt5.initialize(path) if path else mt5.initialize()
        if not ok:
            self.status = f"initialize failed: {mt5.last_error()}"
            return False
        self.mt5 = mt5
        term, acct = mt5.terminal_info(), mt5.account_info()
        self.status = (f"{acct.server if acct else '?'}, terminal build {mt5.version()[1]}, "
                       f"{'connected' if term and term.connected else 'NOT connected to broker'}")
        self._check_clock()
        return True

    def _check_clock(self) -> None:
        """Compare a live tick with the configured server_time rule; warn when they disagree."""
        sym = self.mt5_symbol("EURUSD")
        self.mt5.symbol_select(sym, True)
        tk = self.mt5.symbol_info_tick(sym)
        if not tk:
            self.clock_note = "server clock unchecked (no tick)"
            return
        observed = (tk.time - time.time()) / 3600
        now = pd.Timestamp.now(tz=UTC)
        expected = (now.tz_convert(ET_TZ).utcoffset().total_seconds() / 3600 + 7 if self.server_time == "ny_close"
                    else float(self.server_time))
        if abs(observed - expected) < 0.1:
            self.clock_note = f"server clock UTC{expected:+.0f}h verified"
        elif abs(observed - round(observed)) < 0.1:
            self.clock_note = f"server clock UTC{observed:+.1f}h but config expects UTC{expected:+.0f}h: CHECK mt5.server_time"
            LOG.warning("MT5 %s", self.clock_note)
        else:
            self.clock_note = f"server clock unchecked (last tick {observed - expected:+.1f}h old)"

    def shutdown(self) -> None:
        if self.mt5:
            self.mt5.shutdown()
            self.mt5 = None

    def mt5_symbol(self, scanner_symbol: str) -> str:
        return self.symbol_map.get(scanner_symbol, scanner_symbol)

    def scanner_symbol(self, mt5_symbol: str) -> str:
        return self.reverse_map.get(mt5_symbol, mt5_symbol)

    def to_utc(self, secs) -> pd.DatetimeIndex:
        return mt5_to_utc(secs, self.server_time)

    def ts(self, secs: int) -> pd.Timestamp:
        return self.to_utc([secs])[0]

    def rates(self, symbol: str, tf: str, n: int) -> pd.DataFrame:
        m = self.mt5
        tfc = {"M5": m.TIMEFRAME_M5, TF_1H: m.TIMEFRAME_H1, TF_2H: m.TIMEFRAME_H2, TF_4H: m.TIMEFRAME_H4, TF_D: m.TIMEFRAME_D1}[tf]
        m.symbol_select(symbol, True)
        r = m.copy_rates_from_pos(symbol, tfc, 0, int(n))
        if r is None or len(r) == 0:
            raise RuntimeError(f"no MT5 bars: {m.last_error()}")
        df = pd.DataFrame({k: r[k] for k in ("open", "high", "low", "close")}, index=self.to_utc(r["time"]))
        return normalize_bars(df)

    def prices(self, symbols: list[str]) -> list[dict]:
        out = []
        for s in symbols:
            ms = self.mt5_symbol(s)
            self.mt5.symbol_select(ms, True)
            tk = self.mt5.symbol_info_tick(ms)
            if not tk or not tk.bid or not tk.ask:
                out.append({"symbol": s, "mt5_symbol": ms, "bid": None, "ask": None, "spread_pips": None, "time_utc": ""})
                continue
            out.append({"symbol": s, "mt5_symbol": ms, "bid": tk.bid, "ask": tk.ask,
                        "spread_pips": round((tk.ask - tk.bid) / pip_size(ms), 1),
                        "time_utc": self.ts(tk.time).strftime("%Y-%m-%d %H:%M:%S")})
        return out

    def account(self) -> dict:
        a = self.mt5.account_info()
        if not a:
            return {}
        return {"login": a.login, "server": a.server, "currency": a.currency,
                "mode": {0: "demo", 1: "contest", 2: "real"}.get(a.trade_mode, str(a.trade_mode)),
                "balance": a.balance, "equity": a.equity, "margin": a.margin, "free_margin": a.margin_free,
                "margin_level": a.margin_level, "profit": a.profit, "leverage": a.leverage}

    def positions(self) -> list[dict]:
        out = []
        for p in self.mt5.positions_get() or []:
            out.append({"ticket": p.ticket, "symbol": self.scanner_symbol(p.symbol), "mt5_symbol": p.symbol,
                        "direction": LONG if p.type == 0 else SHORT, "volume": p.volume,
                        "open_utc": self.ts(p.time).strftime("%Y-%m-%d %H:%M"), "open_price": p.price_open,
                        "price": p.price_current, "sl": p.sl or None, "tp": p.tp or None, "swap": p.swap,
                        "profit": p.profit, "magic": p.magic, "comment": p.comment})
        return out

    def deals(self, days: int) -> list[dict]:
        # history_deals_get reads the arguments as server time; pad both ends so the window is never short
        now = datetime.now()
        ds = self.mt5.history_deals_get(now - timedelta(days=days + 1), now + timedelta(days=2)) or []
        return [{"ticket": d.ticket, "position_id": d.position_id, "time": self.ts(d.time), "type": d.type,
                 "entry": d.entry, "symbol": self.scanner_symbol(d.symbol), "mt5_symbol": d.symbol, "volume": d.volume,
                 "price": d.price, "profit": d.profit, "commission": d.commission, "swap": d.swap,
                 "fee": getattr(d, "fee", 0.0), "comment": d.comment} for d in ds]


class Mt5Source(BarSource):
    """[OWNER cfg-0.5.0] Broker bars from the MetaTrader 5 terminal (currency pairs only)."""
    name = "mt5"

    def __init__(self, client: Mt5Client, cfg: dict):
        self.client = client
        self.cfg = cfg["bars"]

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        n = {TF_1H: self.cfg["n_bars_1h"], TF_D: self.cfg["n_bars_daily"]}.get(tf, self.cfg["n_bars_native"])
        return self.client.rates(self.client.mt5_symbol(inst.symbol), tf, n)

    def provider(self, inst: Instrument) -> str:
        return f"MT5:{self.client.mt5_symbol(inst.symbol)}"


class RoutedSource(BarSource):
    """[OWNER cfg-0.5.0] Currency pairs from MT5, everything else (CFDs, futures) from TradingView.
    Before a pair's first MT5 fetch its Daily and 1H history depth is checked; a pair the broker cannot
    serve in full (fetch error or too few bars) comes entirely from TradingView, so one pair never mixes
    feeds. Its provider label says so."""
    name = "mt5 (FX) + tradingview"

    def __init__(self, fx: BarSource, other: BarSource, min_bars: int = 250):
        self.fx, self.other = fx, other
        # Daily needs min_bars completed bars; 1H must rebuild min_bars 4H bars (4 per bar plus weekend gaps)
        self.need = {TF_D: min_bars + 5, TF_1H: int(min_bars * 4.5)}
        self.fallback: dict[str, str] = {}
        self._cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _preflight(self, inst: Instrument) -> None:
        for tf, n in self.need.items():
            try:
                df = self.fx.get(inst, tf)
            except Exception as exc:  # noqa: BLE001
                self.fallback[inst.symbol] = f"{tf} fetch failed: {exc}"
                break
            if len(df) < n:
                self.fallback[inst.symbol] = f"broker history too short: {len(df)} {tf} bars (need {n})"
                break
            self._cache[(inst.symbol, tf)] = df
        if inst.symbol in self.fallback:
            LOG.warning("%s: MT5 %s; using TradingView for this pair", inst.symbol, self.fallback[inst.symbol])

    def get(self, inst: Instrument, tf: str) -> pd.DataFrame:
        if inst.asset != "fx":
            return self.other.get(inst, tf)
        if inst.symbol not in self.fallback and not any(k[0] == inst.symbol for k in self._cache):
            self._preflight(inst)
        if inst.symbol in self.fallback:
            return self.other.get(inst, tf)
        if (inst.symbol, tf) in self._cache:
            return self._cache.pop((inst.symbol, tf))
        return self.fx.get(inst, tf)

    def provider(self, inst: Instrument) -> str:
        if inst.asset != "fx":
            return self.other.provider(inst)
        if inst.symbol in self.fallback:
            return f"{self.other.provider(inst)} (MT5 not used: {self.fallback[inst.symbol]})"
        return self.fx.provider(inst)


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
    """[v1.0 Z01] max(two ticks, 0.10 x 4H ATR14 at setup recognition). Since cfg-0.2.0 this is the
    tolerance for fib (C3) and trend line (C6) checks, and the fallback for a grid with no fixed zone."""
    a = atr4_value if np.isfinite(atr4_value) else 0.0
    return max(int(fcfg["zone_min_ticks"]) * inst.tick, float(fcfg["zone_atr_fraction"]) * a)


def psych_zone_half_width(inst: Instrument, atr_hw: float) -> tuple[float, str]:
    """[OWNER cfg-0.2.0] Key levels are zones, not lines. Half-width around every major and mid grid
    level: the fixed width from config (FX +/-15 pips, gold +/-$20), else the ATR width. Returns (half_width, source) with source 'fixed' or 'atr'."""
    if inst.psych_zone_hw is not None:
        return inst.psych_zone_hw, "fixed"
    return atr_hw, "atr"


def zone_wick_tests(df2: pd.DataFrame, direction: str, zone_low: float, zone_high: float,
                    lookback: int) -> int:
    """[OWNER cfg-0.2.0] Wick principle. Count completed 2H candles in the lookback whose wick reaches into the zone and is rejected in
    the trade direction. Long: lower wick touches the zone, the candle closes at or above the zone floor,
    and the lower wick is at least the body and the upper wick. Short mirrors. Early warning only."""
    sub = df2.iloc[-lookback:] if lookback > 0 else df2.iloc[0:0]
    n = 0
    for o, h, l, c in sub[["open", "high", "low", "close"]].itertuples(index=False):
        body = abs(c - o)
        lower = min(o, c) - l
        upper = h - max(o, c)
        if direction == LONG:
            hit = l <= zone_high and c >= zone_low and lower > 0 and lower >= body and lower >= upper
        else:
            hit = h >= zone_low and c <= zone_high and upper > 0 and upper >= body and upper >= lower
        n += int(hit)
    return n


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

def _prior_trend(c: np.ndarray, last: int, n: int) -> str:
    """Direction of 2H closes over the n bars ending at index last: 'down', 'up' or ''."""
    if n <= 0 or last - n < 0:
        return ""
    if c[last] < c[last - n]:
        return "down"
    if c[last] > c[last - n]:
        return "up"
    return ""


def _touches_zone(h: np.ndarray, l: np.ndarray, idx: range, zone_low: float, zone_high: float) -> bool:
    return any(l[i] <= zone_high and h[i] >= zone_low for i in idx)


def _pattern_at(o, h, l, c, t: int, a: float, direction: str, ccfg: dict) -> tuple[str, int]:
    """[OWNER cfg-0.3.0] Reversal pattern from the candlestick guide that completes on bar t, for the trade direction.
    Returns (name, first bar index) or ('', t). Body colour does not matter for the pin shapes;
    the trend before the pattern does (hammer vs hanging man, inverted hammer vs shooting star)."""
    trend_n = int(ccfg.get("prior_trend_bars", 6))
    pin_mult = float(ccfg.get("pin_wick_body_mult", 2.0))
    small_wick = float(ccfg["rejection_close_top"])        # opposite wick at most this share of range
    min_rng = float(ccfg["rejection_range_atr"]) * a
    strong_body = float(ccfg["engulf_body_atr"]) * a
    want = "down" if direction == LONG else "up"

    def parts(i):
        body = abs(c[i] - o[i])
        return body, h[i] - l[i], min(o[i], c[i]) - l[i], h[i] - max(o[i], c[i])

    # three candles: morning star / evening star
    if t >= 2 and _prior_trend(c, t - 2, trend_n) == want:
        b1, _, _, _ = parts(t - 2)
        b2, _, _, _ = parts(t - 1)
        small = b2 <= float(ccfg.get("star_body_frac", 0.3)) * b1
        mid1 = (o[t - 2] + c[t - 2]) / 2
        if direction == LONG and c[t - 2] < o[t - 2] and b1 >= strong_body and small and c[t] > o[t] and c[t] > mid1:
            return "morning star", t - 2
        if direction == SHORT and c[t - 2] > o[t - 2] and b1 >= strong_body and small and c[t] < o[t] and c[t] < mid1:
            return "evening star", t - 2
    if t >= 1:
        body, _, _, _ = parts(t)
        # two candles: engulfing. The engulfing candle itself must wick into the zone (first index t);
        # its close may finish beyond the zone, which is the signal.
        if direction == LONG and (c[t - 1] < o[t - 1] and c[t] > o[t] and o[t] <= c[t - 1] and c[t] >= o[t - 1]
                                  and body >= strong_body):
            return "bullish engulfing", t
        if direction == SHORT and (c[t - 1] > o[t - 1] and c[t] < o[t] and o[t] >= c[t - 1] and c[t] <= o[t - 1]
                                   and body >= strong_body):
            return "bearish engulfing", t
        # two candles: tweezer bottom / top (matching extremes after a trend)
        tol = float(ccfg.get("tweezer_tol_atr", 0.05)) * a
        if _prior_trend(c, t - 1, trend_n) == want:
            if direction == LONG and c[t - 1] < o[t - 1] and c[t] > o[t] and abs(l[t] - l[t - 1]) <= tol:
                return "tweezer bottom", t - 1
            if direction == SHORT and c[t - 1] > o[t - 1] and c[t] < o[t] and abs(h[t] - h[t - 1]) <= tol:
                return "tweezer top", t - 1
    # single candle pins, judged against the trend before them
    body, rng, lower, upper = parts(t)
    if rng >= min_rng and rng > 0 and _prior_trend(c, t - 1, trend_n) == want:
        long_lower = lower >= pin_mult * body and upper <= small_wick * rng
        long_upper = upper >= pin_mult * body and lower <= small_wick * rng
        if direction == LONG and long_lower:
            return "hammer", t
        if direction == LONG and long_upper:
            return "inverted hammer", t
        if direction == SHORT and long_upper:
            return "shooting star", t
        if direction == SHORT and long_lower:
            return "hanging man", t
    return "", t


def _is_marubozu(o, h, l, c, t: int, a: float, direction: str, ccfg: dict) -> bool:
    body, rng = abs(c[t] - o[t]), h[t] - l[t]
    if rng <= 0 or body < float(ccfg["engulf_body_atr"]) * a:
        return False
    if (c[t] > o[t]) != (direction == LONG):
        return False
    return (rng - body) <= float(ccfg.get("marubozu_wick_max", 0.10)) * rng


def candle_signal(df2: pd.DataFrame, atr2: np.ndarray, inst: Instrument, direction: str, ccfg: dict,
                  zone_low: float, zone_high: float) -> str:
    """[OWNER cfg-0.3.0, C4] Reversal pattern from the candlestick guide, closed on the latest completed bar,
    counted only when the pattern traded inside the key level zone. Also counts a directional marubozu
    that closes right after a zone reversal pattern (the guide's confirmation candle). '' if none."""
    if len(df2) < 2:
        return ""
    o, h, l, c = (df2[x].values for x in ("open", "high", "low", "close"))
    t = len(df2) - 1
    a = atr2[t]
    if not np.isfinite(a) or a <= 0:
        return ""
    name, first = _pattern_at(o, h, l, c, t, a, direction, ccfg)
    if name and _touches_zone(h, l, range(first, t + 1), zone_low, zone_high):
        return name
    if t >= 2 and _is_marubozu(o, h, l, c, t, a, direction, ccfg) and np.isfinite(atr2[t - 1]) and atr2[t - 1] > 0:
        prev, pfirst = _pattern_at(o, h, l, c, t - 1, atr2[t - 1], direction, ccfg)
        if prev and _touches_zone(h, l, range(pfirst, t), zone_low, zone_high):
            return f"{prev} + marubozu"
    return ""


def chart_pattern(df: pd.DataFrame, pivots: list[Pivot], direction: str, inst: Instrument, zone_hw: float,
                  cp: dict, tf: str) -> str:
    """[OWNER cfg-0.4.0, C4] Double bottom/top and (inverse) head and shoulders on Daily or 4H, confirmed by a
    candle CLOSE beyond the neckline (a wick does not count) within the last break_max_age_bars bars,
    with the latest close still beyond it. The reversal extreme (a bottom, or the head) must sit in a key
    level zone. Tolerances scale with the instrument's zone half-width. Returns '' if none."""
    t = len(df) - 1
    c = df["close"].values
    seq = alternating_upto(pivots, t)
    ext = "L" if direction == LONG else "H"
    idx = [i for i, q in enumerate(seq) if q.kind == ext]
    tol = float(cp["match_tol_zone_mult"][tf]) * zone_hw
    margin = float(cp["head_margin_zone_mult"][tf]) * zone_hw
    min_gap = int(cp["min_gap_bars"])
    max_age = int(cp["break_max_age_bars"])
    beyond = (lambda x, lvl: x > lvl) if direction == LONG else (lambda x, lvl: x < lvl)
    more_extreme = (lambda a, b: a < b) if direction == LONG else (lambda a, b: a > b)

    def confirmed(neck, last_k: int, extremes: list[float]) -> bool:
        worst = min(extremes) if direction == LONG else max(extremes)
        brk = None
        for k in range(last_k + 1, t + 1):
            if (c[k] < worst) if direction == LONG else (c[k] > worst):
                return False                       # closed through the pattern's extreme: void
            if brk is None and beyond(c[k], neck(k)):
                brk = k
        return brk is not None and t - brk < max_age and beyond(c[t], neck(t))

    def in_zone(price: float) -> bool:
        return in_psych_zone(inst, price, zone_hw)[0]

    # head and shoulders: shoulder, trough, head, trough, shoulder
    if len(idx) >= 3:
        s1, hd, s2 = (seq[i] for i in idx[-3:])
        t1, t2 = seq[idx[-2] - 1], seq[idx[-1] - 1]
        if (more_extreme(hd.price, s1.price) and more_extreme(hd.price, s2.price)
                and abs(hd.price - s1.price) >= margin and abs(hd.price - s2.price) >= margin
                and abs(s1.price - s2.price) <= tol and s2.k - s1.k >= min_gap and in_zone(hd.price)
                and t2.k > t1.k):
            slope = (t2.price - t1.price) / (t2.k - t1.k)
            if confirmed(lambda k: t1.price + slope * (k - t1.k), s2.k, [s1.price, hd.price, s2.price]):
                return "inverse head and shoulders" if direction == LONG else "head and shoulders"
    # double bottom / top
    if len(idx) >= 2:
        e1, e2 = seq[idx[-2]], seq[idx[-1]]
        neck = seq[idx[-1] - 1].price
        if (abs(e1.price - e2.price) <= tol and e2.k - e1.k >= min_gap
                and (in_zone(e1.price) or in_zone(e2.price))):
            if confirmed(lambda k: neck, e2.k, [e1.price, e2.price]):
                return "double bottom" if direction == LONG else "double top"
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
    if r["rule"] == "day_range":
        return day.month in r["months"] and int(r["from_day"]) <= day.day <= int(r["to_day"])
    return False


def roll_flag(inst: Instrument, dfd: pd.DataFrame, imp: Optional[Impulse], cfg: dict) -> str:
    if not inst.roll:
        return ""
    rules = cfg.get("roll_rules", {})
    n = int(rules.get("lookback_daily_bars", 10))
    days = [ts.date() for ts in dfd.index[-n:]]
    if imp is not None:
        a, b = pd.Timestamp(imp.a_time).date(), pd.Timestamp(imp.b_time).date()
        days += [a + timedelta(days=i) for i in range((b - a).days + 1)]
    hit = sorted({d for d in days if in_roll_window(d, inst.roll, rules)})
    return f"roll window {hit[0]}..{hit[-1]} (approx.)" if hit else ""


# =============================================================================
# Scoring one instrument
# =============================================================================

@dataclass
class Row:
    symbol: str
    group: str
    direction: str
    section: str = "not shown"     # qualified | developing | not shown
    rank: Optional[int] = None
    c1: bool = False
    c2: bool = False
    c3: bool = False
    c4: bool = False
    c5: bool = False
    c6: bool = False
    technical: int = 0
    cot_points: int = 0
    sentiment_points: int = 0
    total: int = 0
    ref_price: Optional[float] = None
    ref_time: str = ""
    zone_level: Optional[float] = None
    zone_kind: str = ""
    zone_half_width: Optional[float] = None
    zone_low: Optional[float] = None
    zone_high: Optional[float] = None
    zone_width_source: str = ""
    zone_wicks: int = 0
    daily_bias: str = ""
    bias_invalidation: Optional[float] = None
    structure_4h: str = ""
    impulse: Optional[dict] = None
    impulse_note: str = ""
    ladder_in_zone: dict = field(default_factory=dict)
    candle: str = ""
    c4_tf: str = ""              # timeframe of the reversal confirmation: D, 4H or 2H
    c4_tf_rank: int = 0          # D 3, 4H 2, 2H 1: higher timeframe ranks first on equal totals
    trendline: str = ""
    cot: dict = field(default_factory=dict)
    sentiment: dict = field(default_factory=dict)
    calendar: dict = field(default_factory=dict)
    short_test: Optional[bool] = None
    same_underlying: bool = False
    roll: str = ""
    flags: list = field(default_factory=list)
    reason: str = ""


@dataclass
class InstrumentResult:
    inst: Instrument
    rows: list[Row]
    errors: list[str]
    provider: str
    last_bar: dict
    c2_reject: bool = False


def setup_section(c1: bool, c2: bool, c4: bool, technical: int, require_close_in_zone: bool = False) -> str:
    """[OWNER cfg-0.4.0] Qualified: C1, a reversal confirmation closed in a key level zone (C4, 2H or higher),
    and at least 3 checks. Developing: C1 and price in the zone (C2) with 2+ checks, no reversal yet."""
    if c1 and c4 and technical >= 3 and (c2 or not require_close_in_zone):
        return "qualified"
    if c1 and c2 and technical >= 2:
        return "developing"
    return "not shown"


def score_instrument(inst: Instrument, bars: dict[str, pd.DataFrame], cfg: dict, cot_reading: CotReading,
                     sent: dict, cal: dict, sent_engine: SentimentEngine) -> list[Row]:
    f = cfg["features"]
    dfd, df4, df2 = bars[TF_D], bars[TF_4H], bars[TF_2H]
    w = int(f["pivot_width"])
    piv_d, piv_4 = find_pivots(dfd, w), find_pivots(df4, w)
    atr4 = atr(df4, int(f["atr_period"]))
    atr2 = atr(df2, int(f["atr_period"]))
    atrd = atr(dfd, int(f["atr_period"]))
    c2h = df2["close"].values
    e_fast, e_slow = (ema(c2h, int(n)) for n in f["ema_2h"])
    ed20, ed50 = (ema(dfd["close"].values, int(n)) for n in f["ema_daily"])
    bias = daily_bias(dfd, piv_d)
    s4 = structure(alternating_upto(piv_4, len(df4) - 1))
    sd_struct = structure(alternating_upto(piv_d, len(dfd) - 1))
    P = float(c2h[-1])
    rows = []
    for direction in (LONG, SHORT):
        r = Row(inst.symbol, inst.group, direction)
        r.ref_price, r.ref_time = P, str(df2.index[-1] + TF_DELTA[TF_2H])
        r.daily_bias, r.bias_invalidation = bias.bias or "neutral", bias.invalidation
        r.structure_4h = s4 or "mixed"
        if bias.invalidated_on_last_bar:
            r.flags.append("Daily bias invalidated on latest Daily bar")
        # C1 [v1.0 D01 + D02]
        r.c1 = bias.bias == direction and s4 == direction
        # Impulse [v1.0 I01]
        imp, note = select_impulse(df4, piv_4, atr4, direction, f)
        r.impulse_note = note
        hw_atr = imp.atr_at_recognition if imp else atr4[-1]
        hw = zone_half_width(inst, hw_atr, f)   # fib (C3) and trend line (C6) tolerance
        # [OWNER cfg-0.2.0] Key level zones: fixed width from config, ATR width where none is set
        zhw, zsrc = psych_zone_half_width(inst, hw)
        r.zone_half_width, r.zone_width_source = zhw, zsrc
        if zsrc == "atr":
            r.flags.append("zone width not set: ATR width used")
        # C2 [v1.0 Z01 / C2] at the latest completed 2H close  [Add 6.1]
        ok, lvl, kind = in_psych_zone(inst, P, zhw)
        r.c2, r.zone_level, r.zone_kind = ok, lvl, kind
        r.zone_low, r.zone_high = lvl - zhw, lvl + zhw
        # [OWNER cfg-0.2.0] Wick principle: wicks testing the zone are an early alert, never a scored check
        wcfg = f.get("zone_wicks", {})
        r.zone_wicks = zone_wick_tests(df2, direction, r.zone_low, r.zone_high, int(wcfg.get("lookback_2h_bars", 6)))
        if r.zone_wicks >= int(wcfg.get("min_count", 2)):
            r.flags.append(f"zone tested: {r.zone_wicks} wicks")
        # C3 Fibonacci
        if imp:
            r.impulse = {"A": imp.a_price, "B": imp.b_price, "A_time": imp.a_time, "B_time": imp.b_time,
                         "span": imp.span, "efficiency": round(imp.efficiency, 3),
                         **{f"fib_{int(round(x * 1000))}": imp.fib(float(x)) for x in f["fib_levels"]}}
            fibs = [imp.fib(float(x)) for x in f["fib_levels"]]
            r.c3 = any(abs(P - fv) <= hw for fv in fibs)
            for x, fv in zip(f["fib_levels"], fibs):
                r.ladder_in_zone[f"{float(x) * 100:.1f}"] = in_psych_zone(inst, fv, zhw)[0]
        # C4 candle, C5 EMA, C6 trend line
        # [OWNER cfg-0.4.0] C4: reversal that started at the zone and closed on 2H or higher; Daily beats 4H beats 2H
        cp = f["chart_patterns"]
        for tf, df_tf, atr_tf, piv_tf, tf_rank in ((TF_D, dfd, atrd, piv_d, 3), (TF_4H, df4, atr4, piv_4, 2),
                                                  (TF_2H, df2, atr2, None, 1)):
            name = chart_pattern(df_tf, piv_tf, direction, inst, zhw, cp, tf) if tf in cp["timeframes"] else ""
            name = name or candle_signal(df_tf, atr_tf, inst, direction, f["candle"], r.zone_low, r.zone_high)
            if name:
                r.candle, r.c4_tf, r.c4_tf_rank = f"{tf} {name}", tf, tf_rank
                break
        r.c4 = bool(r.candle)
        if np.isfinite(e_fast[-1]) and np.isfinite(e_slow[-1]):
            r.c5 = e_fast[-1] > e_slow[-1] if direction == LONG else e_fast[-1] < e_slow[-1]
        r.c6, r.trendline = trendline_signal(df4, piv_4, df2, direction, hw)
        r.technical = int(sum([r.c1, r.c2, r.c3, r.c4, r.c5, r.c6]))
        # Overlays [Add 6.2, 6.3]: rank, never qualify
        r.cot_points = cot_points(cot_reading, direction, cfg["cot"])
        cr = asdict(cot_reading)
        if cr["index"] is not None:
            cr["index_for_direction"] = round(cr["index"] if direction == LONG else 100 - cr["index"], 1)
        cr["crowding"] = ("" if cot_reading.index is None else
                          "crowded long" if cot_reading.index >= cfg["cot"]["crowded_long"] else
                          "crowded short" if cot_reading.index <= cfg["cot"]["crowded_short"] else "")
        r.cot = cr
        r.sentiment_points = sent_engine.points(sent.get("S"), sent.get("thin", True), direction)
        r.sentiment = dict(sent)
        r.calendar = dict(cal)
        r.total = r.technical + r.cot_points + r.sentiment_points
        # Section [Add 8]
        r.section = setup_section(r.c1, r.c2, r.c4, r.technical,
                                  bool(cfg.get("qualification", {}).get("require_close_in_zone", False)))
        # v1.0 short experiment flag for gold and S&P [Add 3]
        if inst.short_test and direction == SHORT:
            dclose = float(dfd["close"].values[-1])
            r.short_test = bool(sd_struct == SHORT and s4 == SHORT and np.isfinite(ed20[-1]) and np.isfinite(ed50[-1])
                                and dclose < ed20[-1] and dclose < ed50[-1] and r.technical >= 4)
        r.roll = roll_flag(inst, dfd, imp, cfg)
        if r.roll:
            r.flags.append(r.roll)
        if cot_reading.status != "ok":
            r.flags.append("COT unavailable" if cot_reading.status == "unavailable" else "COT stale")
        if sent.get("thin", True):
            r.flags.append("thin news")
        if cal.get("event_risk"):
            r.flags.append("event risk")
        rows.append(r)
    return rows


def rejection_reason(rows: list[Row]) -> str:
    bias = rows[0].daily_bias
    if bias == "neutral":
        return "Daily bias neutral"
    r = next(x for x in rows if x.direction == bias)
    if r.structure_4h != bias:
        return "4H disagrees"
    if not r.c2 and not r.c4:
        return "outside key level zone"
    if not r.c4:
        return "in zone, no reversal candle closed yet"
    return f"technical score {r.technical}"


def rank(rows: list[Row], top_n: int = 5) -> list[Row]:
    """[Add 8] Qualified before developing; total desc; ties: C4 timeframe (D > 4H > 2H), technical,
    COT points, symbol. Never pad."""
    cands = [r for r in rows if r.section in ("qualified", "developing")]
    cands.sort(key=lambda r: (0 if r.section == "qualified" else 1, -r.total, -r.c4_tf_rank, -r.technical,
                              -r.cot_points, r.symbol))
    top = cands[:top_n]
    for i, r in enumerate(top, 1):
        r.rank = i
    return top


def mark_same_underlying(top: list[Row], universe: dict[str, Instrument]) -> None:
    groups: dict[str, list[Row]] = {}
    for r in top:
        u = universe[r.symbol].underlying
        if u:
            groups.setdefault(u, []).append(r)
    for g in groups.values():
        if len(g) > 1:
            for r in g:
                r.same_underlying = True


# =============================================================================
# Output
# =============================================================================

def _fmt(v, nd=5):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "–"
    if isinstance(v, float):
        return f"{v:.{nd}g}"
    return str(v)


def _ck(b: bool) -> str:
    return '<span class="ok">✓</span>' if b else '<span class="no">·</span>'


def render_broker(broker: Optional[dict]) -> str:
    """[OWNER cfg-0.5.0] Dashboard sections: MT5 broker prices for the currency pairs and open positions."""
    e = html.escape
    if not broker:
        return ""
    out = "<h2>Open positions (MT5)</h2>"
    if not broker.get("connected"):
        return out + f"<div class='banner warn'>MT5 unavailable: {e(broker.get('status', ''))}</div>"
    pos = broker.get("positions", [])
    if not pos:
        out += "<p class='muted'>No open positions.</p>"
    else:
        out += ("<div class='wrap'><table><tr><th>Ticket</th><th>Instrument</th><th>Dir</th><th class='num'>Lots</th>"
                "<th>Opened (UTC)</th><th class='num'>Entry</th><th class='num'>Now</th><th class='num'>SL</th>"
                "<th class='num'>TP</th><th class='num'>Swap</th><th class='num'>P/L</th><th>Comment</th></tr>")
        for p in pos:
            pl_cls = "long" if p["profit"] >= 0 else "short"
            out += (f"<tr><td>{p['ticket']}</td><td><b>{e(p['symbol'])}</b></td><td class='{p['direction']}'>{p['direction']}</td>"
                    f"<td class='num'>{p['volume']}</td><td>{e(p['open_utc'])}</td><td class='num'>{_fmt(p['open_price'], 6)}</td>"
                    f"<td class='num'>{_fmt(p['price'], 6)}</td><td class='num'>{_fmt(p['sl'], 6)}</td><td class='num'>{_fmt(p['tp'], 6)}</td>"
                    f"<td class='num'>{p['swap']:.2f}</td><td class='num {pl_cls}'>{p['profit']:.2f}</td><td>{e(p['comment'])}</td></tr>")
        out += "</table></div>"
    prices = broker.get("prices", [])
    live = [p for p in prices if p["bid"] is not None]
    out += (f"<details><summary>Broker prices (MT5), {len(live)} of {len(prices)} currency pairs quoting</summary>"
            "<div class='wrap'><table><tr><th>Pair</th><th class='num'>Bid</th><th class='num'>Ask</th>"
            "<th class='num'>Spread (pips)</th><th>Tick time (UTC)</th></tr>")
    for p in prices:
        if p["bid"] is None:
            out += f"<tr><td>{e(p['symbol'])}</td><td colspan='4' class='muted'>no quote from MT5</td></tr>"
            continue
        out += (f"<tr><td>{e(p['symbol'])}</td><td class='num'>{_fmt(p['bid'], 6)}</td><td class='num'>{_fmt(p['ask'], 6)}</td>"
                f"<td class='num'>{p['spread_pips']}</td><td class='muted'>{e(p['time_utc'])}</td></tr>")
    return out + "</table></div></details>"


def build_news_view(cfg: dict, base: Path, asof: pd.Timestamp, events: list[dict], universe: list[Instrument],
                    client: Optional[Mt5Client], demo: bool) -> dict:
    """[OWNER cfg-0.6.0] Upcoming red-folder events with the instruments they affect, and how recent
    red events moved each currency (MT5 5 minute bars). Never raises: news must not stop a scan."""
    ccfg = cfg.get("calendar", {})
    impacts = tuple(ccfg.get("impacts_tracked", ["High"]))
    view: dict = {"impacts": impacts, "upcoming": pd.DataFrame(), "events": pd.DataFrame(), "currencies": pd.DataFrame(),
                  "status": ""}
    try:
        snap = base / cfg["paths"]["data_dir"] / "calendar_snapshots"
        arch = newsmod.parse_events(events, asof) if demo else newsmod.load_archive(snap, events, asof)
        amap = newsmod.affected_map([(i.symbol, i.calendar_currencies) for i in universe])
        up = newsmod.upcoming(arch, asof, float(ccfg.get("upcoming_hours", 192)), impacts).copy()
        up["affects"] = up["currency"].map(lambda c: amap.get(c, []))
        view["upcoming"] = up
        log = base / cfg["paths"]["log_dir"] / "news_reactions.csv"
        if client and client.mt5 and not demo:
            fx = [i.symbol for i in universe if i.asset == "fx"]
            n = newsmod.update_reactions(arch, fx, lambda p: client.rates(client.mt5_symbol(p), "M5", 3500), log, asof,
                                         float(ccfg.get("reaction_lookback_days", 10)), impacts)
            view["status"] = f"{n} new pair reactions measured"
        else:
            view["status"] = "reactions need MT5"
        view["events"], view["currencies"] = newsmod.summarize_reactions(log)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("news view failed: %s", exc)
        view["status"] = f"failed: {exc}"
    return view


def render_news(view: Optional[dict], disp_tz: ZoneInfo) -> str:
    e = html.escape
    if not view:
        return ""
    out = ("<h2>Red-folder news (Forex Factory)</h2><p class='muted'>Source: forexfactory.com/calendar (High impact = red "
           "folder), read from its weekly export, which covers this week only and has no actual values. Times in "
           f"{e(str(disp_tz))}. v1.0 blocks new entries within 30 minutes of a red event for either currency.</p>")
    up = view["upcoming"]
    if up.empty:
        out += "<p class='muted'>No red-folder events left this week.</p>"
    else:
        out += ("<div class='wrap'><table><tr><th>When</th><th class='num'>In</th><th>Currency</th><th>Event</th>"
                "<th>Forecast</th><th>Previous</th><th>Affects</th></tr>")
        now = pd.Timestamp.now(tz=UTC)
        for _, r in up.iterrows():
            hrs = (r["event_time"] - now).total_seconds() / 3600
            cls = "warn" if 0 <= hrs <= 24 else "muted"
            out += (f"<tr><td>{e(r['event_time'].tz_convert(disp_tz).strftime('%a %b %d %I:%M %p'))}</td>"
                    f"<td class='num {cls}'>{'now' if hrs < 0 else f'{hrs:.1f}h'}</td><td><b>{e(r['currency'])}</b></td>"
                    f"<td>{e(r['title'])}</td><td>{e(r['forecast'] or '–')}</td><td>{e(r['previous'] or '–')}</td>"
                    f"<td class='wrapc muted'>{e(', '.join(r['affects']))}</td></tr>")
        out += "</table></div>"
    ev, cur = view["events"], view["currencies"]
    out += (f"<h2>How recent red news moved currencies</h2><p class='muted'>Currency move = average strength change "
            f"of the currency across its tracked pairs (base +, quote −) from the last 5-minute close before the "
            f"release. MT5 Forex.com bars. {e(view.get('status', ''))}. Full log: logs/news_reactions.csv</p>")
    if ev.empty:
        out += "<p class='muted'>No reactions measured yet. Each red event is measured once its 4-hour window has closed.</p>"
        return out
    if not cur.empty:
        out += ("<div class='wrap'><table><tr><th>Currency</th><th class='num'>Red events</th><th class='num'>Avg move 1h</th>"
                "<th class='num'>Avg move 4h</th><th>Last event (UTC)</th></tr>")
        for _, r in cur.iterrows():
            out += (f"<tr><td><b>{e(r['currency'])}</b></td><td class='num'>{int(r['events'])}</td>"
                    f"<td class='num'>{r['avg_abs_1h']:.2f}%</td><td class='num'>{r['avg_abs_4h']:.2f}%</td>"
                    f"<td class='muted'>{e(str(r['last_event']))}</td></tr>")
        out += "</table></div>"

    def mv(v) -> str:
        return f"<span class='{'long' if v > 0 else 'short' if v < 0 else 'muted'}'>{v:+.2f}%</span>"
    out += ("<details open><summary>Per event (newest first, last 30)</summary><div class='wrap'><table><tr><th>Time (UTC)</th>"
            "<th>Currency</th><th>Event</th><th>Fcst / prev</th><th class='num'>15m</th><th class='num'>1h</th>"
            "<th class='num'>4h</th><th class='num'>Pairs agree (1h)</th><th>Biggest pair move (1h)</th></tr>")
    for _, r in ev.head(30).iterrows():
        fp = " / ".join(str(x) if isinstance(x, str) and x else "–" for x in (r["forecast"], r["previous"]))
        out += (f"<tr><td>{e(str(r['event_time']))}</td><td><b>{e(r['currency'])}</b></td><td>{e(r['title'])}</td>"
                f"<td class='muted'>{e(fp)}</td><td class='num'>{mv(r['move_15m'])}</td><td class='num'>{mv(r['move_1h'])}</td>"
                f"<td class='num'>{mv(r['move_4h'])}</td><td class='num'>{int(r['agree_1h'])} of {int(r['pairs'])}</td>"
                f"<td>{e(r['biggest_1h'])}</td></tr>")
    return out + "</table></div></details>"


_CSS = """
    :root{--bg:#fff;--fg:#1b1f24;--muted:#5b6470;--line:#e3e6ea;--card:#f6f8fa;--good:#1a7f37;--warn:#9a6700;--bad:#cf222e;--acc:#0b5cad}
    @media (prefers-color-scheme: dark){:root{--bg:#0f1216;--fg:#e6e9ee;--muted:#9aa4b2;--line:#2a3038;--card:#161b22;--good:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}}
    *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
    main{max-width:1280px;margin:0 auto;padding:20px 16px 40px} h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
    .muted{color:var(--muted)} .meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin:8px 0 14px;font-size:13px}
    .wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
    table{border-collapse:collapse;width:100%;font-size:13px} th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
    th{background:var(--card);font-weight:600;position:sticky;top:0} td.wrapc{white-space:normal;min-width:260px}
    .ok{color:var(--good);font-weight:700} .no{color:var(--muted)} .tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;border:1px solid var(--line);margin:1px 3px 1px 0}
    .q{background:color-mix(in srgb,var(--good) 15%,transparent)} .d{background:color-mix(in srgb,var(--warn) 15%,transparent)}
    .long{color:var(--good);font-weight:600} .short{color:var(--bad);font-weight:600} .num{text-align:right;font-variant-numeric:tabular-nums}
    .warn{color:var(--warn)} details{margin-top:10px} summary{cursor:pointer;color:var(--acc)} ul{margin:4px 0;padding-left:18px}
    .banner{padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card);margin:10px 0}
    nav{display:flex;gap:16px;margin:0 0 12px;font-size:13px} nav a{color:var(--acc)}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0}
    .card{border:1px solid var(--line);border-radius:8px;background:var(--card);padding:10px 12px} .card b{display:block;font-size:18px;font-variant-numeric:tabular-nums}
    """


def render_html(meta: dict, top: list[Row], all_rows: list[Row], footer: list[dict], broker: Optional[dict] = None,
                news_view: Optional[dict] = None) -> str:
    e = html.escape
    css = _CSS
    fr ="".join(f"<span><b>{e(k)}:</b> {e(str(v))}</span>" for k, v in meta["freshness"].items())
    head = ("<nav><b>Scanner dashboard</b><a href='journal.html'>Trading journal and balance</a></nav>"
            f"<h1>Daily Instrument Scanner · {e(meta['run_type'])}</h1>"
            f"<div class='muted'>{e(meta['asof_utc'])} UTC · {e(meta['asof_local'])} · addendum {e(meta['addendum_version'])}"
            f" · config {e(meta['config_version'])} · code {e(meta['code_version'])}</div>"
            f"<div class='meta muted'>{fr}</div>")
    if meta.get("demo"):
        head += "<div class='banner warn'><b>DEMO DATA.</b> Synthetic prices, COT, calendar and headlines. Not a market view.</div>"
    head += ("<div class='banner'>Watchlist only: no orders, no trade levels, no sizing. Overlays rank setups; "
             "they never qualify them. Scores are research outputs, not a validated edge.</div>")
    n = len(top)
    body = f"<h2>Top {n} setups</h2>"
    if n < 5:
        body += f"<p class='muted'>{n} setup(s) met the qualified or developing definition; the list is not padded.</p>"
    if n:
        body += "<div class='wrap'><table><tr><th>#</th><th>Section</th><th>Instrument</th><th>Group</th><th>Dir</th>" \
                "<th>C1</th><th>C2</th><th>C3</th><th>C4</th><th>C5</th><th>C6</th><th class='num'>Tech</th>" \
                "<th class='num'>COT</th><th class='num'>Sent</th><th class='num'>Total</th><th>Zone</th><th>Ladder in zone</th>" \
                "<th>COT detail</th><th>News</th><th>Calendar</th><th>Flags</th></tr>"
        for r in top:
            lad = " ".join(f"{k}{'✓' if v else '·'}" for k, v in r.ladder_in_zone.items()) or "no impulse"
            zone_txt = (f"{_fmt(r.zone_level, 6)} {e(r.zone_kind)}<br><span class='muted'>{_fmt(r.zone_low, 6)} to "
                        f"{_fmt(r.zone_high, 6)}{' (ATR)' if r.zone_width_source == 'atr' else ''} · wicks {r.zone_wicks}</span>"
                        + (f"<br>reversal: {e(r.candle)}" if r.candle else ""))
            c = r.cot
            cot_txt = ("unavailable" if c.get("index") is None else
                       f"idx {c['index']:.0f} · Δnet {_fmt(c.get('weekly_change'), 4)} {c.get('crowding','')}<br>"
                       f"<span class='muted'>{e(c.get('family',''))} · pub {e(str(c.get('publication',''))[:10])} · {e(c.get('status',''))}</span>")
            s = r.sentiment
            news = f"S {_fmt(s.get('S'), 3)}{' · thin news' if s.get('thin') else ''}"
            if s.get("drivers"):
                news += "<ul>" + "".join(f"<li>{e(d['title'][:110])} <span class='muted'>({e(d['theme'] or '')}, {e(d['source'])})</span></li>"
                                         for d in s["drivers"]) + "</ul>"
            cal = r.calendar
            cal_txt = ("none this week" if not cal.get("event") else
                       f"{e(cal['currency'])} {e(cal['event'])}<br><span class='{'warn' if cal.get('event_risk') else 'muted'}'>in {cal['hours']}h</span>")
            flags = list(r.flags)
            if r.same_underlying:
                flags.insert(0, "same underlying")
            if r.short_test is not None:
                flags.append(f"v1.0 short test: {r.short_test}")
            cls = "q" if r.section == "qualified" else "d"
            body += (f"<tr><td>{r.rank}</td><td><span class='tag {cls}'>{e(r.section)}</span></td><td><b>{e(r.symbol)}</b><br>"
                     f"<span class='muted'>{_fmt(r.ref_price, 6)}</span></td><td>{e(r.group)}</td><td class='{r.direction}'>{r.direction}</td>"
                     + "".join(f"<td>{_ck(x)}</td>" for x in (r.c1, r.c2, r.c3, r.c4, r.c5, r.c6))
                     + f"<td class='num'>{r.technical}</td><td class='num'>{r.cot_points:+d}</td><td class='num'>{r.sentiment_points:+d}</td>"
                     f"<td class='num'><b>{r.total}</b></td><td>{zone_txt}</td><td>{e(lad)}</td><td>{cot_txt}</td><td class='wrapc'>{news}</td>"
                     f"<td>{cal_txt}</td><td class='wrapc'>{''.join(f'<span class=tag>{e(x)}</span>' for x in flags)}</td></tr>")
        body += "</table></div>"
    body += render_broker(broker)
    if news_view:
        body += render_news(news_view, ZoneInfo(news_view.get("tz", "America/Chicago")))
    # all scored candidates
    cands = sorted([r for r in all_rows if r.section != "not shown"], key=lambda r: (r.section != "qualified", -r.total))
    body += ("<p class='muted'>Qualified: C1 + a reversal closed in a key level zone (C4, 2H or higher) + at least 3 of C1-C6. "
             "Developing: C1 + price in the zone (C2) + 2 or more checks, reversal not closed yet.</p>")
    body += f"<details><summary>All qualified and developing setups ({len(cands)})</summary><div class='wrap'><table>" \
            "<tr><th>Instrument</th><th>Dir</th><th>Section</th><th>C1</th><th>C2</th><th>C3</th><th>C4</th><th>C5</th><th>C6</th>" \
            "<th class='num'>Tech</th><th class='num'>COT</th><th class='num'>Sent</th><th class='num'>Total</th><th>Notes</th></tr>"
    for r in cands:
        body += (f"<tr><td>{e(r.symbol)}</td><td class='{r.direction}'>{r.direction}</td><td>{e(r.section)}</td>"
                 + "".join(f"<td>{_ck(x)}</td>" for x in (r.c1, r.c2, r.c3, r.c4, r.c5, r.c6))
                 + f"<td class='num'>{r.technical}</td>"
                 f"<td class='num'>{r.cot_points:+d}</td><td class='num'>{r.sentiment_points:+d}</td><td class='num'>{r.total}</td>"
                 f"<td class='wrapc'>{e('; '.join(r.flags))}</td></tr>")
    body += "</table></div></details>"
    # footer
    body += "<h2>Every instrument scanned</h2><div class='wrap'><table><tr><th>Instrument</th><th>Group</th><th>Daily bias</th><th>4H</th><th>Status / rejection reason</th><th>Bars</th></tr>"
    for f in footer:
        body += (f"<tr><td>{e(f['symbol'])}</td><td>{e(f['group'])}</td><td>{e(f['bias'])}</td><td>{e(f['h4'])}</td>"
                 f"<td class='wrapc'>{e(f['status'])}</td><td class='muted'>{e(f['provider'])}</td></tr>")
    body += "</table></div>"
    body += ("<p class='muted' style='margin-top:18px'>Rules: Scanner Addendum v0.1 on Trading Algorithm Specification v1.0. "
             "C2 and C3 are tested at the latest completed 2H close. Key levels are zones (FX +/-15 pips around every major "
             "and mid level); wicks counts recent 2H wick rejections inside the zone, an early alert only. "
             "C4 counts a candlestick reversal pattern only when it forms inside the key level zone. TradingView and Forex Factory access are unofficial and may stop "
             "without notice. FinBERT tone on FX and commodity headlines is untested.</p>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Scanner {e(meta['asof_utc'])}</title><style>{css}</style></head><body><main>{head}{body}</main></body></html>")


def write_outputs(out_dir: Path, meta: dict, top: list[Row], all_rows: list[Row], footer: list[dict],
                  broker: Optional[dict] = None, news_view: Optional[dict] = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"scan_{meta['stamp']}_{meta['run_type'].replace(' ', '')}"
    paths = {"html": out_dir / f"{stem}.html", "json": out_dir / f"{stem}.json", "csv": out_dir / f"{stem}.csv"}
    paths["html"].write_text(render_html(meta, top, all_rows, footer, broker, news_view), encoding="utf-8")
    payload = {"meta": meta, "top": [asdict(r) for r in top], "rows": [asdict(r) for r in all_rows], "instruments": footer}
    if broker:
        payload["broker"] = {k: v for k, v in broker.items() if k != "account"}
    paths["json"].write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    flat_keys = ["symbol", "group", "direction", "section", "rank", "c1", "c2", "c3", "c4", "c5", "c6", "technical",
                 "cot_points", "sentiment_points", "total", "ref_price", "ref_time", "zone_level", "zone_kind",
                 "zone_half_width", "zone_low", "zone_high", "zone_width_source", "zone_wicks", "daily_bias", "bias_invalidation", "structure_4h", "candle", "c4_tf", "trendline",
                 "impulse_note", "short_test", "same_underlying", "roll"]
    with open(paths["csv"], "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["run_utc", "run_type", "config_version"] + flat_keys + ["cot_index", "cot_status", "sentiment_S",
                                                                         "thin_news", "next_event", "event_risk", "flags"])
        for r in all_rows:
            d = asdict(r)
            wr.writerow([meta["asof_utc"], meta["run_type"], meta["config_version"]] + [d[k] for k in flat_keys]
                        + [r.cot.get("index"), r.cot.get("status"), r.sentiment.get("S"), r.sentiment.get("thin"),
                           r.calendar.get("event"), r.calendar.get("event_risk"), "; ".join(r.flags)])
    return paths


def append_c2_log(log_dir: Path, meta: dict, results: list[InstrumentResult]) -> None:
    """[Add 5] Log C2 rejection counts per instrument so a tighter grid can be tested on evidence."""
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "c2_rejections.csv"
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        if new:
            wr.writerow(["run_utc", "run_type", "config_version", "symbol", "c2", "ref_price", "zone_level", "zone_half_width", "distance"])
        for res in results:
            if not res.rows:
                continue
            r = res.rows[0]
            wr.writerow([meta["asof_utc"], meta["run_type"], meta["config_version"], r.symbol, int(r.c2), r.ref_price,
                         r.zone_level, r.zone_half_width, None if r.zone_level is None else abs(r.ref_price - r.zone_level)])


# =============================================================================
# Trading journal and balance page  [OWNER cfg-0.5.0]
# =============================================================================

DEAL_BUY, DEAL_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
JOURNAL_CONF_KEYS = ("section", "rank", "c1", "c2", "c3", "c4", "c5", "c6", "technical", "cot_points",
                     "sentiment_points", "total", "candle", "c4_tf", "zone_level", "zone_kind", "daily_bias",
                     "structure_4h", "reason", "flags")


def build_trades(deals: list[dict], positions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group MT5 deals by position into one journal line per trade. Returns (trades, cash movements).
    Open positions whose opening deal falls outside the history window still get a line."""
    by_pos: dict[int, list[dict]] = {}
    cash = []
    for d in deals:
        if d["type"] not in (DEAL_BUY, DEAL_SELL):
            cash.append(d)   # balance, credit, charges and other non trade deals
            continue
        by_pos.setdefault(d["position_id"], []).append(d)
    open_by_ticket = {p["ticket"]: p for p in positions}
    trades = []
    for pid, ds in by_pos.items():
        ds = sorted(ds, key=lambda d: d["time"])
        ins = [d for d in ds if d["entry"] in (DEAL_ENTRY_IN, DEAL_ENTRY_INOUT)]
        outs = [d for d in ds if d["entry"] in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)]
        if not ins:
            continue  # opened before the history window; covered below if still open
        vol_in = sum(d["volume"] for d in ins)
        vol_out = sum(d["volume"] for d in outs)
        t = {"position_id": pid, "symbol": ins[0]["symbol"], "mt5_symbol": ins[0]["mt5_symbol"],
             "direction": LONG if ins[0]["type"] == DEAL_BUY else SHORT, "volume": vol_in,
             "open_ts": ins[0]["time"], "entry_price": sum(d["price"] * d["volume"] for d in ins) / vol_in,
             "close_ts": outs[-1]["time"] if outs else None,
             "exit_price": sum(d["price"] * d["volume"] for d in outs) / vol_out if vol_out else None,
             "net": sum(d["profit"] + d["commission"] + d["swap"] + d["fee"] for d in ds),
             "status": "closed" if outs and vol_out >= vol_in - 1e-9 else ("partly closed" if outs else "open"),
             "comment": ins[0]["comment"]}
        if pid in open_by_ticket:
            t["net"] += open_by_ticket[pid]["profit"] + open_by_ticket[pid]["swap"]
            t["status"] = "open" if not outs else "partly closed"
        trades.append(t)
    seen = {t["position_id"] for t in trades}
    for p in positions:
        if p["ticket"] not in seen:
            trades.append({"position_id": p["ticket"], "symbol": p["symbol"], "mt5_symbol": p["mt5_symbol"],
                           "direction": p["direction"], "volume": p["volume"],
                           "open_ts": pd.Timestamp(p["open_utc"], tz=UTC), "entry_price": p["open_price"],
                           "close_ts": None, "exit_price": None, "net": p["profit"] + p["swap"],
                           "status": "open", "comment": p["comment"]})
    trades.sort(key=lambda t: t["open_ts"], reverse=True)
    return trades, cash


def load_scan_history(out_dir: Path) -> list[dict]:
    """Every live (non demo) scan written so far, oldest first, with its rows keyed by (symbol, direction)."""
    scans = []
    for p in sorted(out_dir.glob("scan_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("journal: cannot read %s: %s", p.name, exc)
            continue
        m = d.get("meta", {})
        if m.get("demo") or "asof_utc" not in m:
            continue
        rows = {(r["symbol"], r["direction"]): {k: r.get(k) for k in JOURNAL_CONF_KEYS} for r in d.get("rows", [])}
        scans.append({"asof": pd.Timestamp(m["asof_utc"], tz=UTC), "file": p.with_suffix(".html").name,
                      "run_type": m.get("run_type", ""), "config_version": m.get("config_version", ""), "rows": rows})
    scans.sort(key=lambda s: s["asof"])
    return scans


def confluences_before(scans: list[dict], symbol: str, direction: str, entry: pd.Timestamp) -> Optional[dict]:
    """The confluences the most recent scan before the entry logged for this instrument and direction."""
    for s in reversed(scans):
        if s["asof"] <= entry and (symbol, direction) in s["rows"]:
            return {**s["rows"][(symbol, direction)], "scan_asof": s["asof"], "scan_file": s["file"],
                    "scan_run": s["run_type"], "config_version": s["config_version"],
                    "age_hours": round((entry - s["asof"]).total_seconds() / 3600, 1)}
    return None


def append_account_snapshot(log_dir: Path, label: str, asof: pd.Timestamp, acct: dict) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "mt5_account.csv"
    keys = ["balance", "equity", "margin", "free_margin", "margin_level", "profit", "currency", "server", "login"]
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        if new:
            wr.writerow(["time_utc", "run"] + keys)
        wr.writerow([asof.strftime("%Y-%m-%d %H:%M"), label] + [acct.get(k) for k in keys])
    return p


def _money(v, cur: str = "") -> str:
    return "–" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:,.2f}{(' ' + cur) if cur else ''}"


def render_journal_html(asof: pd.Timestamp, disp_tz: ZoneInfo, acct: dict, history: pd.DataFrame, trades: list[dict],
                        cash: list[dict], status: str, latest_dashboard: str, history_days: int,
                        decisions: Optional[pd.DataFrame] = None) -> str:
    e = html.escape

    def local(t) -> str:
        return "–" if t is None else pd.Timestamp(t).tz_convert(disp_tz).strftime("%Y-%m-%d %I:%M %p %Z")

    cur = acct.get("currency", "")
    nav = ("<nav>" + (f"<a href='{e(latest_dashboard)}'>Scanner dashboard</a>" if latest_dashboard else "<span class='muted'>Scanner dashboard</span>")
           + "<b>Trading journal and balance</b></nav>")
    head = (f"<h1>Trading journal and balance</h1><div class='muted'>Built {e(asof.strftime('%Y-%m-%d %H:%M'))} UTC · "
            f"{e(local(asof))} · MT5 {e(status)}</div>")
    if not acct:
        body = f"<div class='banner warn'>MT5 account unavailable: {e(status)}. The page shows logged history only.</div>"
    else:
        mode = acct.get("mode", "")
        body = ("<div class='banner" + (" warn'><b>LIVE ACCOUNT.</b> " if mode == "real" else "'>")
                + f"{e(acct.get('server', ''))} · login {acct.get('login')} · {e(mode)} · leverage 1:{acct.get('leverage')}. "
                "Read only: this report never places, changes or closes orders.</div>")
        cards = [("Balance", _money(acct.get("balance"), cur)), ("Equity", _money(acct.get("equity"), cur)),
                 ("Open P/L", _money(acct.get("profit"), cur)), ("Margin used", _money(acct.get("margin"), cur)),
                 ("Free margin", _money(acct.get("free_margin"), cur)),
                 ("Margin level", f"{acct['margin_level']:,.0f}%" if acct.get("margin_level") else "–")]
        body += "<div class='cards'>" + "".join(f"<div class='card'><span class='muted'>{k}</span><b>{v}</b></div>" for k, v in cards) + "</div>"
    # balance history
    body += "<h2>Balance history</h2>"
    if history.empty:
        body += "<p class='muted'>No snapshots yet. One is logged every scan and every journal build.</p>"
    else:
        h = history.tail(60).iloc[::-1]
        body += ("<p class='muted'>One snapshot per scan or journal build, newest first (last 60). Full log: logs/mt5_account.csv</p>"
                 "<div class='wrap'><table><tr><th>Time</th><th>Run</th><th class='num'>Balance</th><th class='num'>Change</th>"
                 "<th class='num'>Equity</th><th class='num'>Open P/L</th><th class='num'>Margin used</th></tr>")
        bal = history["balance"].astype(float)
        chg = bal.diff()
        for i, r in h.iterrows():
            c = chg.loc[i]
            c_txt = "–" if pd.isna(c) else f"<span class='{'long' if c > 0 else 'short' if c < 0 else 'muted'}'>{c:+,.2f}</span>"
            body += (f"<tr><td>{e(local(pd.Timestamp(r['time_utc'], tz=UTC)))}</td><td>{e(str(r['run']))}</td>"
                     f"<td class='num'>{_money(float(r['balance']))}</td><td class='num'>{c_txt}</td>"
                     f"<td class='num'>{_money(float(r['equity']))}</td><td class='num'>{_money(float(r['profit']))}</td>"
                     f"<td class='num'>{_money(float(r['margin']))}</td></tr>")
        body += "</table></div>"
    # trades
    n_open = sum(1 for t in trades if t["status"] != "closed")
    body += (f"<h2>Positions taken ({len(trades)}, {n_open} open)</h2>"
             f"<p class='muted'>From MT5 deal history (last {history_days} days) plus open positions. Confluences are the ones the "
             "scanner logged for that instrument and direction in the last live scan before the entry, so you can compare "
             "what the screen said with what was traded.</p>")
    if not trades:
        body += "<p class='muted'>No positions in the history window.</p>"
    else:
        body += ("<div class='wrap'><table><tr><th>Position</th><th>Instrument</th><th>Dir</th><th class='num'>Lots</th>"
                 "<th>Opened</th><th class='num'>Entry</th><th>Closed</th><th class='num'>Exit</th><th class='num'>Net P/L</th>"
                 "<th>Status</th><th>Scan before entry</th><th>Setup</th><th>C1</th><th>C2</th><th>C3</th><th>C4</th><th>C5</th><th>C6</th>"
                 "<th class='num'>Total</th><th>Reversal / zone</th></tr>")
        for t in trades:
            cf = t.get("confluences")
            pl_cls = "long" if t["net"] >= 0 else "short"
            row = (f"<tr><td>{t['position_id']}</td><td><b>{e(t['symbol'])}</b></td><td class='{t['direction']}'>{t['direction']}</td>"
                   f"<td class='num'>{t['volume']:g}</td><td>{e(local(t['open_ts']))}</td><td class='num'>{_fmt(t['entry_price'], 6)}</td>"
                   f"<td>{e(local(t['close_ts']))}</td><td class='num'>{_fmt(t['exit_price'], 6)}</td>"
                   f"<td class='num {pl_cls}'>{_money(t['net'])}</td><td>{e(t['status'])}</td>")
            if not cf:
                row += f"<td colspan='10' class='muted'>{e(t.get('confluence_note', 'no scan logged before entry'))}</td></tr>"
            else:
                sec = cf.get("section") or ""
                cls = "q" if sec == "qualified" else "d" if sec == "developing" else ""
                stale = cf["age_hours"] > 24
                row += (f"<td><a href='{e(cf['scan_file'])}'>{e(cf['scan_asof'].strftime('%m-%d %H:%M'))} UTC</a><br>"
                        f"<span class='{'warn' if stale else 'muted'}'>{cf['age_hours']}h before entry</span></td>"
                        f"<td><span class='tag {cls}'>{e(sec)}</span>{(' #' + str(cf['rank'])) if cf.get('rank') else ''}</td>"
                        + "".join(f"<td>{_ck(bool(cf.get(k)))}</td>" for k in ("c1", "c2", "c3", "c4", "c5", "c6"))
                        + f"<td class='num'>{cf.get('total')}</td><td class='wrapc'>{e(cf.get('candle') or '')}"
                        f"{' · ' if cf.get('candle') else ''}zone {_fmt(cf.get('zone_level'), 6)} {e(cf.get('zone_kind') or '')}"
                        + (f"<br><span class='muted'>{e(cf.get('reason') or '')}</span>" if sec == "not shown" else "")
                        + "</td></tr>")
            body += row
        body += "</table></div>"
    body += "<h2>Trade decisions at the hold (trade_gate.py)</h2>"
    if decisions is None or decisions.empty:
        body += "<p class='muted'>No decisions logged yet. Run python trade_gate.py after a scan.</p>"
    else:
        body += ("<p class='muted'>Every setup reviewed at the hold, newest first (last 50). Full log: logs/trade_decisions.csv</p>"
                 "<div class='wrap'><table><tr><th>Time</th><th>Ticket</th><th>Instrument</th><th>Dir</th><th>Setup</th>"
                 "<th>Decision</th><th>Account</th><th>Placed by</th><th>Entries / lots</th><th>Stop / TP1 / TP2</th>"
                 "<th class='num'>Risk</th><th>Your changes and notes</th></tr>")
        for _, r in decisions.tail(50).iloc[::-1].iterrows():
            def sv(k: str) -> str:
                v = r.get(k)
                return "" if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
            dec = sv("decision")
            cls = "long" if dec.startswith(("sent", "approved")) else "short" if dec.startswith("failed") else "muted"
            body += (f"<tr><td>{e(local(pd.Timestamp(r['time_utc'], tz=UTC)))}</td><td>{e(sv('ticket_id'))}</td>"
                     f"<td><b>{e(sv('symbol'))}</b></td><td class='{e(sv('direction'))}'>{e(sv('direction'))}</td>"
                     f"<td>{e(sv('section'))} {e(sv('total'))}</td><td class='{cls}'>{e(dec)}</td><td>{e(sv('account'))}</td>"
                     f"<td>{'' if dec.startswith('skipped') else e(sv('mode'))}</td>"
                     f"<td>{e(sv('entries'))}<br><span class='muted'>{e(sv('lots'))}</span></td>"
                     f"<td>{e(sv('stop'))} / {e(sv('tp1'))} / {e(sv('tp2'))}</td><td class='num'>{e(sv('risk'))}</td>"
                     f"<td class='wrapc'>{e(sv('modifications'))}{'<br>' if sv('modifications') and sv('notes') else ''}"
                     f"<span class='muted'>{e(sv('notes'))}</span>{'<br>orders ' + e(sv('orders')) if sv('orders') else ''}</td></tr>")
        body += "</table></div>"
    if cash:
        body += ("<h2>Deposits, withdrawals and charges</h2><div class='wrap'><table><tr><th>Time</th><th class='num'>Amount</th>"
                 "<th>Comment</th></tr>")
        for d in sorted(cash, key=lambda d: d["time"], reverse=True):
            body += (f"<tr><td>{e(local(d['time']))}</td><td class='num'>{_money(d['profit'] + d['commission'] + d['fee'])}</td>"
                     f"<td>{e(d['comment'])}</td></tr>")
        body += "</table></div>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Trading Journal</title><style>{_CSS}</style></head><body><main>{nav}{head}{body}</main></body></html>")


def write_journal(cfg: dict, base: Path, out_dir: Path, client: Optional[Mt5Client], asof: pd.Timestamp,
                  label: str, latest_dashboard: str = "") -> Path:
    """[OWNER cfg-0.5.0] Rebuild output/journal.html and logs/trade_journal.csv from MT5 and the scan archive."""
    jcfg = cfg.get("journal", {})
    days = int(jcfg.get("history_days", 365))
    log_dir = base / cfg["paths"]["log_dir"]
    acct, trades, cash = {}, [], []
    status = client.status if client else "disabled"
    if client and client.mt5:
        try:
            acct = client.account()
            trades, cash = build_trades(client.deals(days), client.positions())
            if acct:
                append_account_snapshot(log_dir, label, asof, acct)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("journal: MT5 read failed: %s", exc)
            status = f"read failed: {exc}"
    scans = load_scan_history(out_dir)
    for t in trades:
        t["confluences"] = confluences_before(scans, t["symbol"], t["direction"], t["open_ts"])
        if not t["confluences"]:
            in_universe = any((t["symbol"], t["direction"]) in s["rows"] for s in scans)
            t["confluence_note"] = ("no scan logged before entry" if in_universe or not scans
                                    else f"{t['mt5_symbol']} is not in the scanner universe")
    if not latest_dashboard:
        latest_dashboard = scans[-1]["file"] if scans else ""
    p_hist = log_dir / "mt5_account.csv"
    history = pd.read_csv(p_hist) if p_hist.exists() else pd.DataFrame()
    p_dec = log_dir / "trade_decisions.csv"   # written by trade_gate.py
    decisions = pd.read_csv(p_dec) if p_dec.exists() else pd.DataFrame()
    disp_tz = ZoneInfo(cfg.get("display_timezone", "America/Chicago"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "journal.html"
    path.write_text(render_journal_html(asof, disp_tz, acct, history, trades, cash, status, latest_dashboard, days,
                                        decisions), encoding="utf-8")
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "trade_journal.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["position_id", "symbol", "mt5_symbol", "direction", "volume", "open_utc", "entry_price", "close_utc",
                     "exit_price", "net", "status", "comment", "scan_asof_utc", "scan_age_hours"] + list(JOURNAL_CONF_KEYS))
        for t in trades:
            cf = t.get("confluences") or {}
            wr.writerow([t["position_id"], t["symbol"], t["mt5_symbol"], t["direction"], t["volume"], fmt_ts(t["open_ts"]),
                         t["entry_price"], fmt_ts(t["close_ts"]), t["exit_price"], round(t["net"], 2), t["status"], t["comment"],
                         fmt_ts(cf.get("scan_asof")), cf.get("age_hours")]
                        + [("; ".join(cf[k]) if k == "flags" and cf.get(k) else cf.get(k)) for k in JOURNAL_CONF_KEYS])
    LOG.info("journal: %d positions, %d open; wrote %s", len(trades), sum(1 for t in trades if t["status"] != "closed"), path)
    return path


# =============================================================================
# Run orchestration
# =============================================================================

def resolve_run(run: str, asof: pd.Timestamp) -> tuple[str, pd.Timestamp]:
    """Return (run_type, daily_cutoff). Daily bias is computed from bars complete at the evening run."""
    if run == "auto":
        run = "evening" if asof.hour < 6 or asof.hour >= 18 else "preny"
    if run == "evening":
        return "evening", asof
    evening = asof.normalize() + pd.Timedelta(minutes=5)
    if evening > asof:
        evening -= pd.Timedelta(days=1)
    return "pre NY", evening


def run_scan(cfg: dict, base: Path, run: str, asof: pd.Timestamp, source: str, demo: bool,
             out_dir: Optional[Path] = None) -> dict[str, Path]:
    run_type, daily_cutoff = resolve_run(run, asof)
    universe = build_universe(cfg)
    by_sym = {i.symbol: i for i in universe}
    LOG.info("run %s at %s UTC, %d instruments, config %s", run_type, asof, len(universe), cfg["config_version"])

    # [OWNER cfg-0.5.0] MT5 is read only: currency bars, broker prices, open positions, journal
    client: Optional[Mt5Client] = None
    if not demo and cfg.get("mt5", {}).get("enabled", False):
        client = Mt5Client(cfg)
        if not client.connect():
            LOG.warning("MT5 unavailable (%s); currency pairs use TradingView", client.status)
    try:
        return _run_scan(cfg, base, run_type, daily_cutoff, universe, by_sym, asof, source, demo, out_dir, client)
    finally:
        if client:
            client.shutdown()


def _run_scan(cfg: dict, base: Path, run_type: str, daily_cutoff: pd.Timestamp, universe: list[Instrument],
              by_sym: dict[str, Instrument], asof: pd.Timestamp, source: str, demo: bool, out_dir: Optional[Path],
              client: Optional[Mt5Client]) -> dict[str, Path]:
    if demo:
        src: BarSource = DemoSource(asof)
    elif source == "csv":
        src = CsvSource(cfg, base)
    elif client and client.mt5 and cfg["bars"].get("fx_source") == "mt5":
        src = RoutedSource(Mt5Source(client, cfg), TradingViewSource(cfg), int(cfg["bars"]["min_completed_bars"]))
    else:
        src = TradingViewSource(cfg)

    # COT
    if demo:
        cot = demo_cot(asof, cfg)
        cot_status = "demo"
    else:
        try:
            cot = CotSource(cfg, base).load()
            cot_status = f"{len(cot)} markets"
        except Exception as exc:  # noqa: BLE001
            LOG.warning("COT load failed: %s", exc)
            cot, cot_status = {}, f"failed: {exc}"
    # calendar
    events, cal_status = load_calendar(cfg, base, asof, demo)
    # headlines + tone + theme
    heads, head_status = load_headlines(cfg, base, asof, demo)
    tone_cfg = cfg if not demo else {**cfg, "sentiment": {**cfg["sentiment"], "model": "lexicon"}}
    tone = ToneModel(tone_cfg)
    engine = SentimentEngine(cfg)
    recent = [h for h in heads if 0 <= (asof - pd.Timestamp(h.published)).total_seconds() / 3600 <= float(cfg["sentiment"]["window_hours"])]
    for h, s in zip(recent, tone.score([h.title for h in recent])):
        h.tone = s
        h.theme = engine.tag_theme(h.title)

    results: list[InstrumentResult] = []
    all_rows: list[Row] = []
    latest = {TF_D: None, TF_4H: None, TF_2H: None}
    for inst in universe:
        try:
            bars, errs = load_instrument_bars(src, inst, cfg, asof, daily_cutoff)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s: data load failed: %s", inst.symbol, exc)
            results.append(InstrumentResult(inst, [], [f"data load failed: {exc}"], src.provider(inst), {}))
            continue
        last = {tf: (str(b.index[-1] + TF_DELTA[tf]) if len(b) else "") for tf, b in bars.items()}
        for tf, b in bars.items():
            if len(b):
                t = b.index[-1] + TF_DELTA[tf]
                latest[tf] = t if latest[tf] is None or t > latest[tf] else latest[tf]
        if errs:
            LOG.warning("%s: data checks failed: %s", inst.symbol, "; ".join(errs))
            results.append(InstrumentResult(inst, [], errs, src.provider(inst), last))
            continue
        reading = cot_for_instrument(inst, cot, asof, cfg["cot"])
        sent = engine.score(inst, recent, asof) if tone.kind != "off" else {"S": None, "weight_sum": 0, "thin": True, "drivers": []}
        cal = next_event(events, inst.calendar_currencies, asof, cfg.get("calendar", {}))
        rows = score_instrument(inst, bars, cfg, reading, sent, cal, engine)
        results.append(InstrumentResult(inst, rows, [], src.provider(inst), last))
        all_rows += rows

    top = rank(all_rows, 5)
    mark_same_underlying(top, by_sym)
    shown = {(r.symbol, r.direction) for r in top}
    footer = []
    for res in results:
        if res.errors:
            status = "data error: " + "; ".join(res.errors)
            bias = h4 = "–"
        else:
            hit = [r for r in res.rows if (r.symbol, r.direction) in shown]
            if hit:
                status = f"shown: rank {hit[0].rank}, {hit[0].section}, {hit[0].direction}"
            else:
                elig = [r for r in res.rows if r.section != "not shown"]
                status = (f"{elig[0].section} {elig[0].direction}, ranked below top 5" if elig else rejection_reason(res.rows))
            bias, h4 = res.rows[0].daily_bias, res.rows[0].structure_4h
        footer.append({"symbol": res.inst.symbol, "group": res.inst.group, "bias": bias, "h4": h4,
                       "status": status, "provider": res.provider})
        for r in res.rows:
            if (r.symbol, r.direction) not in shown and not r.reason:
                r.reason = status

    disp_tz = ZoneInfo(cfg.get("display_timezone", "America/Chicago"))
    meta = {"run_type": run_type, "asof_utc": asof.strftime("%Y-%m-%d %H:%M"),
            "asof_local": asof.tz_convert(disp_tz).strftime("%Y-%m-%d %I:%M %p %Z"),
            "stamp": asof.strftime("%Y%m%dT%H%MZ"), "addendum_version": cfg["addendum_version"],
            "config_version": cfg["config_version"], "code_version": SCANNER_CODE_VERSION, "demo": demo,
            "daily_cutoff_utc": daily_cutoff.strftime("%Y-%m-%d %H:%M"),
            "freshness": {
                "Bars": f"{src.name}; last D close {fmt_ts(latest[TF_D])}, 4H {fmt_ts(latest[TF_4H])}, 2H {fmt_ts(latest[TF_2H])}",
                "COT": cot_status + latest_cot_pub(cot, asof),
                "Calendar": cal_status,
                "Headlines": f"{head_status}; {len(recent)} in last {cfg['sentiment']['window_hours']}h; tone model {tone.status}",
            },
            "instruments_scanned": len(universe), "instruments_with_errors": sum(1 for r in results if r.errors)}
    broker = None
    if client:
        broker = {"connected": bool(client.mt5), "status": client.status}
        if client.mt5:
            fallback = getattr(src, "fallback", {})
            meta["freshness"]["MT5"] = (f"{client.status}; {client.clock_note}"
                                        + (f"; TradingView fallback for {', '.join(fallback)} (see Bars column)" if fallback else ""))
            try:
                broker["prices"] = client.prices([i.symbol for i in universe if i.asset == "fx"])
                broker["positions"] = client.positions()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("MT5 prices/positions failed: %s", exc)
                broker = {"connected": False, "status": f"read failed: {exc}"}
        else:
            meta["freshness"]["MT5"] = f"unavailable: {client.status}"
    od = out_dir or (base / cfg["paths"]["output_dir"])
    news_view = build_news_view(cfg, base, asof, events, universe, client, demo)
    news_view["tz"] = cfg.get("display_timezone", "America/Chicago")
    paths = write_outputs(od, meta, top, all_rows, footer, broker, news_view)
    append_c2_log(base / cfg["paths"]["log_dir"], meta, results)
    if client:
        try:
            paths["journal"] = write_journal(cfg, base, od, client, asof, run_type, paths["html"].name)
        except Exception:  # noqa: BLE001
            LOG.exception("journal build failed")
    LOG.info("top %d: %s", len(top), ", ".join(f"{r.symbol} {r.direction} {r.total}" for r in top) or "none")
    LOG.info("wrote %s", paths["html"])
    return paths


def fmt_ts(t) -> str:
    return "–" if t is None else pd.Timestamp(t).strftime("%Y-%m-%d %H:%M")


def latest_cot_pub(cot: dict[str, CotSeries], asof: pd.Timestamp) -> str:
    pubs = [s.frame[s.frame["publication_ts"] <= asof]["publication_ts"].max() for s in cot.values() if len(s.frame)]
    pubs = [p for p in pubs if pd.notna(p)]
    return f"; latest published {pd.Timestamp(max(pubs)).strftime('%Y-%m-%d %H:%M')} UTC" if pubs else ""


def demo_cot(asof: pd.Timestamp, cfg: dict) -> dict[str, CotSeries]:
    rng = np.random.default_rng(11)
    out = {}
    end = (asof - pd.Timedelta(days=3)).normalize()
    end -= pd.Timedelta(days=(end.dayofweek - 1) % 7)  # previous Tuesday
    dates = pd.date_range(end=end.tz_localize(None), periods=70, freq="7D")
    for mkt, m in cfg["cot"]["markets"].items():
        net = np.cumsum(rng.normal(0, 8000, len(dates))) + rng.normal(0, 30000)
        df = pd.DataFrame({"report_date": dates, "net": net, "open_interest": 400000.0})
        df["publication_ts"] = [cot_publication_ts(pd.Timestamp(d), cfg["cot"]) for d in dates]
        out[mkt] = CotSeries(mkt, m["family"], df, f"demo {mkt}")
    return out


def parse_asof(s: Optional[str]) -> pd.Timestamp:
    if not s:
        return pd.Timestamp.now(tz=UTC).floor("min")
    t = pd.Timestamp(s.replace("Z", "+00:00"))
    return (t.tz_localize(UTC) if t.tzinfo is None else t.tz_convert(UTC)).floor("min")


def daemon(cfg: dict, base: Path, source: str) -> None:
    """Simple scheduler fixed in UTC so it never drifts with daylight saving [Add 4]."""
    times = {"evening": cfg.get("runs", {}).get("evening", "00:05"), "preny": cfg.get("runs", {}).get("preny", "12:05")}
    LOG.info("daemon started; runs at %s UTC", ", ".join(f"{k} {v}" for k, v in times.items()))
    while True:
        now = pd.Timestamp.now(tz=UTC)
        nxt = []
        for k, hm in times.items():
            hh, mm = (int(x) for x in hm.split(":"))
            t = now.normalize() + pd.Timedelta(hours=hh, minutes=mm)
            if t <= now:
                t += pd.Timedelta(days=1)
            nxt.append((t, k))
        t, k = min(nxt)
        LOG.info("next run %s at %s UTC", k, t)
        time.sleep(max(1.0, (t - pd.Timestamp.now(tz=UTC)).total_seconds()))
        try:
            run_scan(cfg, base, k, parse_asof(None), source, demo=False)
        except Exception:  # noqa: BLE001
            LOG.exception("scan failed")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Daily Instrument Scanner (Addendum v0.1)")
    ap.add_argument("--config", default="scanner_config.yaml")
    ap.add_argument("--run", default="auto", choices=["auto", "evening", "preny"])
    ap.add_argument("--source", default=None, choices=["tradingview", "csv"], help="override bars.source")
    ap.add_argument("--asof", default=None, help="decision time, e.g. 2026-10-02T12:05Z (default: now)")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--daemon", action="store_true", help="stay running and scan at 00:05 and 12:05 UTC")
    ap.add_argument("--journal", action="store_true", help="rebuild only the journal and balance page from MT5")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    cfg_path = Path(a.config).resolve()
    base = cfg_path.parent
    cfg = load_config(cfg_path)
    log_dir = base / cfg["paths"]["log_dir"]
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "scanner.log", encoding="utf-8")])
    source = a.source or cfg["bars"].get("source", "tradingview")
    demo = a.demo or source == "demo"
    if a.daemon:
        daemon(cfg, base, source)
        return 0
    if a.journal:
        client = Mt5Client(cfg)
        if not client.connect():
            LOG.warning("MT5 unavailable: %s", client.status)
        try:
            od = Path(a.out_dir) if a.out_dir else base / cfg["paths"]["output_dir"]
            print(write_journal(cfg, base, od, client, parse_asof(a.asof), "journal"))
        finally:
            client.shutdown()
        return 0
    paths = run_scan(cfg, base, a.run, parse_asof(a.asof), source, demo, Path(a.out_dir) if a.out_dir else None)
    print(paths["html"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
