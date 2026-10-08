"""Player ledger: incentive scores, leverage, plans and credibility, alignment scores, hypotheses.

The ledger (personality_ledger.yaml) is owner-maintained judgment plus primary sources. Every score
carries an evidence label and a source. Inputs labelled unverified or unknown are shown but excluded
from likelihoods, and a missing input is recorded as unknown, never estimated to complete a card.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import check_evidence, evidence_ok, load_yaml

OUTCOME_SIGN = {"up": +1, "down": -1, "range": 0}


@dataclass
class Incentive:
    id: str
    player: str
    instrument: str
    gains_from: str            # up | down | range
    B: Optional[float]
    K: Optional[float]
    U: Optional[float]
    C: Optional[float]
    evidence: str
    source: str
    as_of: str
    move: str = ""
    note: str = ""
    goal: Optional[str] = None  # plan goal it serves (alignment trade anchor)

    @property
    def score(self) -> Optional[float]:
        """I = B K U / C on 1..5 inputs: an ordinal ranking tool, not a probability."""
        if None in (self.B, self.K, self.U, self.C) or not self.C:
            return None
        return float(self.B * self.K * self.U / self.C)

    @property
    def usable(self) -> bool:
        return self.score is not None and evidence_ok(self.evidence)

    def why_excluded(self) -> str:
        if self.score is None:
            return "an input is unknown"
        if not evidence_ok(self.evidence):
            return f"evidence '{self.evidence}'"
        return ""


@dataclass
class Goal:
    id: str
    plan: str
    player: str
    goal: str
    target: str = ""
    date: str = ""
    levers: list = field(default_factory=list)
    markets: dict = field(default_factory=dict)   # instrument -> +1 / -1 (direction the goal pushes it)
    credibility: Optional[float] = None


@dataclass
class Ledger:
    version: str
    players: dict
    incentives: list
    levers: list
    lever_defaults: dict
    pain_zones: list
    plans: list
    goals: dict
    hypotheses: list
    raw: dict

    def player_weight(self, p: str) -> float:
        return float((self.players.get(p) or {}).get("weight", 0.0))

    @property
    def max_weight(self) -> float:
        return max([self.player_weight(p) for p in self.players] or [1.0]) or 1.0

    def incentives_for(self, sym: str) -> list[Incentive]:
        return [i for i in self.incentives if i.instrument == sym]

    def pain_for(self, sym: str) -> list[dict]:
        return [p for p in self.pain_zones if p.get("instrument") == sym]


def _num(x) -> Optional[float]:
    if x is None or (isinstance(x, str) and x.strip().lower() in ("", "unknown", "null", "none")):
        return None
    return float(x)


def parse_ledger(raw: dict) -> Ledger:
    players = raw.get("players") or {}
    for pid, p in players.items():
        check_evidence(p.get("weight_evidence", "working assumption"), f"player {pid} weight")
    incs = []
    for i, it in enumerate(raw.get("incentives") or []):
        lab = check_evidence(it.get("evidence"), f"incentive {it.get('id', i)}")
        g = str(it.get("gains_from", "")).lower()
        if g not in OUTCOME_SIGN:
            raise ValueError(f"incentive {it.get('id', i)}: gains_from must be up, down or range")
        if it.get("player") not in players:
            raise ValueError(f"incentive {it.get('id', i)}: unknown player {it.get('player')}")
        for k in ("B", "K", "U", "C"):
            v = _num(it.get(k))
            if v is not None and not 1 <= v <= 5:
                raise ValueError(f"incentive {it.get('id', i)}: {k}={v} outside 1..5")
        incs.append(Incentive(str(it.get("id", f"INC-{i}")), it["player"], it["instrument"], g, _num(it.get("B")),
                              _num(it.get("K")), _num(it.get("U")), _num(it.get("C")), lab, str(it.get("source", "")),
                              str(it.get("as_of", "")), str(it.get("move", "")), str(it.get("note", "")), it.get("goal")))
    plans = raw.get("plans") or []
    goals: dict[str, Goal] = {}
    for pl in plans:
        fr, dr = _num(pl.get("funding_ratio")), _num(pl.get("delivery_ratio"))
        cred = None if fr is None or dr is None else max(0.0, min(1.0, fr)) * max(0.0, min(1.0, dr))
        if pl.get("credibility_override") is not None:
            cred = _num(pl["credibility_override"])
        pl["_credibility"] = cred
        for g in pl.get("goals") or []:
            gcred = cred
            gf, gd = _num(g.get("funding_ratio")), _num(g.get("delivery_ratio"))
            if gf is not None or gd is not None:   # goal-level ratios override the plan's
                gf = fr if gf is None else gf
                gd = dr if gd is None else gd
                gcred = None if gf is None or gd is None else max(0.0, min(1.0, gf)) * max(0.0, min(1.0, gd))
            goals[g["id"]] = Goal(g["id"], pl["id"], pl["player"], g.get("goal", ""), str(g.get("target", "")),
                                  str(g.get("date", "")), list(g.get("levers") or []), dict(g.get("markets") or {}), gcred)
    for pz in raw.get("pain_zones") or []:
        pz["evidence"] = check_evidence(pz.get("evidence"), f"pain zone {pz.get('label')}")
    for lv in raw.get("levers") or []:
        lv["evidence"] = check_evidence(lv.get("evidence"), f"lever {lv.get('id')}")
    return Ledger(str(raw.get("ledger_version", "unversioned")), players, incs, list(raw.get("levers") or []),
                  dict(raw.get("lever_defaults") or {}), list(raw.get("pain_zones") or []), plans, goals,
                  list(raw.get("hypotheses") or []), raw)


def leverage_score(lv: dict) -> Optional[float]:
    """L = D H tau C, with tau (years to substitute) capped at 5. Unknown input -> not computed."""
    D, H, tau, C = (_num(lv.get(k)) for k in ("dependence", "supply_share", "substitution_years", "credibility"))
    if None in (D, H, tau, C):
        return None
    return float(D * H * min(tau, 5.0) * C)


def scaled_leverage(ledger: Ledger, family: Optional[str], holder: Optional[str]) -> tuple[Optional[float], str]:
    """Scaled 0..1 leverage for an event's lever: a catalogued lever first, then the family default."""
    best = None
    for lv in ledger.levers:
        if lv.get("family") == family and (holder is None or lv.get("holder") == holder):
            s = leverage_score(lv)
            if s is not None and evidence_ok(lv.get("evidence")):
                best = max(best or 0.0, s / 5.0)
    if best is not None:
        return min(best, 1.0), "lever catalog"
    d = ledger.lever_defaults.get(family) if family else None
    if isinstance(d, dict) and d.get("value") is not None and evidence_ok(d.get("evidence")):
        return float(d["value"]), f"family default ({d.get('evidence')})"
    return None, "unknown"


