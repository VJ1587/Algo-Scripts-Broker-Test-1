"""
BROKER-v1.1 trading algorithm: single file research and backtest script.

Strategy owner: Vince Jackson. Research specification, NOT a validated profitable system.
Instrument specs in placeholder_catalog() are ILLUSTRATIVE: replace with your broker's data.

Requirements:  pip install pandas numpy

Usage:
    python broker_v11_algo.py --selftest                 # worked example and Section 8 checks
    python broker_v11_algo.py --demo --route both        # synthetic data, plumbing check only
    python broker_v11_algo.py --data-dir ./quotes --route L --news news.csv --out results

--data-dir: one CSV per symbol (EURUSD.csv, XAUUSD.csv, ...) with columns
    timestamp (bar open, UTC), bid_open, bid_high, bid_low, bid_close,
    ask_open, ask_high, ask_low, ask_close      (5 to 15 minute bars recommended)
--news: CSV with event_time, currency, impact, published_at (all UTC)
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

# ==============================================================================
# CONFIG
# ==============================================================================
"""
BROKER-v1.1 configuration.

Every number the spec labels "Confirmed" or "Test default" lives here, so a change
to any one of them produces a new fingerprint (spec: Parameter governance).
Do not edit defaults in place for an experiment. Create a new config with
dataclasses.replace(cfg, variant="my_variant_name", field=value).
"""


STRATEGY_VERSION = "BROKER-v1.1"


@dataclass(frozen=True)
class StrategyConfig:
    version: str = STRATEGY_VERSION
    variant: str = "baseline"
    route: str = "L"  # "L" = three leg ladder, "M" = confirmed market entry. Never both (Route isolation).

    # ---- Confirmed risk ceilings (Section 7). Never optimize these upward. ----
    idea_risk_pct: Decimal = Decimal("0.02")
    portfolio_risk_pct: Decimal = Decimal("0.05")

    # ---- Confirmed ladder, stop and target geometry (Sections 5, 6) ----
    ladder_ratios: tuple = (Decimal("0.382"), Decimal("0.5"), Decimal("0.618"))
    ladder_weights: tuple = (Decimal("0.20"), Decimal("0.30"), Decimal("0.50"))
    tp1_extension: Decimal = Decimal("0.27")
    tp2_extension: Decimal = Decimal("0.618")
    fx_stop_ratio: Decimal = Decimal("0.893")
    tp1_close_fraction: Decimal = Decimal("0.5")

    # ---- Data (Section 2) ----
    warmup_bars: int = 250
    max_quote_age_seconds: int = 5  # live trading guard only; backtests use their own clock

    # ---- Feature test defaults (Sections 2 to 4) ----
    pivot_width: int = 2
    daily_ema_periods: tuple = (20, 50)
    h2_ema_periods: tuple = (8, 14)
    atr_period: int = 14
    impulse_recent_4h_bars: int = 6
    impulse_min_span: int = 3
    impulse_max_span: int = 30
    impulse_min_atr_multiple: float = 2.0
    impulse_min_efficiency: float = 0.60
    zone_atr_fraction: float = 0.10
    zone_min_ticks: int = 2
    min_confluence_score: int = 3
    engulf_body_atr: float = 0.25
    rejection_range_atr: float = 0.50

    # ---- Execution and management test defaults (Sections 5, 6) ----
    structural_stop_atr: float = 0.10     # gold and SPX500 only
    trail_atr: float = 0.10
    market_max_adverse_atr: float = 0.10
    ladder_expiry_4h_bars: int = 6
    long_only_symbols: tuple = ("XAUUSD", "SPX500")
    limit_fill_through_ticks: int = 0     # 0 = fill when ask/bid touches the limit

    # ---- News filter (Section 9) ----
    news_filter: bool = True
    news_window_minutes: int = 30

    # ---- Broker margin and costs (Section 7) ----
    margin_safety_multiple: Decimal = Decimal("2")
    planned_hold_days: Decimal = Decimal("5")   # financing allowance used in sizing
    cost_multiplier: Decimal = Decimal("1")     # set to 2 for the doubled cost stress test

    def fingerprint(self) -> str:
        """Short hash of every parameter. Logged in the journal for traceability."""
        blob = json.dumps(asdict(self), default=str, sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


@dataclass
class AccountConfig:
    """
    Independent state per brokerage account (Section 1, Account records).
    The spec calls the accounts "Forex broker" and "Magno broker"; their exact
    legal identities and platforms must be confirmed before live connection.
    """
    account_id: str
    label: str
    currency: str = "USD"
    starting_balance: Decimal = Decimal("100000")
    stop_out_pct: Decimal | None = None   # broker stop-out margin level, in percent
    venue_identity: str | None = None
    platform: str | None = None
    verified: bool = False                # True only after broker details are confirmed

    def validate(self) -> None:
        missing = [name for name in ("stop_out_pct",) if getattr(self, name) is None]
        if missing:
            raise MetadataError(f"account {self.account_id}: missing account metadata {missing}")


# ==============================================================================
# INSTRUMENTS
# ==============================================================================
"""
Instrument metadata, decimal precision rules and currency conversion (Sections 1, 2).

IMPORTANT: placeholder_catalog() values are ILLUSTRATIVE ONLY. They are not your
brokers' contract specs. Replace every field with the venue's published symbol
specification before trusting any result. Results produced with placeholders are
labelled "indicative" and do not qualify for deployment.
"""



class MetadataError(Exception):
    """Raised when required instrument or account metadata is missing. Blocks order creation."""


def D(x) -> Decimal:
    """Convert to Decimal without binary float noise (repr gives the shortest exact form)."""
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(repr(float(x)))  # float() strips numpy wrappers
    return Decimal(str(x))


_ROUNDING = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_UP}


def round_price(price: Decimal, tick: Decimal, mode: str) -> Decimal:
    """Round to a whole number of ticks. mode: 'down', 'up' or 'nearest'."""
    steps = (D(price) / tick).quantize(Decimal(1), rounding=_ROUNDING[mode])
    return steps * tick


def round_qty_down(qty: Decimal, step: Decimal) -> Decimal:
    """Quantities are always rounded down to the venue increment (Section 7)."""
    return (D(qty) / step).quantize(Decimal(1), rounding=ROUND_FLOOR) * step


def psych_distance(price: float, grid_mid: float) -> float:
    """Distance to the nearest psychological level. Majors are multiples of the
    midpoint spacing, so the nearest midpoint multiple covers both (Z01)."""
    n = round(price / grid_mid)
    return abs(price - n * grid_mid)


def in_psych_zone(price: float, grid_mid: float, half_width: float) -> bool:
    return psych_distance(price, grid_mid) <= half_width + 1e-12


def check_ladder_levels(entries, grid_mid: float, half_width: float):
    """Baseline ladder rule: if ANY leg misses a psychological zone, skip the whole ladder.
    Returns (ok, list_of_failing_prices)."""
    failing = [e for e in entries if not in_psych_zone(float(e), grid_mid, half_width)]
    return (len(failing) == 0, failing)


@dataclass
class InstrumentSpec:
    symbol: str                        # canonical strategy label, e.g. XAUUSD
    asset_class: str                   # "FX", "METAL" or "INDEX"
    grid_major: Decimal
    grid_mid: Decimal
    venue_symbol: str | None = None    # exact broker symbol
    product_type: str | None = None    # spot FX, CFD, etc. Futures need a separate adapter.
    base_ccy: str | None = None
    quote_ccy: str | None = None
    tick_size: Decimal | None = None
    price_digits: int | None = None
    contract_size: Decimal | None = None       # quote currency P/L per 1.0 price move per lot
    min_qty: Decimal | None = None
    max_qty: Decimal | None = None
    qty_step: Decimal | None = None
    min_stop_distance: Decimal | None = None   # price units
    margin_rate: Decimal | None = None         # fraction of notional
    commission_per_lot_rt: Decimal | None = None  # account currency, round trip
    stop_slippage_ticks: int | None = None
    financing_per_lot_day: Decimal | None = None  # account currency, positive = cost
    cost_data_verified: bool = False

    REQUIRED = (
        "venue_symbol", "product_type", "base_ccy", "quote_ccy", "tick_size", "price_digits",
        "contract_size", "min_qty", "max_qty", "qty_step", "min_stop_distance", "margin_rate",
        "commission_per_lot_rt", "stop_slippage_ticks", "financing_per_lot_day",
    )

    def validate(self) -> None:
        missing = [f for f in self.REQUIRED if getattr(self, f) is None]
        if missing:
            raise MetadataError(f"{self.symbol}: missing instrument metadata {missing}")

    @property
    def currencies(self) -> tuple:
        """Currencies whose high impact news blocks entries (Section 9)."""
        if self.asset_class == "FX":
            return (self.base_ccy, self.quote_ccy)
        return ("USD",)


class FxConverter:
    """Converts quote currency amounts to account currency using the latest known mid prices."""

    def __init__(self, account_ccy: str = "USD"):
        self.account_ccy = account_ccy
        self.mid: dict[str, float] = {}

    def update(self, symbol: str, mid: float) -> None:
        self.mid[symbol] = mid

    def to_account(self, ccy: str) -> Decimal:
        if ccy == self.account_ccy:
            return Decimal(1)
        direct, inverse = f"{ccy}{self.account_ccy}", f"{self.account_ccy}{ccy}"
        if direct in self.mid:
            return D(self.mid[direct])
        if inverse in self.mid:
            return Decimal(1) / D(self.mid[inverse])
        raise MetadataError(f"no {ccy} to {self.account_ccy} conversion rate available")


FX_STANDARD = ("AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "GBPAUD")
FX_JPY = ("EURJPY", "USDJPY")


def placeholder_catalog() -> dict[str, InstrumentSpec]:
    """Illustrative specs so the demo runs. NOT broker data. Replace before use."""
    cat: dict[str, InstrumentSpec] = {}
    for sym in FX_STANDARD + FX_JPY:
        jpy = sym in FX_JPY
        cat[sym] = InstrumentSpec(
            symbol=sym, asset_class="FX",
            grid_major=D("5.00" if jpy else "0.0500"), grid_mid=D("2.50" if jpy else "0.0250"),
            venue_symbol=sym, product_type="spot_fx_PLACEHOLDER",
            base_ccy=sym[:3], quote_ccy=sym[3:],
            tick_size=D("0.001" if jpy else "0.00001"), price_digits=3 if jpy else 5,
            contract_size=D("100000"), min_qty=D("0.01"), max_qty=D("50"), qty_step=D("0.01"),
            min_stop_distance=D("0"), margin_rate=D("0.0333"),
            commission_per_lot_rt=D("7"), stop_slippage_ticks=5, financing_per_lot_day=D("1"),
        )
    cat["XAUUSD"] = InstrumentSpec(
        symbol="XAUUSD", asset_class="METAL", grid_major=D("100"), grid_mid=D("50"),
        venue_symbol="XAUUSD", product_type="cfd_PLACEHOLDER", base_ccy="XAU", quote_ccy="USD",
        tick_size=D("0.01"), price_digits=2, contract_size=D("100"),
        min_qty=D("0.01"), max_qty=D("50"), qty_step=D("0.01"), min_stop_distance=D("0"),
        margin_rate=D("0.05"), commission_per_lot_rt=D("7"), stop_slippage_ticks=20,
        financing_per_lot_day=D("5"),
    )
    cat["SPX500"] = InstrumentSpec(
        symbol="SPX500", asset_class="INDEX", grid_major=D("100"), grid_mid=D("50"),
        venue_symbol="SPX500", product_type="cfd_PLACEHOLDER", base_ccy="SPX", quote_ccy="USD",
        tick_size=D("0.1"), price_digits=1, contract_size=D("1"),
        min_qty=D("0.1"), max_qty=D("500"), qty_step=D("0.1"), min_stop_distance=D("0"),
        margin_rate=D("0.05"), commission_per_lot_rt=D("0"), stop_slippage_ticks=5,
        financing_per_lot_day=D("1"),
    )
    return cat


# ==============================================================================
# DATA
# ==============================================================================
"""
Market data adapter (Section 2).

