"""Invariant checks: lookahead, schema, bounds, determinism, discrimination.

Each check returns a ``Result`` (PASS / WARN / FAIL plus a one line reason).
"""
from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd

from stresslab.adapters import SIGNAL_COLUMNS, validate_signals
from stresslab.result import FAIL, PASS, WARN, Result

_CMP_COLS = ["time", "side", "entry", "stop"]


def _norm_signals(df: pd.DataFrame, decimals: int = 8) -> pd.DataFrame:
    out = df[_CMP_COLS].copy()
    out["time"] = pd.DatetimeIndex(out["time"])
    out["side"] = out["side"].astype(int)
    for c in ("entry", "stop"):
        out[c] = out[c].astype(float).round(decimals)
    return out.sort_values(["time", "side"], kind="stable").reset_index(drop=True)


def _checkpoints(n: int, signal_pos, n_points: int = 12, min_pos: int = 0) -> list[int]:
    """Cut positions: half evenly spaced, half right after signal bars.

    Cutting right after a signal bar makes the signal the LAST bar of the
    truncated history, which is where any use of future data shows up.
    """
    lo = max(min_pos, n // 5)
    even = np.linspace(lo, n - 2, n_points // 2).astype(int).tolist()
    sig = sorted({int(p) for p in signal_pos if lo <= p < n - 1})
    if sig:
        pick = np.linspace(0, len(sig) - 1, min(len(sig), n_points - len(even))).astype(int)
        even += [sig[k] for k in pick]
    return sorted(set(even))


def check_scanner_no_lookahead(scanner, bars: pd.DataFrame, n_points: int = 12, decimals: int = 8) -> Result:
    """Truncation test: signals at or before a cut must not change when later bars are removed."""
    full = scanner.scan(bars)
    if full is None or len(full) == 0:
        return Result(WARN, "scanner produced no signals on the full history; lookahead not testable")
    full_n = _norm_signals(full, decimals)
    pos = bars.index.get_indexer(pd.DatetimeIndex(full_n["time"]))
    cuts = _checkpoints(len(bars), pos, n_points)
    bad = []
    for cut in cuts:
        t_cut = bars.index[cut]
        part = scanner.scan(bars.iloc[:cut + 1])
        a = full_n[full_n["time"] <= t_cut].reset_index(drop=True)
        b = _norm_signals(part, decimals) if part is not None and len(part) else full_n.iloc[0:0]
        b = b[b["time"] <= t_cut].reset_index(drop=True)
        if len(a) != len(b) or not a.equals(b):
            merged = a.merge(b, how="outer", indicator=True)
            diff = merged[merged["_merge"] != "both"]
            first = diff["time"].min() if len(diff) else "?"
            bad.append(f"cut {t_cut}: {len(diff)} differing rows (first at {first})")
    if bad:
        return Result(FAIL, f"lookahead: {len(bad)}/{len(cuts)} cuts changed past signals; " + bad[0])
    return Result(PASS, f"{len(cuts)} truncation cuts, {len(full_n)} signals unchanged")


def check_personality_no_lookahead(model, bars: pd.DataFrame, n_points: int = 12, atol: float = 1e-8) -> Result:
    """``assess_series`` at bar t must equal ``assess(bars[:t+1])``."""
    if not hasattr(model, "assess_series"):
        return Result(PASS, "no assess_series; point in time assess only")
    series = model.assess_series(bars)
    min_bars = int(getattr(model, "min_bars", 0))
    cuts = np.linspace(max(min_bars, len(bars) // 5), len(bars) - 1, n_points).astype(int)
    bad = []
    for t in sorted(set(cuts.tolist())):
        point = model.assess(bars.iloc[:t + 1])
        row = series.iloc[t]
        for k, v in point.items():
            if k not in row.index:
                continue
            s = float(row[k])
            v = float(v)
            same = (math.isnan(s) and math.isnan(v)) or math.isclose(s, v, rel_tol=1e-7, abs_tol=atol)
            if not same:
                bad.append(f"{k} at {bars.index[t]}: series {s:.6g} vs point in time {v:.6g}")
    if bad:
        return Result(FAIL, f"lookahead: {len(bad)} mismatches; " + bad[0])
    return Result(PASS, f"assess_series matches point in time assess at {len(set(cuts.tolist()))} bars")


def _equal(a, b) -> bool:
    if isinstance(a, pd.DataFrame) and isinstance(b, pd.DataFrame):
        try:
            pd.testing.assert_frame_equal(a, b, check_exact=True)
            return True
        except AssertionError:
            return False
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or a == b
    return bool(a == b)


def check_determinism(run_fn, label: str = "output", n: int = 2) -> Result:
    """Calling ``run_fn()`` repeatedly must give identical output."""
    first = run_fn()
    for k in range(1, n):
        if not _equal(first, run_fn()):
            return Result(FAIL, f"{label} differs between identical runs (run {k + 1})")
    return Result(PASS, f"{label} identical across {n} runs")


def check_personality_bounds(model, bars_list) -> Result:
    bounds = model.output_bounds
    bad = []
    for bars in bars_list:
        out = model.assess(bars)
        missing = set(bounds) - set(out)
        if missing:
            bad.append(f"missing outputs {sorted(missing)}")
        for k, v in out.items():
            if k not in bounds:
                continue
            lo, hi = bounds[k]
            if not np.isfinite(v) or v < lo or v > hi:
                bad.append(f"{k}={v} outside [{lo}, {hi}]")
    if bad:
        return Result(FAIL, f"{len(bad)} bound violations; " + bad[0])
    return Result(PASS, f"{len(bounds)} outputs finite and in bounds on {len(bars_list)} datasets")


def check_scanner_schema(scanner, bars: pd.DataFrame) -> Result:
    sig = scanner.scan(bars)
    if not isinstance(sig, pd.DataFrame):
        return Result(FAIL, f"scan returned {type(sig).__name__}, expected DataFrame")
    missing = [c for c in SIGNAL_COLUMNS if c not in sig.columns]
    if missing:
        return Result(FAIL, f"missing signal columns {missing}")
    problems = validate_signals(sig)
    if len(sig):
        t = pd.DatetimeIndex(sig["time"])
        if t.tz is None:
            problems.append("signal times are timezone naive")
        else:
            if (t > bars.index[-1]).any():
                problems.append(f"{int((t > bars.index[-1]).sum())} signals after the last bar")
            off = ~t.isin(bars.index)
            if off.any():
                problems.append(f"{int(off.sum())} signal times not on bar boundaries")
    if problems:
        return Result(FAIL, "; ".join(problems))
    return Result(PASS, f"{len(sig)} signals, schema valid")


def check_personality_discrimination(model, bars_by_regime: dict, metric: str, expect_order) -> Result:
    """Ground truth test: ``metric`` must be lower in regime_low than regime_high.

    ``bars_by_regime`` maps regime name to a list of bars samples; samples are
    compared pairwise in order. PASS at >= 90% correct, WARN at >= 60%.
    """
    low, high = expect_order
    a, b = bars_by_regime[low], bars_by_regime[high]
    n = min(len(a), len(b))
    if n == 0:
        return Result(WARN, "no samples")
    correct = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for x, y in zip(a[:n], b[:n], strict=True):
            if model.assess(x)[metric] < model.assess(y)[metric]:
                correct += 1
    reason = f"{metric}: {low} < {high} in {correct}/{n} samples"
    if correct >= math.ceil(0.9 * n):
        return Result(PASS, reason)
    if correct >= math.ceil(0.6 * n):
        return Result(WARN, reason)
    return Result(FAIL, reason)
