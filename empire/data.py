"""Daily data layer: MT5, TradingView and FRED loaders, a growing local cache, splicing rules and checks.

Source order is set per instrument in personality_config.yaml. The owner's rule: MT5 first, then
TradingView, then FRED for anything still missing. Older history from a later source is spliced in only
when the two sources agree on their overlap, so one feed never silently shifts another's price levels.

Every series is stored by trade date. A daily bar opening at 17:00 New York belongs to the next calendar
day's session, so timestamped bars are dated as (New York time + 7 hours).date().
"""
from __future__ import annotations

import io
import logging
import os
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import Inst, MAJORS

LOG = logging.getLogger("empire.data")
UTC = timezone.utc
NY = ZoneInfo("America/New_York")
COLS = ["open", "high", "low", "close", "volume"]


@dataclass
class Series:
    symbol: str
    bars: pd.DataFrame                    # index: naive trade date; columns COLS + close_only, source
    notes: list[str] = field(default_factory=list)
    spread: Optional[float] = None        # latest broker spread in price units (MT5 only)

    @property
    def close(self) -> pd.Series:
        return self.bars["close"]

    @property
    def years(self) -> float:
        if self.bars.empty:
            return 0.0
        return (self.bars.index[-1] - self.bars.index[0]).days / 365.25


def trade_dates(utc_index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(utc_index, utc=True))
    ny = idx.tz_convert(NY) + pd.Timedelta(hours=7)
    return pd.DatetimeIndex(ny.tz_localize(None).normalize())


def last_completed_session(now: Optional[pd.Timestamp] = None) -> pd.Timestamp:
    """The latest trade date whose New York 17:00 close has passed (weekends roll back to Friday)."""
    now = pd.Timestamp.now(tz="UTC") if now is None else (now if now.tzinfo else now.tz_localize("UTC"))
    ny = now.tz_convert(NY)
    d = ny.normalize().tz_localize(None)
    if ny.hour < 17:
        d -= pd.Timedelta(days=1)
    while d.dayofweek >= 5:
        d -= pd.Timedelta(days=1)
    return d


def clean_daily(df: pd.DataFrame, source: str, close_only: bool = False) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    if "volume" not in df:
        df["volume"] = np.nan
    for c in ("open", "high", "low"):
        if c not in df:
            df[c] = df["close"]
    df = df[COLS].astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df.index.dayofweek < 5]
    df = df.dropna(subset=["close"])
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    df["close_only"] = bool(close_only)
    df["source"] = source
    return df


def quality_issues(df: pd.DataFrame, additive: bool) -> list[str]:
    out = []
    if df.empty:
        return ["no data"]
    if df.index.duplicated().any():
        out.append("duplicate dates")
    if not additive and (df[["open", "high", "low", "close"]] <= 0).any().any():
        out.append("non-positive prices")
    gaps = df.index.to_series().diff().dt.days
    if (gaps > 10).any():
        out.append(f"{int((gaps > 10).sum())} gaps longer than 10 days")
    r = np.log(df["close"]).diff() if not additive else df["close"].diff()
    if len(r.dropna()) > 50:
        z = (r - r.median()).abs() / (1.4826 * (r - r.median()).abs().median() + 1e-12)
        n = int((z > 25).sum())
        if n:
            out.append(f"{n} returns beyond 25 robust sd (check for bad ticks or splices)")
    return out


# =============================================================================
# Sources
# =============================================================================