Input: one CSV per symbol of small interval bars (for example 5 or 15 minute) with
bid and ask OHLC. Columns:
    timestamp (bar OPEN time, UTC), bid_open, bid_high, bid_low, bid_close,
    ask_open, ask_high, ask_low, ask_close
If ask columns are missing you may supply a 'spread' column (price units).

Daily, 4H and 2H bars are built from the same bid stream, anchored at 00:00 UTC,
half open intervals, and become available only at their closing timestamp.
"""


QCOLS = ["bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"]


class DataError(Exception):
    pass


def to_ns(index_like) -> np.ndarray:
    """UTC timestamps as int64 nanoseconds (pandas 3 may default to microseconds)."""
    return pd.DatetimeIndex(index_like).as_unit("ns").asi8


def infer_interval(index: pd.DatetimeIndex) -> pd.Timedelta:
    diffs = pd.Series(index[1:] - index[:-1])
    interval = diffs.mode().iloc[0]
    if pd.Timedelta("2h") % interval != pd.Timedelta(0):
        raise DataError(f"base interval {interval} must divide 2 hours evenly")
    return interval


def load_quotes_csv(path: str, max_gap: str = "3h") -> pd.DataFrame:
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp")
    if "ask_open" not in df.columns:
        if "spread" not in df.columns:
            raise DataError(f"{path}: needs ask_* columns or a spread column")
        for f in ("open", "high", "low", "close"):
            df[f"ask_{f}"] = df[f"bid_{f}"] + df["spread"]
    return validate_quotes(df[QCOLS].astype(float), max_gap=max_gap)


def validate_quotes(df: pd.DataFrame, max_gap: str = "3h", holidays: set | None = None) -> pd.DataFrame:
    """Rejects duplicates, impossible OHLC, crossed quotes and unexpected gaps.
    Weekend closures are expected. Add known holiday dates (datetime.date) to holidays."""
    if df.index.tz is None:
        raise DataError("timestamps must be timezone aware UTC")
    df = df.sort_index()
    if df.index.has_duplicates:
        raise DataError(f"{int(df.index.duplicated().sum())} duplicate timestamps")
    if df[QCOLS].isna().any().any():
        raise DataError("missing price values")
    for side in ("bid", "ask"):
        o, h, l, c = (df[f"{side}_{f}"] for f in ("open", "high", "low", "close"))
        bad = (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l)
        if bad.any():
            raise DataError(f"{int(bad.sum())} impossible {side} OHLC rows, first at {df.index[bad.values][0]}")
    crossed = (df["ask_open"] < df["bid_open"]) | (df["ask_close"] < df["bid_close"])
    if crossed.any():
        raise DataError(f"{int(crossed.sum())} crossed quotes, first at {df.index[crossed.values][0]}")
    gaps = df.index[1:] - df.index[:-1]
    holidays = holidays or set()
    for i in np.where(gaps > pd.Timedelta(max_gap))[0]:
        start, end = df.index[i], df.index[i + 1]
        spans_weekend = start.weekday() >= 4 and end.weekday() in (6, 0) and (end - start) < pd.Timedelta("4D")
        if not spans_weekend and start.date() not in holidays and end.date() not in holidays:
            raise DataError(f"unexpected gap {start} to {end}; add to holidays if it is a known closure")
    return df


def resample_quotes(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a higher timeframe anchored at 00:00 UTC. No synthetic empty candles."""
    agg = {
        "bid_open": "first", "bid_high": "max", "bid_low": "min", "bid_close": "last",
        "ask_open": "first", "ask_high": "max", "ask_low": "min", "ask_close": "last",
    }
    out = df.resample(rule, origin="epoch", label="left", closed="left").agg(agg).dropna()
    out["close_time"] = out.index + pd.Timedelta(rule)
    return out


