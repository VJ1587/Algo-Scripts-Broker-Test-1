"""Event engine: materiality, event study, cross-market fingerprint, needle test, event weight, pressure.

Events come from two places:
  1. personality_events.yaml, written by the owner (wars, sanctions, policy shifts, confirmed headlines).
  2. Forex Factory red-folder releases already archived by the scanner (data/calendar_snapshots). These are
     scheduled official releases; their market reaction is measured from our own price data.
Only tier 1 (primary source) or confirmed events can change a score. Noise and bait verdicts never do.
"""
from __future__ import annotations

import math
import re
from typing import Optional

import numpy as np
import pandas as pd

from .config import evidence_ok
from .data import trade_dates

QUALIFYING_QUESTIONS = [
    "Who released this, and what do they gain from its release now?",
    "Is it new information, or old information repackaged?",
    "Did the market move before the release? If so, who knew, and is the move already done?",
    "Does official data or a primary source confirm it, or is it a single unnamed source?",
    "Which player's stated plan does this advance or set back?",
    "Which lever does it touch, and who holds that lever?",
    "Does the cross market fingerprint agree with the headline?",
    "Is there a second, independent event pointing to the same outcome?",
    "Who is on the other side of the trade this news invites me to take?",
    "Is positioning already crowded in the direction the news points? If so, who is left to buy?",
    "Did it land in thin liquidity: late Friday, a holiday, the hour between the New York close and the Asia open?",
    "Did price sweep an obvious level, where many traders' stops sit, and then reverse?",
    "Does the move show displacement and follow through, or does it stall straight after the headline?",
    "If I take this trade and I am wrong, who profits from my exit?",
]

CCY_PLAYER = {"USD": "US", "EUR": "EU", "JPY": "JP", "GBP": "UK", "CAD": "CA", "AUD": "AU", "CHF": "CH", "CNY": "CN"}
CB_RE = re.compile(r"(rate decision|rate statement|policy rate|cash rate|interest rate|monetary policy|fomc|"
                   r"main refinancing|official bank rate|overnight rate|press conference)", re.I)
DOLLAR_MONEY_RE = re.compile(r"(fomc|federal funds|fed chair|treasury refunding|non-farm|cpi)", re.I)


