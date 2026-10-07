"""Golden master snapshots and cross platform parity checks."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from stresslab.result import FAIL, PASS, WARN, Result


def _to_jsonable(output):
    if isinstance(output, pd.DataFrame):
        df = output.reset_index(drop=True).copy()
        for c in df.columns:
            if isinstance(df[c].dtype, pd.DatetimeTZDtype) or pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = pd.DatetimeIndex(df[c]).strftime("%Y-%m-%dT%H:%M:%S%z")
        return {"kind": "dataframe", "columns": [str(c) for c in df.columns],
                "data": [[_scalar(v) for v in row] for row in df.itertuples(index=False, name=None)]}
    if isinstance(output, dict):
        return {"kind": "dict", "data": {str(k): _scalar(v) for k, v in output.items()}}
    raise TypeError(f"golden_check supports DataFrame or dict, got {type(output).__name__}")


def _scalar(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return None if math.isnan(v) else ("inf" if v == math.inf else ("-inf" if v == -math.inf else v))
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _close(a, b, rtol, atol=1e-12) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None or isinstance(a, str) or isinstance(b, str):
            return a == b
        return math.isclose(float(a), float(b), rel_tol=rtol, abs_tol=atol)
    return a == b


def golden_check(name: str, output, golden_dir, rtol: float = 1e-6, update: bool = False) -> Result:
    """Snapshot ``output`` to ``golden_dir/name.json``; later runs compare with tolerance."""
    path = Path(golden_dir) / f"{name}.json"
    snap = _to_jsonable(output)
    if update or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
        return Result(PASS if update else WARN,
                      f"golden {'updated' if update else 'created (nothing to compare yet)'}: {path.name}")
    gold = json.loads(path.read_text(encoding="utf-8"))
    if gold.get("kind") != snap["kind"]:
        return Result(FAIL, f"{name}: kind changed {gold.get('kind')} -> {snap['kind']}")
    if snap["kind"] == "dict":
        g, s = gold["data"], snap["data"]
        if set(g) != set(s):
            return Result(FAIL, f"{name}: keys changed {sorted(set(g) ^ set(s))}")
        bad = [k for k in g if not _close(g[k], s[k], rtol)]
        if bad:
            return Result(FAIL, f"{name}: {len(bad)} values changed, e.g. {bad[0]}: {g[bad[0]]} -> {s[bad[0]]}")
        return Result(PASS, f"{name}: {len(g)} values match golden")
    if gold["columns"] != snap["columns"]:
        return Result(FAIL, f"{name}: columns changed {gold['columns']} -> {snap['columns']}")
    if len(gold["data"]) != len(snap["data"]):
        return Result(FAIL, f"{name}: row count changed {len(gold['data'])} -> {len(snap['data'])}")
    for i, (ga, sa) in enumerate(zip(gold["data"], snap["data"], strict=True)):
        for c, x, y in zip(snap["columns"], ga, sa, strict=True):
            if not _close(x, y, rtol):
                return Result(FAIL, f"{name}: row {i} column {c} changed {x} -> {y}")
    return Result(PASS, f"{name}: {len(snap['data'])} rows match golden")


def parity_check(reference_df: pd.DataFrame, other_df: pd.DataFrame, key: str = "time",
                 cols=("side", "entry", "stop", "target"), price_tol: float = 1e-5,
                 time_tol=None) -> dict:
    """Compare a Python reference signal or trade list with an MT5 / TradingView export.

    Rows are matched on ``key`` (exact, or nearest within ``time_tol``).
    Returns counts and examples of rows only on one side, side mismatches and
    price mismatches, plus an overall ``status``.
    """
    ref = reference_df.copy()
    oth = other_df.copy()
    ref[key] = pd.DatetimeIndex(ref[key])
    oth[key] = pd.DatetimeIndex(oth[key])
    ref = ref.sort_values(key).reset_index(drop=True)
    oth = oth.sort_values(key).reset_index(drop=True)
    ref["_rid"] = np.arange(len(ref))
    oth["_oid"] = np.arange(len(oth))
    tol = pd.Timedelta(0 if time_tol is None else time_tol)
    if tol > pd.Timedelta(0):
        m = pd.merge_asof(ref, oth, on=key, direction="nearest", tolerance=tol, suffixes=("_ref", "_oth"))
        m = m.dropna(subset=["_oid"])
        m = m.drop_duplicates(subset=["_oid"], keep="first")
    else:
        m = ref.merge(oth, on=key, how="inner", suffixes=("_ref", "_oth"))
    only_ref = ref[~ref["_rid"].isin(m["_rid"])][key].tolist()
    only_oth = oth[~oth["_oid"].isin(m["_oid"])][key].tolist()
    side_mm, price_mm = [], []
    for c in cols:
        a, b = f"{c}_ref", f"{c}_oth"
        if a not in m.columns or b not in m.columns:
            continue
        if c == "side":
            bad = m[m[a].astype(float) != m[b].astype(float)]
            side_mm += bad[key].tolist()
        else:
            diff = (m[a].astype(float) - m[b].astype(float)).abs()
            bad = m[diff > price_tol]
            price_mm += [(t, c, float(x), float(y)) for t, x, y in zip(bad[key], bad[a], bad[b], strict=True)]
    status = PASS if not (only_ref or only_oth or side_mm or price_mm) else FAIL
    return {"status": status, "matched": int(len(m)), "only_in_reference": only_ref,
            "only_in_other": only_oth, "side_mismatch": side_mm, "price_mismatch": price_mm,
            "reason": (f"{len(m)} matched, {len(only_ref)} only in reference, {len(only_oth)} only in other, "
                       f"{len(side_mm)} side and {len(price_mm)} price mismatches")}