class Mt5Daily:
    name = "mt5"

    def __init__(self, scanner_cfg: dict):
        self.client = None
        self.status = "not connected"
        try:
            import scanner
            c = scanner.Mt5Client(scanner_cfg)
            if c.connect():
                self.client = c
            self.status = c.status
        except Exception as exc:  # noqa: BLE001
            self.status = f"MT5 unavailable: {exc}"

    def fetch(self, spec: dict, n: int) -> tuple[pd.DataFrame, Optional[float]]:
        if self.client is None:
            raise RuntimeError(self.status)
        m = self.client.mt5
        sym = self.client.mt5_symbol(spec["mt5"])
        m.symbol_select(sym, True)
        r = m.copy_rates_from_pos(sym, m.TIMEFRAME_D1, 0, int(n))
        if r is None or len(r) == 0:
            raise RuntimeError(f"no MT5 daily bars for {sym}: {m.last_error()}")
        idx = trade_dates(self.client.to_utc(r["time"]))
        df = pd.DataFrame({"open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                           "volume": r["tick_volume"]}, index=idx)
        spread = None
        tk = m.symbol_info_tick(sym)
        if tk and tk.ask and tk.bid:
            spread = float(tk.ask - tk.bid)
        return df, spread

    def close(self) -> None:
        if self.client:
            self.client.shutdown()


class TvDaily:
    name = "tradingview"

    def __init__(self, pause: float = 1.0, retries: int = 3):
        from tvDatafeed import Interval, TvDatafeed
        user, pw = os.environ.get("TV_USERNAME"), os.environ.get("TV_PASSWORD")
        self.tv = TvDatafeed(user, pw) if user and pw else TvDatafeed()
        self.Interval = Interval
        self.pause, self.retries = pause, retries

    def fetch(self, spec: dict, n: int) -> tuple[pd.DataFrame, Optional[float]]:
        last = None
        for attempt in range(self.retries):
            try:
                df = self.tv.get_hist(symbol=spec["tv"], exchange=spec.get("exchange", ""), interval=self.Interval.in_daily,
                                      n_bars=int(n), fut_contract=spec.get("fut_contract"))
                time.sleep(self.pause)
                if df is None or df.empty:
                    raise RuntimeError("empty response")
                # tvDatafeed returns naive local datetimes built with fromtimestamp(); mktime reverses that
                utc = [datetime.fromtimestamp(time.mktime(d.timetuple()), tz=UTC) for d in pd.to_datetime(df.index).to_pydatetime()]
                out = df[[c for c in ("open", "high", "low", "close", "volume") if c in df.columns]].copy()
                out.index = trade_dates(utc)
                return out, None
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"TradingView {spec.get('exchange')}:{spec.get('tv')} failed: {last}")


class FredDaily:
    """FRED keyless CSV download (fredgraph.csv). Close only: no high or low, so ATR falls back to |change|."""
    name = "fred"
    URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

    def __init__(self, start: str):
        self.start = start

    def fetch(self, spec: dict, n: int) -> tuple[pd.DataFrame, Optional[float]]:
        import requests
        sid = spec["fred"]
        r = requests.get(self.URL, params={"id": sid, "cosd": self.start}, timeout=60)
        r.raise_for_status()
        raw = pd.read_csv(io.StringIO(r.text))
        date_col = raw.columns[0]
        vals = pd.to_numeric(raw[sid], errors="coerce") if sid in raw else pd.to_numeric(raw.iloc[:, 1], errors="coerce")
        s = pd.Series(vals.values, index=pd.to_datetime(raw[date_col])).dropna()
        if spec.get("invert"):
            s = 1.0 / s
        if s.empty:
            raise RuntimeError(f"FRED {sid}: no observations")
        return pd.DataFrame({"close": s}), None


class CsvDaily:
    """data/personality/import/<SYMBOL>.csv with date, open, high, low, close[, volume]."""
    name = "csv"

    def __init__(self, folder: Path):
        self.folder = folder

    def fetch(self, spec: dict, n: int) -> tuple[pd.DataFrame, Optional[float]]:
        p = self.folder / f"{spec['csv']}.csv"
        if not p.exists():
            raise FileNotFoundError(str(p))
        df = pd.read_csv(p)
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.pop("date")).dt.normalize()
        return df, None


# =============================================================================
# Demo data: a factor model with regimes. For plumbing checks only, never for decisions.
# =============================================================================

DEMO_LOAD = {  # currency value vs USD: (risk, rate, oil, copper, idio vol)
    "EUR": (0.10, -0.25, 0.00, 0.05, 0.0045), "JPY": (-0.30, -0.45, -0.05, 0.0, 0.0050),
    "GBP": (0.20, -0.20, 0.00, 0.05, 0.0050), "CHF": (-0.20, -0.20, 0.00, 0.0, 0.0045),
    "AUD": (0.50, -0.15, 0.05, 0.30, 0.0055), "NZD": (0.45, -0.15, 0.00, 0.25, 0.0058),
    "CAD": (0.20, -0.10, 0.30, 0.05, 0.0040),
}
DEMO_FIRST_DAY, DEMO_LAST_DAY = "2005-01-03", "2028-12-29"
DEMO_START = {"EUR": 1.25, "JPY": 1 / 110.0, "GBP": 1.55, "CHF": 1 / 1.05, "AUD": 0.80, "NZD": 0.70, "CAD": 1 / 1.20}