def load_owner_events(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for i, e in enumerate(raw or []):
        if not e.get("date"):
            raise ValueError(f"event {e.get('id', i)}: date is required")
        t = pd.Timestamp(e["date"])
        if t.tzinfo is not None:
            date = trade_dates([t])[0]
        else:
            date = t.normalize()
        rows.append({"id": str(e.get("id", f"EV-{i}")), "date": date, "time": str(e.get("date")),
                     "headline": e.get("headline", ""), "type": e.get("type", "other"), "tier": int(e.get("tier", 3)),
                     "confirmed": bool(e.get("confirmed", False)) or int(e.get("tier", 3)) == 1,
                     "player": e.get("player"), "lever": e.get("lever"), "currency": e.get("currency"),
                     "plan_effects": dict(e.get("plan_effects") or {}), "exposures": dict(e.get("exposures") or {}),
                     "verdict": (e.get("qualifying") or {}).get("verdict", "unanswered"),
                     "answers": dict((e.get("qualifying") or {}).get("answers") or {}),
                     "source": e.get("source", ""), "evidence": e.get("evidence", "unverified"),
                     "half_life_days": e.get("half_life_days"), "auto": False,
                     "published": str(e.get("published", e.get("date"))), "seen": str(e.get("seen", ""))})
    return pd.DataFrame(rows, columns=EVENT_COLS)


EVENT_COLS = ["id", "date", "time", "headline", "type", "tier", "confirmed", "player", "lever", "currency",
              "plan_effects", "exposures", "verdict", "answers", "source", "evidence", "half_life_days", "auto",
              "published", "seen"]


def load_calendar_events(archive: pd.DataFrame, auto_confirm: bool) -> pd.DataFrame:
    """Red-folder releases as scheduled events. [IMPL] auto_confirm treats the release itself as a tier 1
    fact (it happened on the official calendar); its importance is judged only from our measured shock."""
    if archive is None or archive.empty:
        return pd.DataFrame(columns=EVENT_COLS)
    rows = []
    for _, e in archive.iterrows():
        t = pd.Timestamp(e["event_time"])
        cb = bool(CB_RE.search(str(e["title"])))
        ccy = str(e["currency"])
        rows.append({"id": f"FF-{t.strftime('%Y%m%dT%H%M')}-{ccy}-{re.sub(r'[^A-Za-z0-9]+', '', str(e['title']))[:24]}",
                     "date": trade_dates([t])[0], "time": t.isoformat(), "headline": f"{ccy} {e['title']}",
                     "type": "central_bank" if cb else "data", "tier": 1 if auto_confirm else 3,
                     "confirmed": bool(auto_confirm), "player": CCY_PLAYER.get(ccy), "lever": "MON", "currency": ccy,
                     "plan_effects": {}, "exposures": {}, "verdict": "aligned" if auto_confirm else "unanswered",
                     "answers": {}, "source": "Forex Factory calendar (alert); official release",
                     "evidence": "confirmed fact" if auto_confirm else "unverified", "half_life_days": None, "auto": True,
                     "published": str(e.get("published_at", "")), "seen": str(e.get("published_at", ""))})
    return pd.DataFrame(rows, columns=EVENT_COLS)


# =============================================================================
# Event study
# =============================================================================

def event_study(r: pd.Series, F: pd.DataFrame, day0: pd.Timestamp, ecfg: dict) -> Optional[dict]:
    """Market model on the drivers over a window that ends before the event; AR = r - r_hat; CAR windows."""
    est_lo, est_hi = int(ecfg.get("estimation_start", -250)), int(ecfg.get("estimation_end", -20))
    idx = r.index
    if day0 not in idx:
        later = idx[idx >= day0]
        if later.empty:
            return None
        day0 = later[0]
    i0 = idx.get_loc(day0)
    if i0 + est_lo < 0:
        return None
    est = slice(i0 + est_lo, i0 + est_hi)
    y = r.iloc[est]
    X = F.reindex(idx).iloc[est] if F is not None and not F.empty else pd.DataFrame(index=y.index)
    j = pd.concat([y.rename("_y"), X], axis=1).dropna()
    if len(j) < 60:
        return None
    Xm = np.column_stack([np.ones(len(j))] + [j[c].values for c in X.columns])
    beta, *_ = np.linalg.lstsq(Xm, j["_y"].values, rcond=None)
    resid = j["_y"].values - Xm @ beta
    sd = float(np.std(resid, ddof=Xm.shape[1]))
    if sd <= 0:
        return None
    lo, hi = max(0, i0 - 5), min(len(idx) - 1, i0 + 20)
    win = idx[lo:hi + 1]
    Fw = F.reindex(win) if F is not None and not F.empty else pd.DataFrame(index=win)
    pred = beta[0] + sum(beta[k + 1] * Fw[c].fillna(0.0).values for k, c in enumerate(Fw.columns)) if len(Fw.columns) else beta[0]
    ar = pd.Series(r.reindex(win).values - pred, index=win)
    rel = np.arange(lo - i0, hi - i0 + 1)
    ar.index = rel

    def car(a, b):
        sel = ar[(ar.index >= a) & (ar.index <= b)]
        return float(sel.sum()) if len(sel) == (b - a + 1) and sel.notna().all() else None

    c01, c020, cpre = car(0, 1), car(0, 20), car(-5, -1)
    if c01 is None:
        c00 = car(0, 0)
        if c00 is None:
            return None
        shock = c00 / sd
        c01 = c00
    else:
        shock = c01 / (sd * math.sqrt(2))
    tiny = abs(c01) < 0.25 * sd
    return {"shock": round(float(shock), 2), "car01": c01, "sd": sd,
            "persistence": None if (c020 is None or tiny) else round(c020 / c01, 2),
            "pre_drift": None if (cpre is None or tiny) else round(cpre / c01, 2),
            "complete": c020 is not None}


def cosine(f: np.ndarray, g: np.ndarray) -> Optional[float]:
    m = np.isfinite(f) & np.isfinite(g)
    if m.sum() < 3:
        return None
    a, b = f[m], g[m]
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return None
    return float(a @ b / (na * nb))


def fingerprint(shocks: dict[str, Optional[float]], basket: list[str], templates: dict[str, dict]) -> dict:
    f = np.array([np.nan if shocks.get(s) is None else shocks[s] for s in basket], dtype=float)
    sims = {}
    for name, t in templates.items():
        g = np.array([float(t.get(s, 0.0)) for s in basket])
        sims[name] = cosine(f, g)
    best = max(((k, v) for k, v in sims.items() if v is not None), key=lambda kv: kv[1], default=(None, None))
    return {"vector": {s: (None if np.isnan(v) else round(float(v), 2)) for s, v in zip(basket, f)},
            "similarity": {k: (None if v is None else round(v, 3)) for k, v in sims.items()},
            "best": best[0], "best_sim": None if best[1] is None else round(best[1], 3)}


def persistence_half_life(p: Optional[float], default: float) -> float:
    """Half life (trading days) from persistence CAR(0,20)/CAR(0,1): the share left after 20 days."""
    if p is None or not np.isfinite(p):
        return default
    left = min(max(p, 0.05), 1.0)
    if left >= 0.999:
        return 60.0
    return float(min(60.0, 20 * math.log(2) / -math.log(left)))


# =============================================================================
# Needle test, event weight, pressure
# =============================================================================

def needle_test(ev: dict, measured: dict, ledger, goal_cred: dict, alignment_flip: bool, regime_change: bool,
                lever_top_quartile: float, ncfg: dict) -> dict:
    a_reasons, b_reasons = [], []
    for gid in (ev.get("plan_effects") or {}):
        g = ledger.goals.get(gid)
        if g and (g.credibility or 0) > float(ncfg.get("plan_credibility_min", 0.5)) and ledger.player_weight(g.player) > 0:
            a_reasons.append(f"plan goal {gid} (credibility {g.credibility:.2f})")
    from .players import scaled_leverage
    lev, lev_src = scaled_leverage(ledger, ev.get("lever"), ev.get("player"))
    if lev is not None and lev >= lever_top_quartile:
        a_reasons.append(f"lever {ev.get('lever')} in top quarter ({lev:.2f}, {lev_src})")
    text = f"{ev.get('headline', '')}"
    if (ev.get("player") == "US" and ev.get("lever") == "MON") or DOLLAR_MONEY_RE.search(text):
        a_reasons.append("price of dollar money")
    if ev.get("type") == "central_bank":
        a_reasons.append("central bank action")
    if regime_change:
        a_reasons.append("regime change")
    shock_max = measured.get("shock_max")
    if shock_max is not None and abs(shock_max) >= float(ncfg.get("shock_min", 2.0)):
        b_reasons.append(f"shock {shock_max:+.1f} on {measured.get('shock_max_on')}")
    if (measured.get("best_sim") or 0) >= float(ncfg.get("fingerprint_min", 0.7)):
        b_reasons.append(f"fingerprint {measured.get('best')} ({measured.get('best_sim'):.2f})")
    if alignment_flip:
        b_reasons.append("plan alignment score changed sign")
    return {"part_a": a_reasons, "part_b": b_reasons, "pass": bool(a_reasons and b_reasons),
            "leverage": lev, "leverage_source": lev_src}


def event_weight(ev: dict, measured: dict, ledger, needle: dict) -> dict:
    """E = w_p * Cred_g * L~ * M. Cred = 1 for central bank actions. Unknown inputs -> not computed."""
    wp = ledger.player_weight(ev.get("player")) if ev.get("player") else None
    if ev.get("type") == "central_bank":
        cred, cred_src = 1.0, "central bank action"
    else:
        creds = [ledger.goals[g].credibility for g in (ev.get("plan_effects") or {}) if g in ledger.goals
                 and ledger.goals[g].credibility is not None]
        cred, cred_src = (max(creds), "plan goal") if creds else (None, "unknown")
    L = needle.get("leverage")
    sm = measured.get("shock_max")
    M = None if sm is None else min(abs(sm) / 3.0, 1.0)
    missing = [n for n, v in (("player weight", wp), ("credibility", cred), ("leverage", L), ("magnitude", M)) if v is None]
    E = None if missing else wp * cred * L * M
    return {"E": None if E is None else round(E, 4), "w_p": wp, "cred": cred, "cred_source": cred_src, "L": L, "M": M,
            "missing": missing}


def counts_for_pressure(ev: dict, needle: dict, weight: dict) -> tuple[bool, str]:
    if not ev.get("confirmed"):
        return False, "not confirmed at a tier 1 source"
    if ev.get("verdict") in ("noise", "bait"):
        return False, f"verdict {ev.get('verdict')}"
    if not evidence_ok(ev.get("evidence")):
        return False, f"evidence {ev.get('evidence')}"
    if not needle.get("pass"):
        return False, "fails the needle test"
    if weight.get("E") is None:
        return False, "event weight unknown: " + ", ".join(weight.get("missing", []))
    return True, ""


def pressure_series(contribs: list[dict], dates: pd.DatetimeIndex) -> pd.Series:
    """Pi_t = sum_e E_e x_e 2^{-(t - t_e)/h_e}, t in business days, only from an event's day 0 onward."""
    out = np.zeros(len(dates))
    d64 = dates.values.astype("datetime64[D]")
    for c in contribs:
        t0 = np.datetime64(pd.Timestamp(c["date"]).date())
        age = np.busday_count(t0, d64).astype(float)
        live = age >= 0
        out[live] += c["E"] * c["x"] * np.power(2.0, -age[live] / c["h"])
    return pd.Series(out, index=dates)
