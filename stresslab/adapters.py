"""Protocols that real user scripts get wrapped in.

The harness only talks to these interfaces; it never touches user internals.
Wrap an existing scanner, personality model, executor or journal in a thin
class that satisfies the matching protocol.

Bars schema
-----------
* index: tz aware UTC ``DatetimeIndex``, strictly increasing, no duplicates.
  Each label is the bar OPEN time.
* columns: ``open high low close volume``; optional ``spread`` in price units.

Signals schema
--------------
``time, side, entry, stop, target`` where ``time`` is the label of the bar
whose close produced the signal and ``side`` is +1 (long) or -1 (short).
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import numpy as np
import pandas as pd

BAR_COLUMNS = ("open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")
SIGNAL_COLUMNS = ("time", "side", "entry", "stop", "target")


@runtime_checkable
class PersonalityModel(Protocol):
    """Scores an instrument's character from bars.

    ``output_bounds`` maps every output name to its (lo, hi) range.
    Optional: ``expectations`` maps a metric to ``[regime_low, regime_high]``
    (synthetic regime names whose metric should be low and high), and
    ``assess_series(bars)`` returns one row per bar.
    """

    output_bounds: dict[str, tuple[float, float]]

    def assess(self, bars: pd.DataFrame) -> dict[str, float]: ...


@runtime_checkable
class Scanner(Protocol):
    def scan(self, bars: pd.DataFrame) -> pd.DataFrame: ...


@runtime_checkable
class Executor(Protocol):
    def on_signal(self, signal_row: Any, broker: Any) -> None: ...


@runtime_checkable
class Journal(Protocol):
    def record(self, trade: dict) -> None: ...

    def summary(self) -> dict: ...


def validate_bars(bars: Any) -> list[str]:
    """Return a list of problems with a bars frame (empty list means valid)."""
    problems: list[str] = []
    if not isinstance(bars, pd.DataFrame):
        return [f"bars must be a DataFrame, got {type(bars).__name__}"]
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        problems.append(f"missing columns: {missing}")
    idx = bars.index
    if not isinstance(idx, pd.DatetimeIndex):
        problems.append(f"index is {type(idx).__name__}, not DatetimeIndex")
    else:
        if idx.tz is None:
            problems.append("index is timezone naive (must be tz aware UTC)")
        elif str(idx.tz) != "UTC":
            problems.append(f"index timezone is {idx.tz}, expected UTC")
        if idx.has_duplicates:
            problems.append(f"{int(idx.duplicated().sum())} duplicate timestamps")
        if not idx.is_monotonic_increasing:
            problems.append("index is out of order (not increasing)")
    present = [c for c in PRICE_COLUMNS if c in bars.columns]
    cols = present + (["volume"] if "volume" in bars.columns else [])
    if cols:
        n_nan = int(bars[cols].isna().sum().sum())
        if n_nan:
            problems.append(f"{n_nan} NaN values in {cols}")
    if len(present) == 4:
        o, h, lo, c = (bars[k] for k in PRICE_COLUMNS)
        bad_hi = (h < np.maximum(o, c)) | (h < lo)
        bad_lo = lo > np.minimum(o, c)
        n_env = int((bad_hi | bad_lo).sum())
        if n_env:
            problems.append(f"{n_env} bars violate the high/low envelope")
        n_nonpos = int((bars[present] <= 0).any(axis=1).sum())
        if n_nonpos:
            problems.append(f"{n_nonpos} bars with non positive prices")
    if "spread" in bars.columns and (bars["spread"] < 0).any():
        problems.append("negative spread values")
    return problems


def validate_signals(df: Any) -> list[str]:
    """Return a list of problems with a signals frame (empty list means valid)."""
    if not isinstance(df, pd.DataFrame):
        return [f"signals must be a DataFrame, got {type(df).__name__}"]
    missing = [c for c in SIGNAL_COLUMNS if c not in df.columns]
    if missing:
        return [f"missing signal columns: {missing}"]
    problems: list[str] = []
    if df.empty:
        return problems
    side = df["side"]
    bad_side = ~side.isin([1, -1])
    if bad_side.any():
        problems.append(f"{int(bad_side.sum())} signals with side not +1/-1")
    prices = df[["entry", "stop", "target"]]
    n_nan = int(prices.isna().any(axis=1).sum())
    if n_nan:
        problems.append(f"{n_nan} signals with NaN prices")
    long_bad = (side == 1) & (df["stop"] >= df["entry"])
    short_bad = (side == -1) & (df["stop"] <= df["entry"])
    if long_bad.any():
        problems.append(f"{int(long_bad.sum())} long signals with stop at or above entry")
    if short_bad.any():
        problems.append(f"{int(short_bad.sum())} short signals with stop at or below entry")
    return problems
