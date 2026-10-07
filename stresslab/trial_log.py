"""CSV ledger of every backtest trial.

Logging every configuration tried (not only the winner) gives the Deflated
Sharpe Ratio an honest trial count and an honest variance of Sharpe ratios.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

FIELDS = ["timestamp_utc", "strategy", "params_hash", "params_json", "sharpe", "n_obs", "note"]


def params_hash(params: dict) -> str:
    blob = json.dumps(params, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob, usedforsecurity=False).hexdigest()[:12]


def log_trial(path, strategy: str, params: dict, sharpe: float, n_obs: int, note: str = "") -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "strategy": strategy,
           "params_hash": params_hash(params), "params_json": json.dumps(params, sort_keys=True, default=str),
           "sharpe": float(sharpe), "n_obs": int(n_obs), "note": note}
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return row


def _rows(path, strategy=None):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if strategy is None or r["strategy"] == strategy]


def trial_count(path, strategy: str | None = None, distinct: bool = True) -> int:
    """Number of trials logged (distinct parameter sets by default)."""
    rows = _rows(path, strategy)
    return len({(r["strategy"], r["params_hash"]) for r in rows}) if distinct else len(rows)


def sharpe_variance(path, strategy: str | None = None) -> float:
    """Variance of the Sharpe ratios across logged trials (NaN if fewer than 2)."""
    vals = np.array([float(r["sharpe"]) for r in _rows(path, strategy)], dtype=float)
    vals = vals[np.isfinite(vals)]
    return float(vals.var(ddof=1)) if len(vals) >= 2 else float("nan")
