"""Personality evolution: phases over the whole history, lined up with politics, policy and market regimes.

Personality is slow. For countries and economies it moves in steps around elections, changes of party and
policy regimes, not day to day. This module supplies the pieces for the evolution page:

  load_politics / timeline   ledger/politics.yaml (offices, elections, control shifts, policy regimes) plus the
                             owner's events file, as one dated list
  countries_for              which countries' politics bear on an instrument
  classify_phases            per period: stable, a return to a known phase, a new phase, or a complete change,
                             plus behavior flips (a sensitivity changing sign)
  regime_mix                 share of days in each market regime per period
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from .config import load_yaml

BETAS = ("fear_beta", "rate_beta", "resource_beta")
PHASES = ("warm-up", "stable", "known phase", "new phase", "complete change")


# =============================================================================
# Politics and policy timeline
# =============================================================================

def load_politics(path: Path) -> dict:
    return load_yaml(path) if path.exists() else {}


def _d(x) -> Optional[pd.Timestamp]:
    if x is None or (isinstance(x, str) and not x.strip()):
        return None
    return pd.Timestamp(str(x)).normalize()


def timeline(pol: dict, owner_events: Optional[list] = None) -> list[dict]:
    """Every dated item as {date, country, kind, text, control_shift, evidence, check}. Leadership changes come
    from consecutive office holders; a change of party in an office that has parties is a change of control."""
    out: list[dict] = []
    ev_default = pol.get("default_evidence", "unverified")
    for cc, c in (pol.get("countries") or {}).items():
        for office, holders in (c.get("offices") or {}).items():
            for prev, cur in zip(holders, holders[1:]):
                d = _d(cur.get("start"))
                if d is None:
                    continue
                party_change = bool(cur.get("party") and prev.get("party") and cur["party"] != prev["party"])
                pp = f" ({prev['party']})" if prev.get("party") else ""
                cp = f" ({cur['party']})" if cur.get("party") else ""
                out.append({"date": d, "country": cc, "kind": "leader", "office": office,
                            "text": f"{office}: {prev['holder']}{pp} -> {cur['holder']}{cp}",
                            "control_shift": party_change, "evidence": cur.get("evidence", ev_default),
                            "check": cur.get("check")})
        for e in c.get("events") or []:
            d = _d(e.get("date"))
            if d is None:
                continue
            out.append({"date": d, "country": cc, "kind": e.get("kind", "event"), "text": e.get("text", ""),
                        "control_shift": bool(e.get("control_shift")), "evidence": e.get("evidence", ev_default),
                        "check": e.get("check")})
    for e in owner_events or []:
        d = _d(e.get("date"))
        if d is None:
            continue
        out.append({"date": d, "country": e.get("player") or "", "kind": e.get("type", "event"),
                    "text": f"{e.get('headline', '')} (events file {e.get('id', '')})", "control_shift": False,
                    "evidence": e.get("evidence", "unknown"), "check": None})
    out.sort(key=lambda x: x["date"])
    return out


def countries_for(sym: str, inst: Any, pol: dict) -> list[str]:
    fixed = (pol.get("instrument_countries") or {}).get(sym)
    if fixed:
        return list(fixed)
    cmap = pol.get("currency_countries") or {}
    ccys = list(getattr(inst, "currencies", []) or []) if inst is not None else []
    if not ccys and sym.endswith("_IDX"):
        ccys = [sym[:3]]
    if not ccys and len(sym) == 6:
        ccys = [sym[:3], sym[3:]]
    return [cmap[c] for c in ccys if c in cmap] or ["US"]


def period_label(ts: pd.Timestamp, kind: str) -> str:
    return str(ts.year) if kind == "annual" else f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def events_by_period(tl: list[dict], kind: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in tl:
        out.setdefault(period_label(e["date"], kind), []).append(e)
    return out


def regime_mix(regimes: Optional[pd.Series], kind: str) -> dict[str, dict]:
    """Share of trading days in each market regime per period, and how many times the regime switched."""
    if regimes is None or len(regimes) == 0:
        return {}
    r = regimes.dropna()
    out = {}
    for ts, g in r.groupby(r.index.year if kind == "annual" else [r.index.year, r.index.quarter]):
        lab = str(ts) if kind == "annual" else f"{ts[0]}-Q{ts[1]}"
        share = g.value_counts(normalize=True)
        out[lab] = {"main": str(share.index[0]), "share": round(float(share.iloc[0]), 2),
                    "mix": {str(k): round(float(v), 2) for k, v in share.items()},
                    "switches": int((g != g.shift()).sum() - 1)}
    return out


# =============================================================================
# Phases: did the personality change completely, or move to another phase?
# =============================================================================

def classify_phases(med: pd.DataFrame, z: pd.DataFrame, warmup: int = 5) -> list[dict]:
    """One row per period after the first, from period medians (`med`) and their robust z scores (`z`).

    shift    mean |z_t - z_t-1| across traits: how far it moved since the last period
    novelty  mean |z_t - z_s| to the closest earlier period s (skipping the one just before): how unlike its past
    nearest  that closest earlier period

    known phase      moved a lot (shift >= own 75th pct) but looks like a phase it has been in before
    new phase        unlike any earlier period (novelty >= own 90th pct): a critical phase, behavior may differ
    complete change  a new phase that held: this period and the last are both new, and close to each other
    stable           everything else
    Thresholds come from each instrument's own history, so a calm instrument and a wild one are each judged
    against themselves. The first `warmup` periods have too little history to compare (with three earlier years
    almost anything looks new) and are marked warm-up.
    flips: a sensitivity (fear, rate, resource beta) that changed sign, with both values at least half its typical
    size and a move of 1.5 robust sd or more, for example a currency that used to rise in fear and now falls.
    On the 2026 live history this keeps about 3 percent of beta-years (one or two flips per instrument in 20 years).
    """
    idx = list(z.index)
    n = len(idx)
    if n < 2:
        return []
    Z = z.to_numpy(dtype=float)
    shift = np.full(n, np.nan)
    novelty = np.full(n, np.nan)
    nearest: list[Optional[int]] = [None] * n
    for t in range(1, n):
        shift[t] = np.nanmean(np.abs(Z[t] - Z[t - 1]))
        if t >= 2:
            d = np.nanmean(np.abs(Z[:t - 1] - Z[t]), axis=1)
            if np.isfinite(d).any():
                k = int(np.nanargmin(d))
                novelty[t], nearest[t] = float(d[k]), k
    ok = np.arange(n) >= warmup
    s75 = np.nanpercentile(shift[ok], 75) if np.isfinite(shift[ok]).any() else np.inf
    n90 = np.nanpercentile(novelty[ok], 90) if np.isfinite(novelty[ok]).any() else np.inf
    # a sign change only counts when both sides are at least half the beta's typical size and it moved 1.5 sd
    noise = {b: 0.5 * float(med[b].abs().median()) for b in BETAS if b in med and med[b].notna().sum() > 3}
    zc = list(z.columns)
    rows = []
    for t in range(1, n):
        if t < warmup:
            phase = "warm-up"
        elif novelty[t] >= n90 and t - 1 >= warmup and novelty[t - 1] >= n90 and shift[t] < s75:
            phase = "complete change"
        elif novelty[t] >= n90:
            phase = "new phase"
        elif shift[t] >= s75:
            phase = "known phase"
        else:
            phase = "stable"
        flips = []
        for b, th in noise.items():
            a, c = med[b].iloc[t - 1], med[b].iloc[t]
            dz = abs(Z[t, zc.index(b)] - Z[t - 1, zc.index(b)]) if b in zc else np.nan
            if pd.notna(a) and pd.notna(c) and np.sign(a) != np.sign(c) and min(abs(a), abs(c)) > th and dz >= 1.5:
                flips.append(f"{b} {'+' if a > 0 else '-'} to {'+' if c > 0 else '-'}")
        rows.append({"t": t, "shift": None if np.isnan(shift[t]) else round(float(shift[t]), 2),
                     "novelty": None if np.isnan(novelty[t]) else round(float(novelty[t]), 2),
                     "nearest": nearest[t], "phase": phase, "flips": flips,
                     "moved": [f"{c} {'up' if Z[t, j] > Z[t - 1, j] else 'down'}" for j, c in enumerate(z.columns)
                               if np.isfinite(Z[t, j] - Z[t - 1, j]) and abs(Z[t, j] - Z[t - 1, j]) >= 2]})
    return rows