def incentive_weights(ledger: Ledger, sym: str) -> dict:
    """W_k = sum_a d_{a,k} I_{a,k}: gainers add, losers subtract. Each I is scaled to 0..1 (I / 125) and
    multiplied by the player's weight relative to the heaviest player."""
    W = {"up": 0.0, "down": 0.0, "range": 0.0}
    rows = []
    for inc in ledger.incentives_for(sym):
        s = inc.score
        w = ledger.player_weight(inc.player) / ledger.max_weight
        c = None if s is None else s / 125.0 * w
        row = {"id": inc.id, "player": inc.player, "gains_from": inc.gains_from, "I": s, "contribution": c,
               "evidence": inc.evidence, "source": inc.source, "as_of": inc.as_of, "move": inc.move,
               "used": inc.usable, "excluded": inc.why_excluded(), "goal": inc.goal}
        rows.append(row)
        if not inc.usable:
            continue
        for k in W:
            if k == inc.gains_from:
                W[k] += c
            elif inc.gains_from in ("up", "down") and k in ("up", "down"):
                W[k] -= c          # the player loses if price goes the other way
    rows.sort(key=lambda r: -(r["contribution"] or 0))
    return {"W": W, "rows": rows}


def alignment_scores(ledger: Ledger, events: pd.DataFrame, asof: pd.Timestamp, days: int = 90) -> dict:
    """A_g = Cred_g Power_g sum_{e in 90 days} s_{e,g},  s in {-1, 0, +1}."""
    out = {}
    recent = events[(events["date"] > asof - pd.Timedelta(days=days)) & (events["date"] <= asof)] if not events.empty else events
    for gid, g in ledger.goals.items():
        s = 0
        n = 0
        if not recent.empty:
            for eff in recent["plan_effects"]:
                if isinstance(eff, dict) and gid in eff:
                    s += int(eff[gid])
                    n += 1
        power = ledger.player_weight(g.player)
        A = None if g.credibility is None else g.credibility * power * s
        out[gid] = {"goal": g.goal, "player": g.player, "credibility": g.credibility, "events": n, "sum": s,
                    "A": None if A is None else round(A, 4)}
    return out


