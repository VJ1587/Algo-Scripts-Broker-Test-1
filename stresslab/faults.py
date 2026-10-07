"""Data corruption the way real feeds fail.

Each fault takes clean bars (plus a seed) and returns corrupted bars; the
input frame is never modified.

``MUST_DETECT``: code must raise or warn; silently producing output is FAIL.
``MUST_SURVIVE``: legitimate ugliness; code must run and produce valid output.
``timezone_shift`` cannot be detected from the data itself, so surviving it is
reported as WARN with a reminder to confirm the broker's server offset.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_PRICES = ["open", "high", "low", "close"]


def missing_bars(bars, seed=0, frac=0.05):
    rng = np.random.default_rng(seed)
    keep = rng.random(len(bars)) >= frac
    keep[0] = keep[-1] = True
    return bars[keep].copy()


def duplicate_timestamps(bars, seed=0, n=5):
    rng = np.random.default_rng(seed)
    pos = np.sort(rng.choice(np.arange(1, len(bars) - 1), size=n, replace=False))
    dup = bars.iloc[pos]
    return pd.concat([bars, dup]).sort_index(kind="stable")


def out_of_order(bars, seed=0, block=20):
    rng = np.random.default_rng(seed)
    a = int(rng.integers(len(bars) // 4, len(bars) // 2))
    b = a + block
    parts = [bars.iloc[:a], bars.iloc[b:b + block], bars.iloc[a:b], bars.iloc[b + block:]]
    return pd.concat(parts)


def nan_values(bars, seed=0, n=5):
    rng = np.random.default_rng(seed)
    out = bars.copy()
    rows = rng.choice(np.arange(len(out) // 2, len(out)), size=n, replace=False)
    out.iloc[rows, out.columns.get_loc("close")] = np.nan
    return out


def bad_tick_spike(bars, seed=0, n=3, size=0.04):
    """A few bars whose high carries a bad tick (envelope still valid)."""
    rng = np.random.default_rng(seed)
    out = bars.copy()
    rows = rng.choice(np.arange(10, len(out) - 10), size=n, replace=False)
    col = out.columns.get_loc("high")
    out.iloc[rows, col] = out["high"].to_numpy()[rows] * (1 + size)
    return out


def stale_feed(bars, seed=0, length=48):
    """The feed freezes: price stuck at one value, zero volume."""
    rng = np.random.default_rng(seed)
    out = bars.copy()
    a = int(rng.integers(len(out) // 3, len(out) // 2))
    px = float(out["close"].iloc[a - 1])
    for c in _PRICES:
        out.iloc[a:a + length, out.columns.get_loc(c)] = px
    out.iloc[a:a + length, out.columns.get_loc("volume")] = 0.0
    # the first live bar after the freeze opens at the stale price
    nxt = a + length
    if nxt < len(out):
        out.iloc[nxt, out.columns.get_loc("open")] = px
        out.iloc[nxt, out.columns.get_loc("high")] = max(px, out["high"].iloc[nxt])
        out.iloc[nxt, out.columns.get_loc("low")] = min(px, out["low"].iloc[nxt])
    return out


def timezone_shift(bars, seed=0, hours=2):
    """MT5 broker server time labelled as UTC (e.g. server at UTC+2)."""
    out = bars.copy()
    out.index = out.index + pd.Timedelta(hours=hours)
    return out


def naive_timestamps(bars, seed=0):
    out = bars.copy()
    out.index = out.index.tz_localize(None)
    return out


def high_low_violation(bars, seed=0, n=5):
    rng = np.random.default_rng(seed)
    out = bars.copy()
    rows = rng.choice(np.arange(len(out) // 2, len(out)), size=n, replace=False)
    hcol = out.columns.get_loc("high")
    out.iloc[rows, hcol] = np.minimum(out["open"].to_numpy(), out["close"].to_numpy())[rows] * 0.999
    return out


def truncated_history(bars, seed=0, keep=200):
    return bars.iloc[-keep:].copy()


def price_scale_change(bars, seed=0, factor=100.0):
    """Quote convention changes mid series (e.g. a 3 versus 5 digit feed switch)."""
    out = bars.copy()
    a = len(out) * 2 // 3
    cols = [out.columns.get_loc(c) for c in _PRICES + (["spread"] if "spread" in out.columns else [])]
    out.iloc[a:, cols] = out.iloc[a:, cols] * factor
    return out


FAULTS = {
    "missing_bars": missing_bars,
    "duplicate_timestamps": duplicate_timestamps,
    "out_of_order": out_of_order,
    "nan_values": nan_values,
    "bad_tick_spike": bad_tick_spike,
    "stale_feed": stale_feed,
    "timezone_shift": timezone_shift,
    "naive_timestamps": naive_timestamps,
    "high_low_violation": high_low_violation,
    "truncated_history": truncated_history,
    "price_scale_change": price_scale_change,
}

MUST_DETECT = ("duplicate_timestamps", "out_of_order", "nan_values", "naive_timestamps",
               "high_low_violation", "price_scale_change")
MUST_SURVIVE = ("missing_bars", "bad_tick_spike", "stale_feed", "timezone_shift", "truncated_history")
UNDETECTABLE = ("timezone_shift",)


def apply_fault(name: str, bars: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    return FAULTS[name](bars, seed=seed)