def synthetic_quotes(start: str = "2021-01-03 22:00", years: float = 4.0, interval_min: int = 30,
                     start_price: float = 1.10, annual_vol: float = 0.08, spread: float = 0.00010,
                     decimals: int = 5, seed: int = 7) -> pd.DataFrame:
    """Regime switching random walk for plumbing tests ONLY. It contains no real market
    information, so any profit or loss on it says nothing about the strategy's edge."""
    rng = np.random.default_rng(seed)
    sub = 5
    idx = pd.date_range(start, periods=int(years * 365 * 24 * 60 / sub), freq=f"{sub}min", tz="UTC")
    wd, hr = idx.weekday, idx.hour
    open_mask = ~((wd == 5) | ((wd == 4) & (hr >= 21)) | ((wd == 6) & (hr < 22)))
    idx = idx[open_mask]
    n = len(idx)
    sigma = annual_vol / np.sqrt(252 * 24 * 60 / sub)
    regime_len = rng.integers(300, 3000, size=n // 300 + 2)
    drift = np.repeat(rng.normal(0, 0.025, size=len(regime_len)), regime_len)[:n] * sigma
    smooth = np.convolve(rng.standard_normal(n), np.ones(2000) / np.sqrt(2000), mode="same")
    vol_scale = np.exp(0.25 * smooth)  # mean reverting volatility clusters
    vol_scale = vol_scale / vol_scale.mean()
    rets = drift + sigma * vol_scale * rng.standard_normal(n)
    mid = start_price * np.exp(np.cumsum(rets))
    s = pd.Series(mid, index=idx)
    bars = s.resample(f"{interval_min}min", origin="epoch").ohlc().dropna()
    out = pd.DataFrame(index=bars.index)
    half = spread / 2
    for f in ("open", "high", "low", "close"):
        out[f"bid_{f}"] = (bars[f] - half).round(decimals)
        out[f"ask_{f}"] = (bars[f] + half).round(decimals)
    return out[QCOLS]


# ==============================================================================
# FEATURES
# ==============================================================================
"""
Feature engine (Sections 2 to 4). Everything here is causal: each object is fed one
COMPLETED bar at a time and never looks at a bar that has not closed yet.
"""



# --------------------------------------------------------------------------- indicators
class EMA:
    """alpha = 2/(n+1), seeded with the arithmetic mean of the first n closes."""

    def __init__(self, n: int):
        self.n, self.alpha = n, 2.0 / (n + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self.value is None:
            self._seed.append(close)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
        else:
            self.value = self.alpha * close + (1 - self.alpha) * self.value
        return self.value


class WilderATR:
    """ATR14(t) = [13 x ATR14(t-1) + TR(t)] / 14, seeded with the first 14 valid true ranges.
    The first bar has no previous close, so it has no valid TR."""

    def __init__(self, n: int = 14):
        self.n = n
        self.prev_close: float | None = None
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is not None:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
            if self.value is None:
                self._seed.append(tr)
                if len(self._seed) == self.n:
                    self.value = sum(self._seed) / self.n
            else:
                self.value = ((self.n - 1) * self.value + tr) / self.n
        self.prev_close = close
        if self.value is not None and self.value <= 0:
            return None  # zero ATR means skip computations (Section 2)
        return self.value


# --------------------------------------------------------------------------- pivots
@dataclass(frozen=True)
class Pivot:
    kind: str          # "H" swing high or "L" swing low
    idx: int           # bar index where the pivot occurred
    price: float       # wick high or low
    time_ns: int       # open time of the pivot bar
    confirm_idx: int   # bar whose close confirmed it (idx + width)
    confirm_ns: int    # usable from this timestamp only


class PivotTracker:
    """Strict pivots with `width` bars on each side. A pivot at bar k is usable only at the
    close of bar k+width. Ties do not qualify; a bar that is both high and low is discarded.
    Maintains an alternating H/L sequence: consecutive same type keeps the more extreme
    (the earlier one on equal prices)."""

    def __init__(self, width: int = 2):
        self.width = width
        self.alt: list[Pivot] = []

    def update(self, highs, lows, open_ns, close_ns) -> list[tuple[Pivot, bool]]:
        """Call after appending the newest completed bar. Returns [(pivot, appended_to_alt)]."""
        w = self.width
        j = len(highs) - 1
        k = j - w
        if k - w < 0:
            return []
        neighbours = [i for i in range(k - w, k + w + 1) if i != k]
        is_high = all(highs[k] > highs[i] for i in neighbours)
        is_low = all(lows[k] < lows[i] for i in neighbours)
        if is_high == is_low:  # neither, or both (both are discarded)
            return []
        kind = "H" if is_high else "L"
        p = Pivot(kind, k, highs[k] if is_high else lows[k], open_ns[k], j, close_ns[j])
        return [(p, self._add(p))]

    def _add(self, p: Pivot) -> bool:
        if self.alt and self.alt[-1].kind == p.kind:
            last = self.alt[-1]
            more_extreme = p.price > last.price if p.kind == "H" else p.price < last.price
            if more_extreme:
                self.alt[-1] = p
            return False
        self.alt.append(p)
        return True


class TFState:
    """Rolling state for one timeframe of one symbol."""

    def __init__(self, name: str, ema_periods, atr_period: int, pivot_width: int):
        self.name = name
        self.o, self.h, self.l, self.c = [], [], [], []
        self.ask_h, self.ask_l = [], []
        self.open_ns, self.close_ns = [], []
        self.emas = {n: EMA(n) for n in ema_periods}
        self.atr = WilderATR(atr_period)
        self.atr_hist: list[float | None] = []
        self.pivots = PivotTracker(pivot_width)

    def add_bar(self, o, h, l, c, ask_h, ask_l, open_ns, close_ns):
        self.o.append(o); self.h.append(h); self.l.append(l); self.c.append(c)
        self.ask_h.append(ask_h); self.ask_l.append(ask_l)
        self.open_ns.append(open_ns); self.close_ns.append(close_ns)
        for e in self.emas.values():
            e.update(c)
        self.atr_hist.append(self.atr.update(h, l, c))
        return self.pivots.update(self.h, self.l, self.open_ns, self.close_ns)

    @property
    def n(self) -> int:
        return len(self.c)

    @property
    def atr_now(self) -> float | None:
        return self.atr_hist[-1] if self.atr_hist else None

    def ema(self, period: int) -> float | None:
        return self.emas[period].value

    def ready(self, warmup: int) -> bool:
        return (self.n >= warmup and self.atr_now is not None
                and all(e.value is not None for e in self.emas.values()))

    def latest(self, kind: str) -> Pivot | None:
        for p in reversed(self.pivots.alt):
            if p.kind == kind:
                return p
        return None

    def last_two(self, kind: str) -> list[Pivot]:
        out = [p for p in self.pivots.alt if p.kind == kind][-2:]
        return out if len(out) == 2 else []

    def structure(self) -> int:
        """+1 when the latest two confirmed highs AND lows are rising, -1 when both falling, else 0."""
        hs, ls = self.last_two("H"), self.last_two("L")
        if not hs or not ls:
            return 0
        if hs[1].price > hs[0].price and ls[1].price > ls[0].price:
            return 1
        if hs[1].price < hs[0].price and ls[1].price < ls[0].price:
            return -1
        return 0


# --------------------------------------------------------------------------- impulse (I01)
@dataclass(frozen=True)
class Impulse:
    direction: int
    A: Pivot
    B: Pivot
    prior_extreme: Pivot
    atr_at_B: float
    efficiency: float
    span: int

    @property
    def setup_id(self) -> str:
        return f"{self.direction:+d}:{self.A.time_ns}:{self.B.time_ns}"

    def fib(self, ratio: float) -> float:
        return self.B.price - ratio * (self.B.price - self.A.price)


def find_impulse(tf4: TFState, direction: int, cfg) -> tuple[Impulse | None, str]:
    """Latest qualifying 4H impulse in `direction`. Returns (impulse or None, reason)."""
    alt = tf4.pivots.alt
    b_kind = "H" if direction == 1 else "L"
    current = tf4.n - 1
    reason = "no recent B pivot"
    for i in range(len(alt) - 1, 1, -1):
        B = alt[i]
        if B.kind != b_kind:
            continue
        if B.confirm_idx < current - (cfg.impulse_recent_4h_bars - 1):
            break
        A, prior = alt[i - 1], alt[i - 2]
        span = B.idx - A.idx
        if not cfg.impulse_min_span <= span <= cfg.impulse_max_span:
            reason = "impulse span outside limits"; continue
        atr_b = tf4.atr_hist[B.idx]
        if not atr_b:
            reason = "ATR unavailable at B"; continue
        if abs(B.price - A.price) < cfg.impulse_min_atr_multiple * atr_b:
            reason = "impulse smaller than ATR multiple"; continue
        denom = sum(abs(tf4.c[j] - tf4.c[j - 1]) for j in range(A.idx + 1, B.idx + 1))
        if denom == 0:
            reason = "zero efficiency denominator"; continue
        eff = abs(tf4.c[B.idx] - tf4.c[A.idx]) / denom
        if eff < cfg.impulse_min_efficiency:
            reason = "impulse efficiency too low"; continue
        if (B.price - prior.price) * direction <= 0:
            reason = "B does not break prior swing"; continue
        return Impulse(direction, A, B, prior, atr_b, eff, span), "ok"
    return None, reason


# --------------------------------------------------------------------------- candles (C4)
def candle_signal(tf2: TFState, direction: int, tick: float, cfg) -> bool:
    """Directional engulfing or rejection on the just completed 2H bar."""
    if tf2.n < 2 or not tf2.atr_now:
        return False
    atr = tf2.atr_now
    po, pc = tf2.o[-2], tf2.c[-2]
    o, h, l, c = tf2.o[-1], tf2.h[-1], tf2.l[-1], tf2.c[-1]
    rng = h - l
    if direction == 1:
        engulf = pc < po and c > o and o <= pc and c >= po and (c - o) >= cfg.engulf_body_atr * atr
        body = c - o
        reject = (rng > 0 and body >= tick and (o - l) >= 2 * body and (h - c) <= body
                  and c >= l + 0.75 * rng and rng >= cfg.rejection_range_atr * atr)
    else:
        engulf = pc > po and c < o and o >= pc and c <= po and (o - c) >= cfg.engulf_body_atr * atr
        body = o - c
        reject = (rng > 0 and body >= tick and (h - o) >= 2 * body and (c - l) <= body
                  and c <= l + 0.25 * rng and rng >= cfg.rejection_range_atr * atr)
    return bool(engulf or reject)


# --------------------------------------------------------------------------- trend line (C6)
def trendline_ok(tf4: TFState, tf2: TFState, direction: int, zone_w: float) -> bool:
    kind = "L" if direction == 1 else "H"
    anchors = tf4.last_two(kind)
    if not anchors:
        return False
    a1, a2 = anchors
    if (a2.price - a1.price) * direction <= 0 or a2.time_ns == a1.time_ns:
        return False

    def line(t_ns: int) -> float:
        return a1.price + (a2.price - a1.price) * (t_ns - a1.time_ns) / (a2.time_ns - a1.time_ns)

    for i in range(tf2.n - 1, -1, -1):
        if tf2.close_ns[i] <= a2.confirm_ns:
            break
        lv = line(tf2.close_ns[i])
        if (lv - tf2.c[i]) * direction > zone_w:  # close broke the line beyond zone width
            return False
    lv = line(tf2.close_ns[-1])
    touches = tf2.l[-1] <= lv + zone_w and tf2.h[-1] >= lv - zone_w
    trend_side = (tf2.c[-1] - lv) * direction >= 0
    return touches and trend_side


# ==============================================================================
# RISK
# ==============================================================================
"""
Risk / account engine (Section 7). All money math uses Decimal.
"""



ZERO = Decimal(0)


def capital_base(balance: Decimal, equity: Decimal) -> Decimal:
    """K = min(balance, equity). Floating gains cannot enlarge the budget."""
    return min(balance, equity)


def allowed_idea_risk(K: Decimal, used_risk: Decimal, cfg) -> Decimal:
    """Smaller of 2% of K and whatever remains under the 5% portfolio ceiling."""
    if K <= 0:
        return ZERO
    available = max(ZERO, cfg.portfolio_risk_pct * K - used_risk)
    return min(cfg.idea_risk_pct * K, available)


def cost_allowance_per_lot(spec: InstrumentSpec, V: Decimal, cfg) -> Decimal:
    """Commission + expected stop slippage + planned financing. Spread is NOT added here
    because it is already inside the executable bid/ask prices."""
    slippage = Decimal(spec.stop_slippage_ticks) * spec.tick_size * V
    financing = spec.financing_per_lot_day * cfg.planned_hold_days
    return (spec.commission_per_lot_rt + slippage + financing) * cfg.cost_multiplier


def loss_per_lot(entry: Decimal, stop: Decimal, V: Decimal, cost: Decimal) -> Decimal:
    return abs(entry - stop) * V + cost


@dataclass
class SizingResult:
    ok: bool
    quantities: list = field(default_factory=list)
    planned_risk: Decimal = ZERO
    losses_per_lot: list = field(default_factory=list)
    reason: str = ""


def _finish(qtys, losses, spec) -> SizingResult:
    if any(q < spec.min_qty for q in qtys):
        return SizingResult(False, qtys, ZERO, losses, "leg below minimum quantity")
    if any(q > spec.max_qty for q in qtys):
        return SizingResult(False, qtys, ZERO, losses, "leg above maximum quantity")
    risk = sum((q * l for q, l in zip(qtys, losses)), ZERO)
    return SizingResult(True, qtys, risk, losses)


def size_ladder(entries, weights, stop, V, cost, allowed_risk, spec) -> SizingResult:
    """TotalLots = AllowedIdeaRisk / WeightedLoss, split by weight, each rounded DOWN.
    Never round up or redistribute to reach exactly 2%."""
    if allowed_risk <= 0:
        return SizingResult(False, reason="no risk budget available")
    losses = [loss_per_lot(e, stop, V, cost) for e in entries]
    weighted = sum((w * l for w, l in zip(weights, losses)), ZERO)
    if weighted <= 0:
        return SizingResult(False, reason="non positive weighted loss")
    total = allowed_risk / weighted
    qtys = [round_qty_down(total * w, spec.qty_step) for w in weights]
    return _finish(qtys, losses, spec)


def size_market(entry, stop, V, cost, allowed_risk, spec) -> SizingResult:
    if allowed_risk <= 0:
        return SizingResult(False, reason="no risk budget available")
    loss = loss_per_lot(entry, stop, V, cost)
    return _finish([round_qty_down(allowed_risk / loss, spec.qty_step)], [loss], spec)


def open_leg_risk(direction: int, entry: Decimal, stop: Decimal, liq_price: Decimal,
                  qty: Decimal, V: Decimal, remaining_cost_per_lot: Decimal) -> Decimal:
    """Larger of (entry to stop) and (current liquidation price to stop), never negative.
    Keeps protected floating profit visible as risk to current equity."""
    loss_from_entry = (entry - stop) * direction
    giveback = (liq_price - stop) * direction
    return max(ZERO, loss_from_entry, giveback) * qty * V + remaining_cost_per_lot * qty


def pending_reserved_risk(entry: Decimal, stop: Decimal, qty: Decimal, V: Decimal, cost: Decimal) -> Decimal:
    return (abs(entry - stop) * V + cost) * qty


def margin_per_lot(spec: InstrumentSpec, price: Decimal, quote_to_account: Decimal) -> Decimal:
    return spec.contract_size * price * quote_to_account * spec.margin_rate


def margin_level(equity: Decimal, used_margin: Decimal) -> Decimal | None:
    """100 x equity / used margin. None means no margin in use (no constraint)."""
    if used_margin <= 0:
        return None
    return Decimal(100) * equity / used_margin


def projected_margin_ok(equity, used_margin, used_risk, new_margin, new_risk,
                        stop_out_pct, safety_multiple) -> bool:
    """After every planned stop loss and cost, margin level must stay at or above
    safety_multiple x the broker stop-out level."""
    level = margin_level(equity - used_risk - new_risk, used_margin + new_margin)
    return level is None or level >= safety_multiple * stop_out_pct


# ==============================================================================
# OVERLAYS
# ==============================================================================
"""
News filter (Section 9) and COT research layer (Section 10).

COT values are computed for LOGGING ONLY in BROKER-v1.1. They never change a
baseline trade. Wire your existing scraper output in only after its schema,
timestamps and sample records have been inspected.
"""




class NewsCalendar:
    """CSV columns: event_time (UTC), currency (e.g. USD), impact (high/medium/low),
    published_at (UTC time the calendar entry was knowable). Using published_at stops
    the backtest from knowing about events before they were announced."""

    def __init__(self, df: pd.DataFrame, window_minutes: int = 30):
        df = df[df["impact"].str.lower() == "high"].copy()
        self.event_ns = to_ns(pd.to_datetime(df["event_time"], utc=True))
        self.pub_ns = to_ns(pd.to_datetime(df["published_at"], utc=True))
        self.ccy = df["currency"].str.upper().to_numpy()
        self.window_ns = window_minutes * 60 * 1_000_000_000

    @classmethod
    def from_csv(cls, path: str, window_minutes: int = 30) -> "NewsCalendar":
        return cls(pd.read_csv(path), window_minutes)

    def in_blackout(self, currencies, t_ns: int) -> bool:
        near = np.abs(self.event_ns - t_ns) <= self.window_ns
        known = self.pub_ns <= t_ns
        relevant = np.isin(self.ccy, list(currencies))
        return bool(np.any(near & known & relevant))


# ------------------------------------------------------------------ COT (logging only)
def cot_features(df: pd.DataFrame) -> pd.DataFrame:
    """Input: one market, one report family, one trader category, one futures-only/combined
    flag, sorted by position_date, with columns longs, shorts, open_interest, published_at.
    Never mix report categories or futures-only with combined totals."""
    out = df.copy()
    out["net"] = out["longs"] - out["shorts"]
    out["weekly_change"] = out["net"].diff()
    out["net_share"] = np.where(out["open_interest"] > 0, out["net"] / out["open_interest"], np.nan)
    lo = out["net"].rolling(52, min_periods=52).min()
    hi = out["net"].rolling(52, min_periods=52).max()
    rng = hi - lo
    out["cot_index"] = np.where((rng > 0) & (out["open_interest"] > 0), 100 * (out["net"] - lo) / rng, np.nan)
    return out


def cot_as_of(cot: pd.DataFrame, decision_time: pd.Timestamp, stale_days: int = 10):
    """Latest report PUBLISHED (not position-dated) at or before decision time.
    Returns (row or None, is_stale)."""
    avail = cot[pd.to_datetime(cot["published_at"], utc=True) <= decision_time]
    if avail.empty:
        return None, True
    row = avail.iloc[-1]
    age = decision_time - pd.to_datetime(row["published_at"], utc=True)
    return row, age > pd.Timedelta(days=stale_days)


def cot_crowding(cot_index: float) -> str | None:
    """Experiment hypothesis only (not an established threshold): >=90 long crowded, <=10 short crowded."""
    if cot_index is None or np.isnan(cot_index):
        return None
    if cot_index >= 90:
        return "long_crowded"
    if cot_index <= 10:
        return "short_crowded"
    return None


# ==============================================================================
# REPORTING
# ==============================================================================
"""
Portfolio / reporting (Sections 11, 12): journal rows, scorecard formulas, rejection counts.
"""



TS = lambda ns: pd.Timestamp(ns, tz="UTC") if ns is not None else None  # noqa: E731


@dataclass
class BacktestResult:
    journal: pd.DataFrame
    events: pd.DataFrame
    equity: pd.DataFrame
    rejections: dict
    scorecard: dict
    labels: list = field(default_factory=list)


def journal_row(bt, idea) -> dict:
    filled = [l for l in idea.legs if l.filled_qty > 0]
    realized = sum(float(l.realized) for l in idea.legs)
    costs = sum(float(l.costs) for l in idea.legs) + float(idea.financing)
    net = realized - costs
    filled_risk = sum(float(l.filled_qty * (abs(l.fill_price - idea.plan.stop) * l.V + l.cost_per_lot))
                      for l in filled)
    row = {
        "account": bt.account.account_id, "strategy_version": bt.cfg.version,
        "variant": bt.cfg.variant, "config_fingerprint": bt.cfg.fingerprint(),
        "idea_id": idea.idea_id, "symbol": idea.symbol, "route": idea.route,
        "direction": idea.direction, "decision_time": TS(idea.created_ns),
        "first_fill_time": TS(min((l.fill_ns for l in filled), default=None)),
        "close_time": TS(idea.close_ns),
        **idea.confluences, "confluence_score": idea.score,
        "planned_entries": [float(e) for e in idea.plan.entries],
        "intended_qty": [float(l.planned_qty) for l in idea.legs],
        "filled_qty": float(idea.filled_qty()),
        "fill_prices": [float(l.fill_price) if l.fill_price is not None else None for l in idea.legs],
        "initial_stop": float(idea.plan.stop), "final_stop": float(idea.stop),
        "tp1": float(idea.plan.tp1), "tp2": float(idea.plan.tp2), "tp1_hit": idea.tp1_done,
        "order_ids": [l.leg_id for l in idea.legs],
        "cancel_reasons": [l.cancel_reason for l in idea.legs if l.cancel_reason],
        "exit_reason": idea.exit_reason, "ambiguous_bars": idea.ambiguous_bars,
        "gross_pnl": realized, "costs": costs, "financing": float(idea.financing), "net_pnl": net,
        "committed_risk": float(idea.committed_risk), "filled_risk": filled_risk,
        "R_committed": net / float(idea.committed_risk) if idea.committed_risk > 0 else np.nan,
        "R_filled": net / filled_risk if filled_risk > 0 else np.nan,
    }
    row.update(idea.context)
    return row


def scorecard(journal: pd.DataFrame, equity: pd.DataFrame, start_balance: float, events: pd.DataFrame) -> dict:
    sc: dict = {}
    if journal.empty:
        sc["closed_ideas"] = 0
        return sc
    traded = journal[(journal["filled_qty"] > 0) & (journal["exit_reason"] != "open_at_end")]
    legs_planned = journal["intended_qty"].apply(len).sum()
    legs_filled = journal["fill_prices"].apply(lambda xs: sum(x is not None for x in xs)).sum()
    sc["ideas_created"] = len(journal)
    sc["closed_ideas"] = len(traded)
    sc["open_at_end"] = int((journal["exit_reason"] == "open_at_end").sum())
    sc["leg_fill_rate"] = legs_filled / legs_planned if legs_planned else np.nan
    if len(traded):
        R = traded["R_committed"]
        wins, losses = traded[traded["net_pnl"] > 0], traded[traded["net_pnl"] <= 0]
        sc["expectancy_R_committed"] = R.mean()
        sc["expectancy_R_filled"] = traded["R_filled"].mean()
        gl = losses["net_pnl"].sum()
        sc["profit_factor"] = wins["net_pnl"].sum() / abs(gl) if gl < 0 else "undefined (no losses)"
        sc["win_rate"] = len(wins) / len(traded)
        sc["avg_win"] = wins["net_pnl"].mean() if len(wins) else np.nan
        sc["avg_loss"] = losses["net_pnl"].mean() if len(losses) else np.nan
        streak = best = 0
        for pnl in traded.sort_values("close_time")["net_pnl"]:
            streak = streak + 1 if pnl <= 0 else 0
            best = max(best, streak)
        sc["max_loss_streak"] = best
        sc["financing_cost"] = traded["financing"].sum()
        sc["ambiguous_bars"] = int(traded["ambiguous_bars"].sum())
        dur = (traded["close_time"] - traded["first_fill_time"]).dt.total_seconds().sum()
        span = (equity["time"].iloc[-1] - equity["time"].iloc[0]).total_seconds() if len(equity) > 1 else np.nan
        sc["exposure_time_fraction_sum"] = dur / span if span else np.nan
    if len(equity):
        eq = equity["equity"].astype(float)
        peak = eq.cummax()
        sc["net_return"] = eq.iloc[-1] / start_balance - 1
        sc["max_drawdown"] = ((peak - eq) / peak).max()
    if not events.empty:
        sc["broker_stop_outs"] = int((events["event"] == "BROKER_STOP_OUT").sum())
        sc["margin_safety_events"] = int((events["event"] == "MARGIN_SAFETY").sum())
        sc["risk_cap_breaches"] = int((events["event"] == "RISK_CAP_BREACH").sum())
    if sc["closed_ideas"] < 100:
        sc["evidence_note"] = "fewer than 100 closed ideas: insufficient evidence, not pass or fail"
    return sc


def build_result(bt) -> BacktestResult:
    journal = pd.DataFrame([journal_row(bt, i) for i in bt.closed_ideas])
    events = pd.DataFrame(bt.events)
    equity = pd.DataFrame([(pd.Timestamp(t, tz="UTC"), float(b), float(e)) for t, b, e in bt.equity_curve],
                          columns=["time", "balance", "equity"])
    sc = scorecard(journal, equity, float(bt.account.starting_balance), events)
    return BacktestResult(journal, events, equity, dict(bt.rejections), sc, list(bt.labels))


# ==============================================================================
# ENGINE
# ==============================================================================
"""
Signal engine + order state engine + portfolio engine (Sections 3 to 9, 12).

Event order at every timestamp t (spec X03):
  1. queued market exits, then queued market entries, at the first tradable quote (bar open)
  2. intrabar: pending limit fills, touched stops, targets (stop first when ambiguous)
  3. financing on day roll, margin and 5% portfolio checks
  4. closed bar logic: Daily invalidation, 4H structural exit / disagreement / expiry,
     2H trailing, news blackout cancels
  5. new entries (ladder placement or market signal queued for the next quote)
"""




ZERO = Decimal(0)
NS_PER_DAY = 86_400 * 1_000_000_000
TF_RULES = {"D": "24h", "H4": "4h", "H2": "2h"}  # tick-like rules keep the 00:00 UTC epoch anchor


# =============================================================================== records
@dataclass
class Leg:
    leg_id: str
    ratio: Decimal | None
    weight: Decimal
    order_type: str             # LIMIT or MARKET
    entry_price: Decimal        # limit price, or requested price for market
    planned_qty: Decimal
    V: Decimal                  # account ccy per 1.0 price unit per lot, frozen at sizing
    cost_per_lot: Decimal
    status: str = "PENDING"     # PENDING, OPEN, CLOSED, CANCELLED
    filled_qty: Decimal = ZERO
    open_qty: Decimal = ZERO
    fill_price: Decimal | None = None
    fill_ns: int | None = None
    realized: Decimal = ZERO
    costs: Decimal = ZERO
    cancel_reason: str = ""


@dataclass
class Setup:
    impulse: Impulse
    rec_ns: int
    rec_idx: int                # 4H bar index at recognition
    atr4h: float                # frozen at recognition
    zone_w: float               # frozen zone half width
    struct_price: float | None  # latest opposite 4H pivot at recognition (gold/SPX stop anchor)

    @property
    def direction(self) -> int:
        return self.impulse.direction

    @property
    def setup_id(self) -> str:
        return self.impulse.setup_id


@dataclass
class Plan:
    entries: list
    stop: Decimal
    tp1: Decimal
    tp2: Decimal


@dataclass
class Idea:
    idea_id: str
    symbol: str
    direction: int
    route: str
    setup: Setup
    plan: Plan
    stop: Decimal
    legs: list
    created_ns: int
    expiry_idx: int
    committed_risk: Decimal
    confluences: dict
    score: int
    context: dict
    status: str = "PENDING"
    tp1_done: bool = False
    trail_pending: Decimal | None = None
    financing: Decimal = ZERO
    ambiguous_bars: int = 0
    exit_reason: str = ""
    close_ns: int | None = None

    def open_legs(self):
        return [l for l in self.legs if l.open_qty > 0]

    def pending_legs(self):
        return [l for l in self.legs if l.status == "PENDING"]

    def open_qty(self) -> Decimal:
        return sum((l.open_qty for l in self.legs), ZERO)

    def filled_qty(self) -> Decimal:
        return sum((l.filled_qty for l in self.legs), ZERO)


@dataclass
class Candidate:
    symbol: str
    route: str
    setup: Setup
    plan: Plan
    score: int
    rr: float
    confluences: dict
    info: dict

    def priority(self):
        # Higher score, then higher TP1 reward/risk after costs, then alphabetical symbol.
        return (-self.score, -self.rr, self.symbol)


# =============================================================================== per symbol
class SymbolState:
    def __init__(self, symbol: str, spec: InstrumentSpec, df: pd.DataFrame, cfg: StrategyConfig):
        self.symbol, self.spec = symbol, spec
        self.interval = infer_interval(df.index)
        self.base = {c: df[c].to_numpy(dtype=float) for c in QCOLS}
        self.base_row = {int(v): i for i, v in enumerate(to_ns(df.index + self.interval))}
        self.tf_data, self.tf_row, self.tf = {}, {}, {}
        emas = {"D": cfg.daily_ema_periods, "H4": (), "H2": cfg.h2_ema_periods}
        for tf, rule in TF_RULES.items():
            f = resample_quotes(df, rule)
            data = {c: f[c].to_numpy(dtype=float) for c in QCOLS}
            data["open_ns"], data["close_ns"] = to_ns(f.index), to_ns(f["close_time"])
            self.tf_data[tf] = data
            self.tf_row[tf] = {int(v): i for i, v in enumerate(data["close_ns"])}
            self.tf[tf] = TFState(tf, emas[tf], cfg.atr_period, cfg.pivot_width)
        self.event_times = set(self.base_row).union(*(self.tf_row[tf].keys() for tf in TF_RULES))

        self.bias = 0
        self.inv_level: float | None = None
        self.inv_confirm_idx = -1
        self.block_ns: int | None = None       # no new position at this Daily decision timestamp
        self.active_idea: Idea | None = None
        self.current_setup: Setup | None = None
        self.used_setups: set = set()
        self.queued_exit: str | None = None
        self.last_bid = self.last_ask = None
        self.last_day: int | None = None

    def warm(self, n: int) -> bool:
        return all(self.tf[tf].ready(n) for tf in TF_RULES)


# =============================================================================== engine
class Backtester:
    def __init__(self, cfg: StrategyConfig, account: AccountConfig, specs: dict,
                 quotes: dict, news=None, verbose: bool = False):
        self.cfg, self.account, self.news, self.verbose = cfg, account, news, verbose
        self.syms = {s: SymbolState(s, specs[s], df, cfg) for s, df in quotes.items()}
        self.balance = D(account.starting_balance)
        self.converter = FxConverter(account.currency)
        self.queued_entries: list[Candidate] = []
        self.closed_ideas: list[Idea] = []
        self.events: list[dict] = []
        self.rejections: Counter = Counter()
        self.equity_curve: list[tuple] = []
        self._seq = 0
        self.labels = []
        if news is None and cfg.news_filter:
            self.labels.append("news gate omitted")
        if not all(sp.cost_data_verified for sp in specs.values()):
            self.labels.append("indicative: venue cost data not verified")

    # ------------------------------------------------------------------- logging helpers
    def _log(self, t, sym, kind, detail="", **extra):
        self.events.append({"time": pd.Timestamp(t, tz="UTC"), "symbol": sym, "event": kind,
                            "detail": detail, **extra})
        if self.verbose:
            print(pd.Timestamp(t, tz="UTC"), sym, kind, detail)

    def _reject(self, t, sym, reason, detail=""):
        self.rejections[reason] += 1
        self._log(t, sym, "REJECT", f"{reason} {detail}".strip())

    def _next_id(self, sym):
        self._seq += 1
        return f"{self.account.account_id}-{sym}-{self._seq:06d}"

    # ------------------------------------------------------------------- price helpers
    def _V(self, s: SymbolState, fallback: Decimal | None = None) -> Decimal:
        try:
            return s.spec.contract_size * self.converter.to_account(s.spec.quote_ccy)
        except MetadataError:
            if fallback is None:
                raise
            return fallback

    def _slip(self, s: SymbolState) -> Decimal:
        return Decimal(s.spec.stop_slippage_ticks) * s.spec.tick_size * self.cfg.cost_multiplier

    def _liq_price(self, s: SymbolState, direction: int) -> Decimal:
        """Executable liquidation price: bid for longs, ask for shorts."""
        return D(s.last_bid if direction == 1 else s.last_ask)

    # ------------------------------------------------------------------- run loop
    def run(self):
        times = sorted(set().union(*(s.event_times for s in self.syms.values())))
        for t in times:
            self._step(t)
        for s in self.syms.values():  # report still open ideas without inventing an exit
            if s.active_idea:
                s.active_idea.exit_reason = "open_at_end"
                self.closed_ideas.append(s.active_idea)
        return build_result(self)

    def _step(self, t: int):
        live = [s for s in self.syms.values() if t in s.base_row]

        # 1. first tradable quote: exits first, then entries by priority
        for s in live:
            row = s.base_row[t]
            self.converter.update(s.symbol, (s.base["bid_open"][row] + s.base["ask_open"][row]) / 2)
            s.last_bid, s.last_ask = s.base["bid_open"][row], s.base["ask_open"][row]
            if s.queued_exit and s.active_idea:
                self._exit_idea_at(s, s.active_idea, t, s.queued_exit, open_bar=True)
            s.queued_exit = None
        ready = [c for c in self.queued_entries if t in self.syms[c.symbol].base_row]
        self.queued_entries = [c for c in self.queued_entries if c not in ready]
        for c in sorted(ready, key=Candidate.priority):
            self._exec_market(c, t)

        # 2. intrabar simulation, then 3. financing
        for s in live:
            row = s.base_row[t]
            self._intrabar(s, t, row)
            s.last_bid, s.last_ask = s.base["bid_close"][row], s.base["ask_close"][row]
            self.converter.update(s.symbol, (s.last_bid + s.last_ask) / 2)
            day = t // NS_PER_DAY
            if s.last_day is not None and day != s.last_day and s.active_idea:
                self._charge_financing(s, t, day - s.last_day)
            s.last_day = day

        if live:
            self._portfolio_checks(t)
            self.equity_curve.append((t, self.balance, self._equity()))

        # 4 and 5. closed bars and new entries
        candidates = []
        for s in self.syms.values():
            closed = [tf for tf in TF_RULES if t in s.tf_row[tf]]
            if closed:
                c = self._on_bar_close(s, t, closed, t in s.base_row)
                if c:
                    candidates.append(c)
        for c in sorted(candidates, key=Candidate.priority):
            if c.route == "L":
                self._place_ladder(c, t)
            else:
                self.queued_entries.append(c)

    # ------------------------------------------------------------------- intrabar
    def _intrabar(self, s: SymbolState, t: int, row: int):
        idea = s.active_idea
        if idea is None:
            return
        b = {k: v[row] for k, v in s.base.items()}
        d, spec = idea.direction, s.spec
        through = self.cfg.limit_fill_through_ticks * float(spec.tick_size)
        filled_now = False

        # a) pending limit fills: buy limit when ASK reaches it, sell limit when BID reaches it
        for leg in idea.pending_legs():
            lim = float(leg.entry_price)
            if d == 1 and b["ask_low"] <= lim - through:
                self._fill_leg(s, idea, leg, min(D(b["ask_open"]), leg.entry_price), t)
                filled_now = True
            elif d == -1 and b["bid_high"] >= lim + through:
                self._fill_leg(s, idea, leg, max(D(b["bid_open"]), leg.entry_price), t)
                filled_now = True
        if idea.open_qty() == 0:
            return

        # b) protective stop, touched intrabar (bid for longs, ask for shorts)
        stop = float(idea.stop)
        ex_open, ex_hi, ex_lo = ((b["bid_open"], b["bid_high"], b["bid_low"]) if d == 1
                                 else (b["ask_open"], b["ask_high"], b["ask_low"]))
        touched = ex_lo <= stop if d == 1 else ex_hi >= stop
        if touched:
            gap = ex_open <= stop if d == 1 else ex_open >= stop
            raw = D(ex_open) if gap else idea.stop
            px = raw - self._slip(s) if d == 1 else raw + self._slip(s)
            fav = ex_hi if d == 1 else ex_lo
            target = idea.plan.tp2 if idea.tp1_done else idea.plan.tp1
            if not gap and ((fav - float(target)) * d >= 0 or filled_now):
                idea.ambiguous_bars += 1
                self._log(t, s.symbol, "AMBIGUOUS_BAR", "stop first applied")
            self._cancel_pending(s, idea, t, "stop activation")
            self._close_all(s, idea, px, t, "stop")
            return

        # c) targets (limit exits at bid for longs, ask for shorts)
        fav_hi = ex_hi if d == 1 else ex_lo
        if not idea.tp1_done and (fav_hi - float(idea.plan.tp1)) * d >= 0:
            px = max(D(ex_open), idea.plan.tp1) if d == 1 else min(D(ex_open), idea.plan.tp1)
            self._take_tp1(s, idea, px, t)
            if s.active_idea is None:
                return
        if idea.tp1_done and (fav_hi - float(idea.plan.tp2)) * d >= 0:
            px = max(D(ex_open), idea.plan.tp2) if d == 1 else min(D(ex_open), idea.plan.tp2)
            self._close_all(s, idea, px, t, "TP2")
            return

        # d) retry a tightened stop that was too close to price earlier
        if idea.trail_pending is not None:
            self._apply_stop(s, idea, idea.trail_pending, t, "trail retry")

    # ------------------------------------------------------------------- fills and exits
    def _fill_leg(self, s, idea, leg, px, t):
        leg.fill_price, leg.fill_ns, leg.status = px, t, "OPEN"
        leg.filled_qty = leg.open_qty = leg.planned_qty
        comm = s.spec.commission_per_lot_rt / 2 * leg.planned_qty * self.cfg.cost_multiplier
        leg.costs += comm
        self.balance -= comm
        idea.status = "OPEN"
        self._log(t, s.symbol, "FILL", f"{leg.leg_id} {leg.planned_qty} @ {px}")

    def _close_leg_qty(self, s, idea, leg, qty, px, t, reason):
        qty = min(qty, leg.open_qty)
        if qty <= 0:
            return
        V = self._V(s, leg.V)
        pnl = (px - leg.fill_price) * idea.direction * qty * V
        comm = s.spec.commission_per_lot_rt / 2 * qty * self.cfg.cost_multiplier
        leg.open_qty -= qty
        leg.realized += pnl
        leg.costs += comm
        self.balance += pnl - comm
        if leg.open_qty == 0:
            leg.status = "CLOSED"
        self._log(t, s.symbol, "EXIT", f"{leg.leg_id} {qty} @ {px} {reason}", pnl=float(pnl))

    def _close_all(self, s, idea, px, t, reason):
        for leg in idea.open_legs():
            self._close_leg_qty(s, idea, leg, leg.open_qty, px, t, reason)
        if not idea.exit_reason:
            idea.exit_reason = reason
        self._maybe_finalize(s, idea, t)

    def _reduce_idea(self, s, idea, qty, px, t, reason):
        """Close `qty` across open legs proportionally, rounded down to the increment."""
        step, total = s.spec.qty_step, idea.open_qty()
        remaining = qty
        for leg in idea.open_legs():
            part = round_qty_down(leg.open_qty * qty / total, step)
            self._close_leg_qty(s, idea, leg, part, px, t, reason)
            remaining -= part
        for leg in sorted(idea.open_legs(), key=lambda l: -l.open_qty):
            while remaining > 0 and leg.open_qty > 0:
                self._close_leg_qty(s, idea, leg, step, px, t, reason)
                remaining -= step

    def _exit_idea_at(self, s, idea, t, reason, open_bar=False):
        """Market exit at the next executable quote, with slippage."""
        self._cancel_pending(s, idea, t, reason)
        if idea.open_qty() > 0:
            px = self._liq_price(s, idea.direction)
            px = px - self._slip(s) if idea.direction == 1 else px + self._slip(s)
            self._close_all(s, idea, px, t, reason)
        else:
            self._maybe_finalize(s, idea, t)

    def _take_tp1(self, s, idea, px, t):
        spec, open_q = s.spec, idea.open_qty()
        close_q = round_qty_down(open_q * self.cfg.tp1_close_fraction, spec.qty_step)
        idea.tp1_done = True
        self._cancel_pending(s, idea, t, "TP1")
        if close_q < spec.min_qty or open_q - close_q < spec.min_qty:
            self._close_all(s, idea, px, t, "TP1 full (below min qty)")
            return
        self._reduce_idea(s, idea, close_q, px, t, "TP1")
        # X02: tighten remainder to quantity weighted ACTUAL entry, rounded toward protection
        filled = [l for l in idea.legs if l.filled_qty > 0]
        avg = sum((l.fill_price * l.filled_qty for l in filled), ZERO) / sum((l.filled_qty for l in filled), ZERO)
        be = round_price(avg, spec.tick_size, "up" if idea.direction == 1 else "down")
        self._apply_stop(s, idea, be, t, "breakeven")

    def _apply_stop(self, s, idea, new_stop, t, why):
        """Only ever tightens. Crossed = exit at market; too close = keep old stop, retry later."""
        d = idea.direction
        if (new_stop - idea.stop) * d <= 0:
            idea.trail_pending = None
            return
        liq = self._liq_price(s, d)
        if (liq - new_stop) * d <= 0:
            idea.trail_pending = None
            s.queued_exit = f"{why}: tightened stop already crossed"
            return
        if abs(liq - new_stop) < s.spec.min_stop_distance:
            idea.trail_pending = new_stop
            return
        idea.stop, idea.trail_pending = new_stop, None
        self._log(t, s.symbol, "STOP_MOVE", f"{why} -> {new_stop}")

    def _cancel_pending(self, s, idea, t, reason):
        for leg in idea.pending_legs():
            leg.status, leg.cancel_reason = "CANCELLED", reason
            self._log(t, s.symbol, "CANCEL", f"{leg.leg_id} {reason}")
        self._maybe_finalize(s, idea, t)

    def _maybe_finalize(self, s, idea, t):
        if idea.pending_legs() or idea.open_qty() > 0 or idea.close_ns is not None:
            return
        idea.status, idea.close_ns = "CLOSED", t
        if idea.filled_qty() == 0 and not idea.exit_reason:
            idea.exit_reason = "expired or cancelled unfilled"
        s.active_idea = None
        s.used_setups.add(idea.setup.setup_id)  # no re-entry on the same frozen A/B setup
        self.closed_ideas.append(idea)
        self._log(t, s.symbol, "IDEA_CLOSED", idea.exit_reason)

    def _charge_financing(self, s, t, days):
        idea = s.active_idea
        lots = idea.open_qty()
        if lots > 0:
            cost = s.spec.financing_per_lot_day * lots * days * self.cfg.cost_multiplier
            idea.financing += cost
            self.balance -= cost

    # ------------------------------------------------------------------- portfolio state
    def _equity(self) -> Decimal:
        eq = self.balance
        for s in self.syms.values():
            idea = s.active_idea
            if idea and idea.open_qty() > 0:
                liq = self._liq_price(s, idea.direction)
                for leg in idea.open_legs():
                    eq += (liq - leg.fill_price) * idea.direction * leg.open_qty * self._V(s, leg.V)
        return eq

    def _used_risk(self) -> Decimal:
        used = ZERO
        for s in self.syms.values():
            idea = s.active_idea
            if not idea:
                continue
            for leg in idea.legs:
                if leg.status == "PENDING":
                    used += pending_reserved_risk(leg.entry_price, idea.stop, leg.planned_qty,
                                                       leg.V, leg.cost_per_lot)
                elif leg.open_qty > 0:
                    remaining = leg.cost_per_lot - s.spec.commission_per_lot_rt / 2 * self.cfg.cost_multiplier
                    used += open_leg_risk(idea.direction, leg.fill_price, idea.stop,
                                               self._liq_price(s, idea.direction), leg.open_qty,
                                               self._V(s, leg.V), max(ZERO, remaining))
        return used

    def _used_margin(self) -> Decimal:
        m = ZERO
        for s in self.syms.values():
            idea = s.active_idea
            if idea and idea.open_qty() > 0:
                conv = self._V(s, None) / s.spec.contract_size
                m += idea.open_qty() * margin_per_lot(s.spec, D((s.last_bid + s.last_ask) / 2), conv)
        return m

    def _open_leg_views(self):
        for s in self.syms.values():
            if s.active_idea:
                for leg in s.active_idea.open_legs():
                    yield s, s.active_idea, leg

    def _portfolio_checks(self, t):
        if not any(s.active_idea for s in self.syms.values()):
            return
        stop_out = self.account.stop_out_pct
        level = margin_level(self._equity(), self._used_margin())
        if level is not None and level < stop_out:
            self._log(t, "ALL", "BROKER_STOP_OUT", f"margin level {level:.1f}%")
            for s in list(self.syms.values()):
                if s.active_idea:
                    self._exit_idea_at(s, s.active_idea, t, "broker stop-out")
            return
        if level is not None and level < self.cfg.margin_safety_multiple * stop_out:
            self._log(t, "ALL", "MARGIN_SAFETY", f"margin level {level:.1f}%")
            self._cancel_all_pending(t, "margin safety")
            self._reduce_until(t, lambda: (margin_level(self._equity(), self._used_margin()) or Decimal("1e9"))
                               >= self.cfg.margin_safety_multiple * stop_out, by="margin")
        K = capital_base(self.balance, self._equity())
        cap = self.cfg.portfolio_risk_pct * K
        if self._used_risk() > cap:
            self._log(t, "ALL", "RISK_CAP_BREACH", f"used {self._used_risk():.2f} > cap {cap:.2f}")
            self._cancel_all_pending(t, "portfolio risk cap")
            self._reduce_until(t, lambda: self._used_risk() <= cap, by="risk")

    def _cancel_all_pending(self, t, reason):
        for s in list(self.syms.values()):
            if s.active_idea and s.active_idea.pending_legs():
                self._cancel_pending(s, s.active_idea, t, reason)

    def _reduce_until(self, t, satisfied, by: str, guard: int = 10_000):
        """Reduce the largest margin (or largest risk) open leg in minimum size steps; tie = oldest fill."""
        for _ in range(guard):
            if satisfied():
                return
            legs = list(self._open_leg_views())
            if not legs:
                return

            def metric(item):
                s, idea, leg = item
                if by == "margin":
                    conv = self._V(s, leg.V) / s.spec.contract_size
                    val = leg.open_qty * margin_per_lot(s.spec, D(s.last_bid), conv)
                else:
                    val = open_leg_risk(idea.direction, leg.fill_price, idea.stop,
                                             self._liq_price(s, idea.direction), leg.open_qty,
                                             self._V(s, leg.V), ZERO)
                return (val, -leg.fill_ns)
            s, idea, leg = max(legs, key=metric)
            px = self._liq_price(s, idea.direction)
            px = px - self._slip(s) if idea.direction == 1 else px + self._slip(s)
            step = max(s.spec.min_qty, s.spec.qty_step)
            self._close_leg_qty(s, idea, leg, min(step, leg.open_qty), px, t, f"{by} reduction")
            if not idea.exit_reason and idea.open_qty() == 0:
                idea.exit_reason = f"{by} reduction"
            self._maybe_finalize(s, idea, t)

    # ------------------------------------------------------------------- bar close logic
    def _on_bar_close(self, s: SymbolState, t: int, closed: list, session_open: bool):
        new_pivots = {}
        for tf in ("D", "H4", "H2"):
            if tf in closed:
                r = s.tf_row[tf][t]
                d = s.tf_data[tf]
                new_pivots[tf] = s.tf[tf].add_bar(d["bid_open"][r], d["bid_high"][r], d["bid_low"][r],
                                                  d["bid_close"][r], d["ask_high"][r], d["ask_low"][r],
                                                  int(d["open_ns"][r]), int(d["close_ns"][r]))
        if not s.warm(self.cfg.warmup_bars):
            return None
        h4 = s.tf["H4"]

        if "D" in closed:
            self._update_bias(s, t)

        idea = s.active_idea
        if idea and "H4" in closed:
            latest = h4.latest("L" if idea.direction == 1 else "H")
            if idea.open_qty() > 0 and latest and (latest.price - h4.c[-1]) * idea.direction > 0:
                s.queued_exit = "4H structural close"
            if idea.pending_legs() and h4.structure() != idea.direction:
                self._cancel_pending(s, idea, t, "4H disagreement")
            if s.active_idea and idea.pending_legs() and h4.n - 1 >= idea.expiry_idx:
                self._cancel_pending(s, idea, t, "ladder expiry")

        idea = s.active_idea
        if idea and "H2" in closed and idea.tp1_done:
            self._trail(s, idea, t, new_pivots.get("H2", []))

        blackout = self._in_blackout(s, t)
        if idea and blackout and idea.pending_legs():
            self._cancel_pending(s, idea, t, "news blackout")

        if s.current_setup and h4.n - 1 >= s.current_setup.rec_idx + self.cfg.ladder_expiry_4h_bars:
            s.current_setup = None

        # ---- new entries
        if s.active_idea or s.queued_exit or s.block_ns == t or s.bias == 0:
            return None
        if any(c.symbol == s.symbol for c in self.queued_entries):
            return None
        if "H4" in closed:
            self._recognize_setup(s, t)
        setup = s.current_setup
        if setup is None or setup.setup_id in s.used_setups or setup.direction != s.bias:
            return None
        if self.cfg.route == "L" and "H4" in closed and setup.rec_ns == t:
            if not session_open:
                self._reject(t, s.symbol, "session closed at recognition")
                s.used_setups.add(setup.setup_id)
                return None
            return self._ladder_candidate(s, setup, t)
        if self.cfg.route == "M" and "H2" in closed:
            return self._market_candidate(s, setup, t)
        return None

    def _in_blackout(self, s, t) -> bool:
        return bool(self.cfg.news_filter and self.news and self.news.in_blackout(s.spec.currencies, t))

    def _update_bias(self, s: SymbolState, t: int):
        D1 = s.tf["D"]
        close = D1.c[-1]
        if s.bias != 0:
            kind = "L" if s.bias == 1 else "H"
            p = D1.latest(kind)
            if p and p.confirm_idx > s.inv_confirm_idx and (p.price - s.inv_level) * s.bias > 0:
                s.inv_level, s.inv_confirm_idx = p.price, p.confirm_idx
                self._log(t, s.symbol, "BIAS_INVALIDATION_MOVED", f"{p.price}")
            if (s.inv_level - close) * s.bias > 0:  # strict close beyond invalidation; wicks ignored
                self._log(t, s.symbol, "BIAS_INVALIDATED", f"close {close} vs {s.inv_level}")
                s.bias, s.current_setup, s.block_ns = 0, None, t
                if s.active_idea:
                    self._cancel_pending(s, s.active_idea, t, "daily invalidation")
                    if s.active_idea and s.active_idea.open_qty() > 0:
                        s.queued_exit = "daily invalidation"
        if s.bias == 0:
            st = D1.structure()
            p = D1.latest("L" if st == 1 else "H") if st != 0 else None
            # Interpretation (versioned): a direction is only "valid" if the current close has not
            # already broken its invalidation level. Without this, the same stale pivots re-arm the
            # old bias every day after an invalidation.
            if p is not None and (close - p.price) * st > 0:
                s.bias, s.inv_level, s.inv_confirm_idx = st, p.price, p.confirm_idx
                self._log(t, s.symbol, "BIAS_SET", f"{st:+d} invalidation {p.price}")

    def _recognize_setup(self, s: SymbolState, t: int):
        d = s.bias
        if d == -1 and s.symbol in self.cfg.long_only_symbols:
            return
        h4 = s.tf["H4"]
        if h4.structure() != d:
            return
        imp, _ = find_impulse(h4, d, self.cfg)
        if imp is None or imp.setup_id in s.used_setups:
            return
        if s.current_setup and s.current_setup.setup_id == imp.setup_id:
            return
        atr = h4.atr_now
        zone_w = max(self.cfg.zone_min_ticks * float(s.spec.tick_size), self.cfg.zone_atr_fraction * atr)
        struct = h4.latest("L" if d == 1 else "H")
        s.current_setup = Setup(imp, t, h4.n - 1, atr, zone_w, struct.price if struct else None)
        self._log(t, s.symbol, "SETUP", f"{imp.setup_id} A={imp.A.price} B={imp.B.price} eff={imp.efficiency:.2f}")

    # ------------------------------------------------------------------- signals
    def _plan(self, s: SymbolState, setup: Setup, entries=None):
        spec, cfg, d = s.spec, self.cfg, setup.direction
        tick = spec.tick_size
        A, B = D(setup.impulse.A.price), D(setup.impulse.B.price)
        rng = B - A
        toward_entry = "down" if d == 1 else "up"
        tp1 = round_price(B + cfg.tp1_extension * rng, tick, toward_entry)
        tp2 = round_price(B + cfg.tp2_extension * rng, tick, toward_entry)
        if spec.asset_class == "FX":
            stop = round_price(B - cfg.fx_stop_ratio * rng, tick, "nearest")  # exact 89.3%, no buffer
        else:
            if setup.struct_price is None:
                return None, "no structural pivot for stop"
            raw = D(setup.struct_price) - d * D(cfg.structural_stop_atr) * D(setup.atr4h)
            stop = round_price(raw, tick, "down" if d == 1 else "up")
        if entries is None:
            entries = [round_price(B - r * rng, tick, "down" if d == 1 else "up") for r in cfg.ladder_ratios]
        for e in entries:
            if (e - stop) * d <= 0:
                return None, "stop not beyond every entry"
            if abs(e - stop) < spec.min_stop_distance:
                return None, "below minimum stop distance"
            if (tp1 - e) * d <= 0:
                return None, "TP1 not beyond entry"
        return Plan(list(entries), stop, tp1, tp2), None

    def _confluences(self, s, setup, price: float) -> dict:
        tick = float(s.spec.tick_size)
        d, w = setup.direction, setup.zone_w
        fibs = [setup.impulse.fib(float(r)) for r in self.cfg.ladder_ratios]
        h2 = s.tf["H2"]
        e8, e14 = (h2.ema(p) for p in self.cfg.h2_ema_periods)
        return {
            "C1_direction": s.bias == d and s.tf["H4"].structure() == d,
            "C2_psych": in_psych_zone(price, float(s.spec.grid_mid), w),
            "C3_fib": any(abs(price - f) <= w for f in fibs),
            "C4_candle": candle_signal(h2, d, tick, self.cfg),
            "C5_ema": (e8 - e14) * d > 0,
            "C6_trendline": trendline_ok(s.tf["H4"], h2, d, w),
        }

    def _context(self, s, setup) -> dict:
        D1, h2 = s.tf["D"], s.tf["H2"]
        imp = setup.impulse
        return {
            "bias": s.bias, "invalidation_level": s.inv_level,
            "A_price": imp.A.price, "A_time": pd.Timestamp(imp.A.time_ns, tz="UTC"),
            "A_confirmed": pd.Timestamp(imp.A.confirm_ns, tz="UTC"),
            "B_price": imp.B.price, "B_time": pd.Timestamp(imp.B.time_ns, tz="UTC"),
            "B_confirmed": pd.Timestamp(imp.B.confirm_ns, tz="UTC"),
            "impulse_efficiency": imp.efficiency, "atr4h": setup.atr4h, "atr2h": h2.atr_now,
            "zone_half_width": setup.zone_w,
            **{f"D_EMA{p}": D1.ema(p) for p in self.cfg.daily_ema_periods},
            **{f"2H_EMA{p}": h2.ema(p) for p in self.cfg.h2_ema_periods},
        }

    def _rr(self, s, plan, entry_avg: Decimal) -> float:
        V = self._V(s, None)
        cost = cost_allowance_per_lot(s.spec, V, self.cfg)
        reward = abs(plan.tp1 - entry_avg) * V - cost
        loss = abs(entry_avg - plan.stop) * V + cost
        return float(reward / loss) if loss > 0 else 0.0

    def _ladder_candidate(self, s, setup, t):
        d = setup.direction
        plan, why = self._plan(s, setup)
        s.used_setups.add(setup.setup_id)  # a ladder is attempted once per frozen setup
        if plan is None:
            self._reject(t, s.symbol, why); return None
        ok, failing = check_ladder_levels(plan.entries, float(s.spec.grid_mid), setup.zone_w)
        if not ok:
            self._reject(t, s.symbol, "ladder leg outside psychological zone", str(failing)); return None
        quote = s.last_ask if d == 1 else s.last_bid
        if any((quote - float(e)) * d <= 0 for e in plan.entries):
            self._reject(t, s.symbol, "limit on wrong side of market"); return None
        h4 = s.tf["H4"]
        b_idx = setup.impulse.B.idx
        touched = (min(h4.ask_l[b_idx:]) <= float(max(plan.entries)) if d == 1
                   else max(h4.h[b_idx:]) >= float(min(plan.entries)))
        if touched:
            self._reject(t, s.symbol, "stale ladder: level touched before recognition"); return None
        if self._in_blackout(s, t):
            self._reject(t, s.symbol, "news blackout"); return None
        conf = self._confluences(s, setup, float(plan.entries[0]))
        conf["C2_psych"] = conf["C3_fib"] = True  # every leg verified above; C3 true by construction
        score = sum(conf.values())
        if not conf["C1_direction"] or score < self.cfg.min_confluence_score:
            self._reject(t, s.symbol, "confluence gate"); return None
        w = self.cfg.ladder_weights
        avg = sum((wi * e for wi, e in zip(w, plan.entries)), ZERO)
        try:
            rr = self._rr(s, plan, avg)
        except MetadataError as e:
            self._reject(t, s.symbol, "metadata", str(e)); return None
        return Candidate(s.symbol, "L", setup, plan, score, rr, conf, {})

    def _market_candidate(self, s, setup, t):
        d, h2 = setup.direction, s.tf["H2"]
        w = setup.zone_w
        fibs = [setup.impulse.fib(float(r)) for r in self.cfg.ladder_ratios]
        if not any(h2.l[-1] <= f + w and h2.h[-1] >= f - w for f in fibs):
            return None
        conf = self._confluences(s, setup, h2.c[-1])
        if not (conf["C1_direction"] and conf["C2_psych"] and conf["C3_fib"] and conf["C4_candle"]):
            return None
        plan, why = self._plan(s, setup, entries=[D(h2.c[-1])])
        if plan is None:
            self._reject(t, s.symbol, why); return None
        try:
            rr = self._rr(s, plan, plan.entries[0])
        except MetadataError as e:
            self._reject(t, s.symbol, "metadata", str(e)); return None
        return Candidate(s.symbol, "M", setup, plan, sum(conf.values()), rr, conf,
                         {"signal_close": h2.c[-1], "atr2h": h2.atr_now, "signal_ns": t})

    # ------------------------------------------------------------------- order creation
    def _size(self, s, plan, route, t):
        """Returns (quantities, planned_risk, V, cost, risk_before) or raises ValueError(reason)."""
        s.spec.validate()
        self.account.validate()
        V = self._V(s, None)
        cost = cost_allowance_per_lot(s.spec, V, self.cfg)
        equity = self._equity()
        K = capital_base(self.balance, equity)
        if K <= 0:
            raise ValueError("non positive capital base")
        used = self._used_risk()
        allowed = allowed_idea_risk(K, used, self.cfg)
        if route == "L":
            res = size_ladder(plan.entries, self.cfg.ladder_weights, plan.stop, V, cost, allowed, s.spec)
        else:
            res = size_market(plan.entries[0], plan.stop, V, cost, allowed, s.spec)
        if not res.ok:
            raise ValueError(res.reason)
        conv = V / s.spec.contract_size
        mpl = [margin_per_lot(s.spec, e, conv) for e in plan.entries]
        qtys, scale = list(res.quantities), Decimal(1)
        while True:  # broker solvency check: shrink proportionally or reject
            new_margin = sum((q * m for q, m in zip(qtys, mpl)), ZERO)
            new_risk = sum((q * l for q, l in zip(qtys, res.losses_per_lot)), ZERO)
            if projected_margin_ok(equity, self._used_margin(), used, new_margin, new_risk,
                                        self.account.stop_out_pct, self.cfg.margin_safety_multiple):
                break
            scale *= Decimal("0.9")
            qtys = [round_qty_down(q * scale, s.spec.qty_step) for q in res.quantities]
            if any(q < s.spec.min_qty for q in qtys):
                raise ValueError("broker margin check failed")
        return qtys, new_risk, V, cost, used

    def _new_idea(self, c: Candidate, qtys, planned_risk, V, cost, t, order_type, used_before):
        s = self.syms[c.symbol]
        idea_id = self._next_id(c.symbol)
        ratios = self.cfg.ladder_ratios if c.route == "L" else (None,)
        weights = self.cfg.ladder_weights if c.route == "L" else (Decimal(1),)
        legs = [Leg(f"{idea_id}-L{i + 1}", r, w, order_type, e, q, V, cost)
                for i, (r, w, e, q) in enumerate(zip(ratios, weights, c.plan.entries, qtys))]
        ctx = self._context(s, c.setup)
        ctx.update({"risk_before": float(used_before), "risk_after": float(used_before + planned_risk),
                    "V_value_per_unit_lot": float(V), "cost_allowance_per_lot": float(cost),
                    "margin_level_at_entry": None if not self._used_margin() else
                    float(margin_level(self._equity(), self._used_margin()))})
        idea = Idea(idea_id, c.symbol, c.setup.direction, c.route, c.setup, c.plan, c.plan.stop, legs,
                    t, c.setup.rec_idx + self.cfg.ladder_expiry_4h_bars, planned_risk,
                    c.confluences, c.score, ctx)
        s.active_idea = idea
        self._log(t, c.symbol, "IDEA_OPENED", f"{idea_id} {c.route} qty={qtys} stop={c.plan.stop} "
                  f"tp1={c.plan.tp1} tp2={c.plan.tp2} risk={planned_risk:.2f}")
        return idea

    def _place_ladder(self, c: Candidate, t):
        s = self.syms[c.symbol]
        if s.active_idea:
            self._reject(t, c.symbol, "one active idea per symbol"); return
        try:
            qtys, prisk, V, cost, used = self._size(s, c.plan, "L", t)
        except (ValueError, MetadataError) as e:
            self._reject(t, c.symbol, str(e)); return
        self._new_idea(c, qtys, prisk, V, cost, t, "LIMIT", used)

    def _exec_market(self, c: Candidate, t):
        s = self.syms[c.symbol]
        setup, d = c.setup, c.setup.direction
        if s.active_idea:
            self._reject(t, c.symbol, "one active idea per symbol"); return
        if s.bias != d or s.tf["H4"].structure() != d:
            self._reject(t, c.symbol, "gate failed at execution"); return
        if self._in_blackout(s, t):
            self._reject(t, c.symbol, "news blackout"); return
        row = s.base_row[t]
        bid_open, ask_open = s.base["bid_open"][row], s.base["ask_open"][row]
        adverse = (bid_open - c.info["signal_close"]) * d
        if adverse > self.cfg.market_max_adverse_atr * c.info["atr2h"]:
            self._reject(t, c.symbol, "adverse move after signal"); return
        exec_px = D(ask_open) + self._slip(s) if d == 1 else D(bid_open) - self._slip(s)
        fibs = [setup.impulse.fib(float(r)) for r in self.cfg.ladder_ratios]
        if not (in_psych_zone(float(exec_px), float(s.spec.grid_mid), setup.zone_w)
                and any(abs(float(exec_px) - f) <= setup.zone_w for f in fibs)):
            self._reject(t, c.symbol, "executable price left zone"); return
        plan, why = self._plan(s, setup, entries=[exec_px])
        if plan is None:
            self._reject(t, c.symbol, why); return
        c.plan = plan
        try:
            qtys, prisk, V, cost, used = self._size(s, plan, "M", t)
        except (ValueError, MetadataError) as e:
            self._reject(t, c.symbol, str(e)); return
        idea = self._new_idea(c, qtys, prisk, V, cost, t, "MARKET", used)
        s.used_setups.add(setup.setup_id)
        self._fill_leg(s, idea, idea.legs[0], exec_px, t)

    # ------------------------------------------------------------------- trailing
    def _trail(self, s, idea, t, new_pivots):
        kind = "L" if idea.direction == 1 else "H"
        h2 = s.tf["H2"]
        for p, appended in new_pivots:
            if p.kind != kind or not appended:
                continue
            same = [q for q in h2.pivots.alt if q.kind == kind]
            if len(same) < 2 or (same[-1].price - same[-2].price) * idea.direction <= 0:
                continue  # needs a higher low (long) or lower high (short)
            raw = D(p.price) - idea.direction * D(self.cfg.trail_atr) * D(h2.atr_now)
            new_stop = round_price(raw, s.spec.tick_size, "up" if idea.direction == 1 else "down")
            self._apply_stop(s, idea, new_stop, t, "trail")


# ==============================================================================
# SELF TEST (worked example and Section 8 acceptance checks)
# ==============================================================================
def selftest() -> None:
    cfg, specs = StrategyConfig(), placeholder_catalog()
    gold = specs["XAUUSD"]
    B, rng = Decimal("4200"), Decimal("200")
    entries = [round_price(B - r * rng, gold.tick_size, "down") for r in cfg.ladder_ratios]
    assert entries == [Decimal("4123.60"), Decimal("4100.00"), Decimal("4076.40")]
    res = size_ladder(entries, cfg.ladder_weights, Decimal("3990"), Decimal("100"), Decimal("0"),
                      Decimal("2000"), gold)
    assert res.quantities == [Decimal("0.03"), Decimal("0.05"), Decimal("0.09")], res.quantities
    assert res.planned_risk == Decimal("1728.40")
    assert round_price(B + cfg.tp1_extension * rng, gold.tick_size, "down") == Decimal("4254.00")
    assert round_price(B + cfg.tp2_extension * rng, gold.tick_size, "down") == Decimal("4323.60")
    print("PASS worked gold ladder: 0.03/0.05/0.09 lots, $1,728.40 risk, TP1 4254.00, TP2 4323.60")

    fx_stop = round_price(Decimal("1.1500") - cfg.fx_stop_ratio * Decimal("0.05"), Decimal("0.00001"), "nearest")
    assert fx_stop == Decimal("1.10535")
    print("PASS FX stop at exactly 89.3%: 1.10535")

    assert allowed_idea_risk(Decimal("100000"), Decimal("4000"), cfg) == Decimal("1000")
    print("PASS two ideas at 2% each: third idea capped at 1%")

    ok, failing = check_ladder_levels([Decimal("1.2500"), Decimal("1.2400"), Decimal("1.2250")], 0.025, 0.0005)
    assert not ok and failing == [Decimal("1.2400")]
    print("PASS one ladder leg outside psychological zone: whole ladder rejected")

    tracker, emitted = PivotTracker(2), []
    highs, lows = [1.0, 1.1, 1.5, 1.2, 1.1], [0.9, 1.0, 1.3, 1.1, 1.0]
    for j in range(5):
        emitted.append(tracker.update(highs[:j + 1], lows[:j + 1], list(range(j + 1)), list(range(1, j + 2))))
    assert emitted[2] == [] and emitted[3] == [] and emitted[4][0][0].confirm_idx == 4
    print("PASS pivot at bar k usable only at close of bar k+2")

    specs["EURUSD"].venue_symbol = None
    try:
        specs["EURUSD"].validate()
        raise AssertionError("missing metadata not caught")
    except MetadataError as e:
        assert "venue_symbol" in str(e)
    print("PASS missing instrument metadata blocks orders with a specific reason")
    print("All self tests passed.")


# ==============================================================================
# RUNNER
# ==============================================================================


def load_data(args):
    if args.demo:
        return {
            "EURUSD": synthetic_quotes(start_price=1.10, annual_vol=0.08, spread=0.00010, decimals=5, seed=1),
            "USDJPY": synthetic_quotes(start_price=140.0, annual_vol=0.10, spread=0.012, decimals=3, seed=2),
            "XAUUSD": synthetic_quotes(start_price=2000.0, annual_vol=0.16, spread=0.30, decimals=2, seed=3),
        }
    data = {}
    for fn in sorted(os.listdir(args.data_dir)):
        if fn.endswith(".csv"):
            data[fn[:-4].upper()] = load_quotes_csv(os.path.join(args.data_dir, fn))
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--data-dir")
    ap.add_argument("--route", choices=["L", "M", "both"], default="both")
    ap.add_argument("--news")
    ap.add_argument("--balance", default="100000")
    ap.add_argument("--stop-out-pct", default="50", help="broker stop-out margin level %% (placeholder)")
    ap.add_argument("--cost-multiplier", default="1", help="2 = doubled cost stress test")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    if not args.demo and not args.data_dir:
        ap.error("use --selftest, --demo or --data-dir")

    quotes = load_data(args)
    specs = placeholder_catalog()  # REPLACE with your broker's verified symbol specifications
    news = NewsCalendar.from_csv(args.news) if args.news else None
    os.makedirs(args.out, exist_ok=True)
    routes = ["M", "L"] if args.route == "both" else [args.route]

    for route in routes:  # route isolation: separate runs on identical data
        cfg = dataclasses.replace(StrategyConfig(), route=route, variant=f"baseline_{route}",
                                  cost_multiplier=Decimal(args.cost_multiplier))
        account = AccountConfig("FX01", "Forex broker", starting_balance=Decimal(args.balance),
                                stop_out_pct=Decimal(args.stop_out_pct))
        t0 = time.time()
        res = Backtester(cfg, account, specs, quotes, news=news).run()
        print(f"\n=== {cfg.version} route {route}  config {cfg.fingerprint()}  ({time.time() - t0:.1f}s)")
        for label in res.labels:
            print(f"LABEL: {label}")
        for k, v in res.scorecard.items():
            print(f"  {k:30s} {v:.4f}" if isinstance(v, float) else f"  {k:30s} {v}")
        print("  rejection counts:")
        for k, v in sorted(res.rejections.items(), key=lambda kv: -kv[1]):
            print(f"    {v:6d}  {k}")
        res.journal.to_csv(os.path.join(args.out, f"journal_{route}.csv"), index=False)
        res.events.to_csv(os.path.join(args.out, f"events_{route}.csv"), index=False)
        res.equity.to_csv(os.path.join(args.out, f"equity_{route}.csv"), index=False)


if __name__ == "__main__":
    main()