class DemoData:
    def __init__(self, end: pd.Timestamp, years: int = 21, seed: int = 11):
        self.end = pd.Timestamp(end).tz_localize(None).normalize() if pd.Timestamp(end).tzinfo else pd.Timestamp(end).normalize()
        rng = np.random.default_rng(seed)
        # a fixed calendar, so a date has the same synthetic price whatever the as-of date of the run
        idx = pd.bdate_range(start=DEMO_FIRST_DAY, end=max(self.end, pd.Timestamp(DEMO_LAST_DAY)))
        n = len(idx)
        regime_len = 180
        reg = np.repeat(rng.integers(0, 4, n // regime_len + 1), regime_len)[:n]
        vol_mult = np.where(np.isin(reg, (2, 3)), 1.7, 0.9)
        f_risk = rng.normal(0, 1, n) * vol_mult
        f_rate = rng.normal(0, 1, n) + np.where(reg % 2 == 1, 0.08, -0.08)
        f_oil = rng.normal(0, 1, n)
        f_cu = 0.4 * f_risk + rng.normal(0, 1, n)
        self.idx = idx
        self.series: dict[str, pd.Series] = {}
        us2 = 2.0 + np.cumsum(0.035 * f_rate)
        us2 = np.clip(us2, 0.05, None)
        self.series["US02Y"] = pd.Series(us2, idx)
        self.series["US10Y"] = pd.Series(np.clip(us2 + 0.8 + np.cumsum(rng.normal(0, 0.02, n)), 0.3, None), idx)
        vix = np.empty(n)
        vix[0] = 17.0
        for t in range(1, n):
            vix[t] = max(9.0, vix[t - 1] + 0.04 * (17 - vix[t - 1]) - 0.9 * f_risk[t] + 0.3 * rng.normal())
        self.series["VIX"] = pd.Series(vix, idx)
        self.series["HYOAS"] = pd.Series(np.clip(4 + np.cumsum(-0.03 * f_risk + rng.normal(0, 0.02, n)) * 0.3, 2, 12), idx)
        logv = {}
        for c, (a, b, o, cu, iv) in DEMO_LOAD.items():
            d = 0.004 * (a * f_risk + b * f_rate + o * f_oil + cu * f_cu) + rng.normal(0, iv, n)
            logv[c] = np.log(DEMO_START[c]) + np.cumsum(d)
        logv["USD"] = np.zeros(n)
        for c in MAJORS:
            if c != "USD":
                self.series[f"{c}USD"] = pd.Series(np.exp(logv[c]), idx)
        self.logv = logv
        self.series["XAUUSD"] = pd.Series(900 * np.exp(np.cumsum(0.0003 + 0.01 * (-0.2 * f_risk - 0.4 * f_rate) / 2 + rng.normal(0, 0.009, n))), idx)
        self.series["WTI"] = pd.Series(np.clip(60 * np.exp(np.cumsum(0.02 * f_oil * 0.9 + 0.006 * f_risk)), 8, None), idx)
        self.series["COPPER"] = pd.Series(2.5 * np.exp(np.cumsum(0.012 * f_cu + rng.normal(0, 0.004, n))), idx)
        self.series["SPX"] = pd.Series(1200 * np.exp(np.cumsum(0.0003 + 0.009 * f_risk + rng.normal(0, 0.003, n))), idx)
        self.series["NDX"] = pd.Series(1600 * np.exp(np.cumsum(0.0004 + 0.011 * f_risk + rng.normal(0, 0.004, n))), idx)
        # dollar index: geometric basket, euro about 57 percent
        w = {"EUR": 0.576, "JPY": 0.136, "GBP": 0.119, "CAD": 0.091, "CHF": 0.036}
        dxy = 50.14348112 * np.exp(-sum(wt * logv[c] for c, wt in w.items()))
        dxy = dxy / dxy[0] * 85.0
        self.series["DXY"] = pd.Series(dxy, idx)
        for c in MAJORS:
            base = self.series["US02Y"].values if c == "USD" else 1.5 + np.cumsum(rng.normal(0, 0.03, n))
            self.series[f"{c}02Y"] = pd.Series(base, idx)
        self.rng = rng

    def close_series(self, sym: str) -> pd.Series:
        if sym in self.series:
            return self.series[sym]
        b, q = sym[:3], sym[3:]
        if b in self.logv and q in self.logv:
            return pd.Series(np.exp(self.logv[b] - self.logv[q]), self.idx)
        raise KeyError(sym)

    def bars(self, sym: str, additive: bool) -> pd.DataFrame:
        c = self.close_series(sym)
        rng = np.random.default_rng(zlib.crc32(sym.encode()))
        prev = c.shift(1).fillna(c.iloc[0])
        if additive:
            span = np.abs(rng.normal(0, 0.03, len(c))) * max(c.abs().median(), 1.0) * 0.05
        else:
            span = np.abs(rng.normal(0, 0.004, len(c))) * c
        o = prev
        h = np.maximum(o, c) + span
        lo = np.minimum(o, c) - span
        vol = np.abs(rng.normal(1e5, 2e4, len(c)))
        return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": vol}, index=c.index)


# =============================================================================
# Loader: sources in order, local cache, splice rules
# =============================================================================

class DataLoader:
    def __init__(self, cfg: dict, base: Path, demo: bool = False, asof: Optional[pd.Timestamp] = None,
                 scanner_cfg: Optional[dict] = None, offline: bool = False):
        self.cfg = cfg
        self.dcfg = cfg.get("data", {})
        self.base = base
        self.demo = demo
        self.offline = offline
        self.asof = asof
        self.cache_dir = base / cfg["paths"]["state_dir"] / "bars"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.years = float(self.dcfg.get("history_years", 20))
        self.n_bars = int(self.dcfg.get("n_daily_bars", 5400))
        self._sources: dict[str, object] = {}
        self._scanner_cfg = scanner_cfg or {}
        self._demo: Optional[DemoData] = None
        self.status: dict[str, str] = {}

    def _source(self, kind: str):
        if kind in self._sources:
            return self._sources[kind]
        src = None
        try:
            if kind == "mt5":
                src = Mt5Daily(self._scanner_cfg)
                self.status["mt5"] = src.status
            elif kind == "tv":
                src = TvDaily(float(self.dcfg.get("request_pause_sec", 1.0)), int(self.dcfg.get("retries", 3)))
                self.status["tradingview"] = "ok"
            elif kind == "fred":
                start = (pd.Timestamp.now() - pd.DateOffset(years=int(self.years) + 2)).strftime("%Y-%m-%d")
                src = FredDaily(start)
                self.status["fred"] = "ok"
            elif kind == "csv":
                src = CsvDaily(self.base / self.cfg["paths"]["state_dir"] / "import")
        except Exception as exc:  # noqa: BLE001
            self.status[kind] = f"unavailable: {exc}"
            src = None
        self._sources[kind] = src
        return src

    def close(self) -> None:
        s = self._sources.get("mt5")
        if s is not None:
            s.close()

    @staticmethod
    def _kind(spec: dict) -> str:
        for k in ("mt5", "tv", "fred", "csv"):
            if k in spec:
                return k
        raise ValueError(f"source spec without a type: {spec}")

    def _cache_path(self, sym: str) -> Path:
        return self.cache_dir / f"{sym}.csv"

    def read_cache(self, sym: str) -> pd.DataFrame:
        p = self._cache_path(sym)
        if not p.exists():
            return pd.DataFrame(columns=COLS + ["close_only", "source"])
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        df.index = pd.DatetimeIndex(df.index).normalize()
        df["close_only"] = df["close_only"].astype(str).str.lower().isin(("true", "1"))
        return df

    def write_cache(self, sym: str, df: pd.DataFrame) -> None:
        out = df.copy()
        out.index.name = "date"
        out.to_csv(self._cache_path(sym))

    def _agree(self, a: pd.Series, b: pd.Series, additive: bool) -> tuple[bool, str]:
        j = pd.concat([a, b], axis=1, join="inner").dropna()
        if len(j) < 20:
            return False, f"overlap only {len(j)} days"
        if additive:
            d = float((j.iloc[:, 0] - j.iloc[:, 1]).abs().median())
            tol = float(self.dcfg.get("splice_tolerance_points", 0.05))
        else:
            d = float(np.log(j.iloc[:, 0] / j.iloc[:, 1]).abs().median())
            tol = float(self.dcfg.get("splice_tolerance_log", 0.004))
        return d <= tol, f"median overlap gap {d:.5f} (tolerance {tol})"

    def load(self, inst: Inst) -> Series:
        if self.demo:
            return self._load_demo(inst)
        notes: list[str] = []
        cache = self.read_cache(inst.symbol)
        primary: Optional[pd.DataFrame] = None
        spread = None
        used = []
        for spec in inst.sources:
            if "demo" in spec:
                continue
            kind = self._kind(spec)
            if self.offline and kind != "csv":
                continue
            src = self._source(kind)
            if src is None:
                notes.append(f"{kind} unavailable")
                continue
            try:
                raw, spr = src.fetch(spec, self.n_bars)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"{kind} failed: {exc}")
                continue
            df = clean_daily(raw, f"{kind}:{spec.get(kind)}", close_only=(kind == "fred"))
            if primary is None:
                primary, spread = df, spr
                used.append(kind)
                continue
            # older history only, and only if the feeds agree where they overlap
            older = df[df.index < primary.index[0]]
            if older.empty:
                continue
            ok, why = self._agree(primary["close"], df["close"], inst.additive)
            if ok:
                primary = pd.concat([older, primary])
                used.append(f"{kind} (backfill before {primary.index[len(older)].date()}, {why})")
            else:
                notes.append(f"{kind} not spliced: {why}")
            if self._enough(primary):
                break
        if primary is None:
            if cache.empty:
                notes.append("no source returned data and no cache")
                return Series(inst.symbol, cache, notes)
            notes.append("using cache only (stale)")
            merged = cache
        else:
            # the cache keeps history the sources no longer serve; fresh rows win where both exist
            keep = cache[~cache.index.isin(primary.index)] if not cache.empty else cache
            if not keep.empty:
                ok, why = self._agree(primary["close"], cache["close"], inst.additive)
                if not ok and not cache[cache.index.isin(primary.index)].empty:
                    notes.append(f"cache disagrees with fresh data ({why}); cache rows outside the fresh window kept")
            # an empty cache has object columns; pandas 3 would turn every merged column into object
            merged = pd.concat([keep, primary]).sort_index() if not keep.empty else primary.sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
            self.write_cache(inst.symbol, merged)
        if used:
            notes.insert(0, "sources: " + ", ".join(used))
        merged = self._cut(merged)
        notes += quality_issues(merged, inst.additive)
        return Series(inst.symbol, merged, notes, spread)

    def _enough(self, df: pd.DataFrame) -> bool:
        return (df.index[-1] - df.index[0]).days / 365.25 >= self.years - 0.1

    def _cut(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.asof is not None:
            df = df[df.index <= self.asof]
        return df

    def _load_demo(self, inst: Inst) -> Series:
        if self._demo is None:
            end = self.asof if self.asof is not None else pd.Timestamp.now().normalize()
            self._demo = DemoData(end)
        spec_syms = [s.get("demo") for s in inst.sources if "demo" in s]
        sym = spec_syms[0] if spec_syms else inst.symbol
        try:
            df = self._demo.bars(sym, inst.additive)
        except KeyError:
            return Series(inst.symbol, pd.DataFrame(columns=COLS), ["demo: no synthetic series"])
        df = clean_daily(df, "demo")
        spread = float(df["close"].iloc[-1]) * (0.00008 if inst.is_fx else 0.0002)
        return Series(inst.symbol, self._cut(df), ["demo data: synthetic, never use for decisions"], spread)


def returns(s: Series, additive: bool) -> pd.Series:
    c = s.close.astype(float)
    return c.diff() if additive else np.log(c).diff()


def synth_cross(a: Series, b: Series, sym: str) -> Series:
    """A cross built from two dollar legs (closes only), e.g. EURJPY = EURUSD * USDJPY."""
    j = pd.concat([a.close.rename("a"), b.close.rename("b")], axis=1, join="inner").dropna()
    df = clean_daily(pd.DataFrame({"close": j["a"] * j["b"]}), "synthetic", close_only=True)
    return Series(sym, df, [f"synthetic from {a.symbol} x {b.symbol} closes"])
