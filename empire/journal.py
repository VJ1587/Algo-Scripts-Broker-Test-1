"""Journal: the memory that lets each report build on the last.

    forecasts.csv           every forecast, written before the outcome, resolved and scored after
    personality_changes.csv drift beyond +/-2 for 60+ days: a rewritten profile with a date and a suspected cause
    flips.csv               relationship sign flips, opened and closed
    hypotheses_log.csv      each behavior's status at every monthly review (validated / invalidated / ...)
    alignment_log.csv       plan goal alignment scores over time (pivot triggers on sign change)
    params_state.json       live parameters (T, lambda, thresholds) with full change history
    params_changelog.csv    every parameter change, its reason and the evidence behind it
    ledger_changes.csv      every ledger edit seen between runs (hash, version, fields changed)
    runs.csv                one line per run
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .config import stable_hash
from .stories import DIR, likelihood

FORECAST_FIELDS = ["forecast_id", "made_date", "run_type", "instrument", "horizon_days", "expiry_date", "price",
                   "zone_up", "zone_down", "zone_up_edge", "zone_down_edge", "pi_up", "pi_down", "pi_range",
                   "W_up", "W_down", "W_range", "pressure", "T", "lambda", "P_up", "P_down", "P_range", "favored",
                   "p_dir", "p_reach", "p_hold", "p_opp", "grade", "setup_type", "trade_type", "gates", "gate_status",
                   "regime", "alignment_cells", "story_dir", "excluded_inputs", "config_version", "ledger_version",
                   "params_version", "source", "status", "outcome", "resolved_date", "brier", "logloss", "hit"]


class Journal:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.dir / name

    # ------------------------------------------------------------ generic csv helpers
    def read(self, name: str) -> pd.DataFrame:
        p = self.path(name)
        if not p.exists() or p.stat().st_size == 0:
            return pd.DataFrame()
        return pd.read_csv(p, dtype=str, keep_default_na=False)

    def append(self, name: str, rows: list[dict], fields: Optional[list[str]] = None) -> None:
        if not rows:
            return
        p = self.path(name)
        fields = fields or list(rows[0].keys())
        new = not p.exists() or p.stat().st_size == 0
        if not new:
            with open(p, newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh), None)
            if header and header != fields:
                fields = header + [f for f in fields if f not in header]
                if fields != header:
                    old = pd.read_csv(p, dtype=str, keep_default_na=False)
                    for f in fields:
                        if f not in old:
                            old[f] = ""
                    old[fields].to_csv(p, index=False)
        with open(p, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: _cell(r.get(k)) for k in fields})

    def rewrite(self, name: str, df: pd.DataFrame) -> None:
        df.to_csv(self.path(name), index=False)

    # ------------------------------------------------------------ parameters (change control)
    def params(self, defaults: dict) -> dict:
        p = self.path("params_state.json")
        if p.exists():
            st = json.loads(p.read_text(encoding="utf-8"))
            for k, v in defaults.items():
                st["values"].setdefault(k, v)
            return st
        st = {"version": "params-1", "values": dict(defaults), "history": []}
        self.save_params(st)
        return st

    def save_params(self, st: dict) -> None:
        self.path("params_state.json").write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")

    def change_param(self, st: dict, key: str, new: Any, reason: str, evidence: str, run: str) -> None:
        old = st["values"].get(key)
        if old == new:
            return
        st["values"][key] = new
        n = int(str(st.get("version", "params-0")).split("-")[-1]) + 1
        st["version"] = f"params-{n}"
        entry = {"version": st["version"], "date": run, "key": key, "old": old, "new": new, "reason": reason,
                 "evidence": evidence}
        st["history"].append(entry)
        self.append("params_changelog.csv", [entry])

    # ------------------------------------------------------------ ledger change control
    def ledger_snapshot(self, ledger_raw: dict, path: Path, run: str) -> dict:
        snap_dir = self.path("ledger_snapshots")
        snap_dir.mkdir(exist_ok=True)
        h = stable_hash(ledger_raw)
        dest = snap_dir / f"ledger_{h}.json"
        prev = self.read("ledger_changes.csv")
        last_hash = prev["hash"].iloc[-1] if not prev.empty else None
        changes: list[str] = []
        if last_hash != h:
            if last_hash and (snap_dir / f"ledger_{last_hash}.json").exists():
                old = json.loads((snap_dir / f"ledger_{last_hash}.json").read_text(encoding="utf-8"))
                changes = diff_paths(old, ledger_raw)
            if not dest.exists():
                dest.write_text(json.dumps(ledger_raw, indent=1, default=str), encoding="utf-8")
            self.append("ledger_changes.csv", [{"run": run, "hash": h, "ledger_version": ledger_raw.get("ledger_version"),
                                                 "changed": "; ".join(changes[:60]) or ("first snapshot" if not last_hash else "")}])
        return {"hash": h, "changed": changes, "new": last_hash != h}


def diff_paths(a: Any, b: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            out += diff_paths(a.get(k), b.get(k), f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(a, list) and isinstance(b, list):
        ida = {x.get("id"): x for x in a if isinstance(x, dict) and x.get("id")}
        idb = {x.get("id"): x for x in b if isinstance(x, dict) and x.get("id")}
        if ida or idb:
            for k in sorted(set(ida) | set(idb), key=str):
                out += diff_paths(ida.get(k), idb.get(k), f"{prefix}[{k}]")
        elif a != b:
            out.append(prefix)
    elif a != b:
        out.append(f"{prefix}: {a!r} -> {b!r}" if not isinstance(a, (dict, list)) and not isinstance(b, (dict, list)) else prefix)
    return out


def _cell(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str, sort_keys=True)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ""
    if isinstance(v, float):
        return round(v, 6)
    return "" if v is None else v


# =============================================================================
# Forecasts: resolve and score
# =============================================================================

def resolve_forecasts(fc: pd.DataFrame, bars: dict[str, pd.DataFrame], asof: pd.Timestamp) -> tuple[pd.DataFrame, list[dict]]:
    """An open forecast resolves when price first reaches the up zone's near edge (up), the down zone's
    near edge (down), or the horizon ends (range). A same-day double touch scores half to each."""
    resolved = []
    if fc.empty:
        return fc, resolved
    fc = fc.copy()
    for i, f in fc.iterrows():
        if f.get("status") != "open":
            continue
        b = bars.get(f["instrument"])
        if b is None or b.empty:
            continue
        made, exp = pd.Timestamp(f["made_date"]), pd.Timestamp(f["expiry_date"])
        fut = b[(b.index > made) & (b.index <= min(exp, asof))]
        up_e = _f(f.get("zone_up_edge"))
        dn_e = _f(f.get("zone_down_edge"))
        outcome = None
        when = None
        for t, row in fut.iterrows():
            hu = up_e is not None and row["high"] >= up_e
            hd = dn_e is not None and row["low"] <= dn_e
            if hu and hd:
                outcome, when = "both", t
                break
            if hu:
                outcome, when = "up", t
                break
            if hd:
                outcome, when = "down", t
                break
        if outcome is None and asof >= exp and (len(fut) and fut.index[-1] >= exp - pd.Timedelta(days=3)):
            outcome, when = "range", exp
        if outcome is None:
            continue
        P = {k: _f(f.get(f"P_{k}")) or 0.0 for k in DIR}
        y = {k: 0.0 for k in DIR}
        if outcome == "both":
            y["up"] = y["down"] = 0.5
        else:
            y[outcome] = 1.0
        brier = sum((P[k] - y[k]) ** 2 for k in DIR)
        ll = -sum(y[k] * math.log(max(P[k], 1e-6)) for k in DIR)
        fav = f.get("favored")
        hit = y.get(fav, 0.0) if fav in DIR else None
        fc.at[i, "status"] = "resolved"
        fc.at[i, "outcome"] = outcome
        fc.at[i, "resolved_date"] = str(pd.Timestamp(when).date())
        fc.at[i, "brier"] = str(round(brier, 4))
        fc.at[i, "logloss"] = str(round(ll, 4))
        fc.at[i, "hit"] = "" if hit is None else str(hit)
        resolved.append(dict(fc.loc[i]))
    return fc, resolved


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def scorecard(fc: pd.DataFrame, since: Optional[pd.Timestamp] = None, live_only: bool = True) -> dict:
    """Hit rates by setup, grade, gate status and alignment cell; Brier and log loss; calibration buckets."""
    if fc.empty:
        return {"n": 0}
    d = fc[fc["status"] == "resolved"].copy()
    if live_only and "source" in d:
        d = d[d["source"] != "replay"]
    if since is not None:
        d = d[pd.to_datetime(d["resolved_date"]) >= since]
    if d.empty:
        return {"n": 0}
    for c in ("brier", "logloss", "hit", "p_dir"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    out: dict[str, Any] = {"n": int(len(d)), "brier": round(float(d["brier"].mean()), 4),
                           "logloss": round(float(d["logloss"].mean()), 4)}
    base = d[["pi_up", "pi_down", "pi_range"]].apply(pd.to_numeric, errors="coerce")
    y = pd.DataFrame({k: (d["outcome"] == k).astype(float) + 0.5 * ((d["outcome"] == "both") & (k != "range")) for k in DIR})
    bb = ((base.values - y[["up", "down", "range"]].values) ** 2).sum(axis=1)
    out["brier_base_rates"] = round(float(np.mean(bb)), 4)
    out["skill_vs_base"] = None if out["brier_base_rates"] == 0 else round(1 - out["brier"] / out["brier_base_rates"], 3)

    def group(col: str) -> list[dict]:
        rows = []
        for k, g in d.groupby(col):
            h = g["hit"].dropna()
            rows.append({col: k, "n": int(len(g)), "hit_rate": None if h.empty else round(float(h.mean()), 3),
                         "mean_p_dir": round(float(g["p_dir"].mean()), 3) if g["p_dir"].notna().any() else None,
                         "judge": "too few to judge (<30)" if len(g) < 30 else ""})
        return rows

    out["by_setup"] = group("setup_type")
    out["by_grade"] = group("grade")
    out["by_gate_status"] = group("gate_status")
    out["by_trade_type"] = group("trade_type")
    cal = []
    pfav = d["p_dir"]
    for lo in np.arange(0.3, 1.0, 0.1):
        sel = (pfav >= lo) & (pfav < lo + 0.1)
        g = d[sel]
        if len(g):
            cal.append({"bucket": f"{lo:.1f}-{lo + 0.1:.1f}", "n": int(len(g)), "predicted": round(float(g["p_dir"].mean()), 3),
                        "actual": round(float(g["hit"].mean()), 3)})
    out["calibration"] = cal
    cells: dict[str, dict] = {}
    for _, r in d.iterrows():
        try:
            ac = json.loads(r.get("alignment_cells") or "{}")
        except ValueError:
            ac = {}
        for scanner, cell in ac.items():
            cc = cells.setdefault(scanner, {})
            s = cc.setdefault(cell, {"n": 0, "hits": 0.0})
            s["n"] += 1
            s["hits"] += float(r["hit"]) if pd.notna(r["hit"]) else 0.0
    out["alignment"] = {sc: {cell: {"n": v["n"], "hit_rate": round(v["hits"] / v["n"], 3)} for cell, v in cc.items()}
                        for sc, cc in cells.items()}
    return out


def fit_temperature(fc: pd.DataFrame, T_grid: list[float], lam_grid: list[float]) -> Optional[dict]:
    """Choose T and lambda that minimize log loss on resolved live forecasts (so 70 percent calls happen
    about 70 percent of the time)."""
    d = fc[(fc.get("status") == "resolved") & (fc.get("source") != "replay")] if not fc.empty else fc  # caller filters live/demo
    if d is None or d.empty:
        return None
    rows = []
    for _, r in d.iterrows():
        pi = {k: _f(r.get(f"pi_{k}")) for k in DIR}
        W = {k: _f(r.get(f"W_{k}")) for k in DIR}
        if None in pi.values() or None in W.values():
            continue
        y = {k: 0.0 for k in DIR}
        if r["outcome"] == "both":
            y["up"] = y["down"] = 0.5
        elif r["outcome"] in DIR:
            y[r["outcome"]] = 1.0
        rows.append((pi, W, _f(r.get("pressure")) or 0.0, y))
    if not rows:
        return None
    best = None
    for T in T_grid:
        for lam in lam_grid:
            ll = 0.0
            for pi, W, pr, y in rows:
                P = likelihood(pi, W, pr, lam, T)["P"]
                ll -= sum(y[k] * math.log(max(P[k], 1e-6)) for k in DIR)
            ll /= len(rows)
            if best is None or ll < best["logloss"] - 1e-9:
                best = {"T": T, "lambda": lam, "logloss": round(ll, 5), "n": len(rows)}
    return best