def merge_scorecard(raw: dict, path: Path) -> dict:
    """Fold ledger/cycle_scorecard.yaml into the raw ledger: its values replace each player's cycle_scorecard
    and its indicator definitions ride along, so the ledger snapshot and change log cover the scorecard too.
    Players are copied one by one because the ledger seeds them from a single shared YAML anchor."""
    if not path.exists():
        return raw
    sc = load_yaml(path) or {}
    raw = copy.deepcopy(raw)
    for pid, vals in (sc.get("values") or {}).items():
        if pid in (raw.get("players") or {}):
            raw["players"][pid]["cycle_scorecard"] = copy.deepcopy(vals)
    raw["cycle_indicators"] = sc.get("indicators") or {}
    raw["cycle_scorecard_version"] = sc.get("scorecard_version")
    return raw


def cycle_staleness(ledger: Ledger, asof: pd.Timestamp, max_days: int = 100) -> list[dict]:
    """Inputs that are blank, or older than their indicator's refresh window (max_age_days in the scorecard
    file; `max_days` when an indicator does not set one). Inputs marked applicable: false are skipped."""
    defs = ledger.raw.get("cycle_indicators") or {}
    out = []
    for pid, p in ledger.players.items():
        for ind, v in (p.get("cycle_scorecard") or {}).items():
            v = v or {}
            if v.get("applicable") is False:
                continue
            limit = int((defs.get(ind) or {}).get("max_age_days", max_days))
            ao = v.get("as_of")
            age = None
            if ao:
                age = (asof - pd.Timestamp(ao)).days
            if v.get("value") is None or age is None or age > limit:
                out.append({"player": pid, "indicator": ind, "value": v.get("value"), "as_of": ao,
                            "status": "unknown" if v.get("value") is None else f"stale ({age} days)"})
    return out


# =============================================================================
# Hypotheses: written behaviors that each monthly run validates or invalidates
# =============================================================================

def check_expect(value: Optional[float], expect: str) -> Optional[bool]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    e = expect.replace(" ", "")
    if e.startswith(">="):
        return value >= float(e[2:])
    if e.startswith("<="):
        return value <= float(e[2:])
    if e.startswith(">"):
        return value > float(e[1:])
    if e.startswith("<"):
        return value < float(e[1:])
    if e.startswith("between"):
        a, b = e[len("between"):].split(",")
        return float(a) <= value <= float(b)
    raise ValueError(f"cannot read expectation '{expect}'")


def evaluate_hypothesis(h: dict, now_value: Optional[float], long_value: Optional[float],
                        regime_values: dict[str, Optional[float]]) -> dict:
    """validated: holds now and over the long history; regime-dependent: holds in one but not the other
    (or only in some regimes); invalidated: holds in neither; inconclusive: not measurable yet."""
    exp = h["expect"]
    now_ok, long_ok = check_expect(now_value, exp), check_expect(long_value, exp)
    reg = {k: check_expect(v, exp) for k, v in regime_values.items() if v is not None}
    if now_ok is None and long_ok is None:
        status = "inconclusive"
    elif now_ok and long_ok:
        status = "validated"
    elif not now_ok and not long_ok and now_ok is not None and long_ok is not None:
        status = "invalidated"
    else:
        status = "regime-dependent"
    return {"id": h["id"], "text": h.get("text", ""), "subject": h.get("subject"), "metric": h.get("metric"),
            "expect": exp, "now": now_value, "long_run": long_value,
            "regimes": {k: v for k, v in reg.items()}, "status": status, "source": h.get("source", "")}
