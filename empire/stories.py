"""Story layer: likelihoods, the four gates, grades, setup types, scanner alignment and the exposure check."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Optional

import pandas as pd

DIR = {"up": +1, "down": -1, "range": 0}


def likelihood(pi: dict, W: dict, pressure: Optional[float], lam: float, T: float) -> dict:
    """P(k) = pi_k exp(W_k / T) / sum_j pi_j exp(W_j / T), with W_k += lambda * Pi * dir_k."""
    p = pressure or 0.0
    Wt = {k: W.get(k, 0.0) + lam * p * DIR[k] for k in DIR}
    m = max(Wt.values())
    z = {k: pi[k] * math.exp((Wt[k] - m) / T) for k in DIR}
    s = sum(z.values())
    return {"P": {k: z[k] / s for k in DIR}, "W_total": Wt}


def ev_entry(p_dir: float, p_hold: float, reward: float, risk: float, cost: float) -> float:
    """EV_entry = P_dir P_hold Reward - (1 - P_dir P_hold) Risk - Cost, in units of risk."""
    q = p_dir * p_hold
    return q * reward - (1 - q) * risk - cost


def gate_results(card: dict, gcfg: dict) -> dict:
    fav = card.get("favored")
    d = DIR.get(fav or "range", 0)
    top = card.get("top_incentive")
    inc_ok = bool(top and top.get("used") and d != 0 and DIR.get(top["gains_from"], 0) == d)
    inc_why = (f"{top['player']} ({top['id']}) gains from {top['gains_from']}" if top else "no usable incentive entry")
    ch = card.get("character", {})
    band = ch.get("band")
    char_ok = (band in ("core", "stretched") and card.get("at_zone")) or (band == "out of character" and bool(ch.get("cause")))
    char_why = f"{band}" + (f", cause: {ch.get('cause')}" if ch.get("cause") else "") + (", at a zone" if card.get("at_zone") else "")
    loc = card.get("location", {})
    loc_ok = bool(loc.get("in_proven_zone") or loc.get("leaving_into_vacuum"))
    win = card.get("window", {})
    win_ok = bool(win.get("open")) and not win.get("event_inside_stop")
    out = {"incentive": {"pass": inc_ok, "why": inc_why},
           "character": {"pass": bool(char_ok), "why": char_why},
           "location": {"pass": loc_ok, "why": loc.get("why", "")},
           "window": {"pass": win_ok, "why": win.get("why", "")}}
    n = sum(v["pass"] for v in out.values())
    out["_count"] = n
    out["_status"] = "candidate" if n == 4 else "watch" if n == 3 else "none"
    return out


def grade(card: dict, g: dict) -> dict:
    """Layer 5 grading. A needs every rule; B relaxes direction to 0.55-0.60 or is a weakness trade;
    C passes the needle test but misses a rule; otherwise no grade."""
    rules = {
        "pressure top 10%": (card.get("pressure_pct") or 0) >= float(g.get("pressure_pct_min", 90)),
        "direction >= A min": (card.get("p_dir") or 0) >= float(g.get("p_dir_a", 0.60)),
        "EV at entry >= min": (card.get("ev_entry") or -9) >= float(g.get("ev_min", 0.5)),
        "reward >= 2x risk": (card.get("reward_r") or 0) >= float(g.get("reward_min", 2.0)),
        "alignment trade": card.get("trade_type") == "alignment",
        "story and push agree": bool(card.get("story_push_agree")),
    }
    missed = [k for k, v in rules.items() if not v]
    needle = bool(card.get("needle_pass"))
    conflict = card.get("story_technical_conflict", False)
    if not needle or conflict:
        return {"grade": "none", "missed": missed, "action": "log only",
                "why": "fails the needle test" if not needle else "story and technicals conflict"}
    if not missed:
        return {"grade": "A", "missed": [], "action": "full planned risk at the zone", "why": "every rule met"}
    b_ok = set(missed) <= {"direction >= A min", "alignment trade"} and \
        (card.get("p_dir") or 0) >= float(g.get("p_dir_b", 0.55)) and \
        ("alignment trade" not in missed or card.get("trade_type") == "weakness")
    if b_ok:
        return {"grade": "B", "missed": missed, "action": "half planned risk", "why": "A rules met except " + ", ".join(missed)}
    return {"grade": "C", "missed": missed, "action": "monitor only", "why": "misses " + ", ".join(missed)}


def setup_type(card: dict) -> str:
    """The defended line or the forced march."""
    band = card.get("character", {}).get("band")
    z = card.get("zone_now")
    gap = card.get("gap") or {}
    if z and z.get("family") in ("round_major", "round_mid", "market", "pain") and z.get("memory_or_pain") and \
            band in ("core", "stretched") and card.get("incentive_to_hold"):
        return "defended line"
    if band == "out of character" and gap.get("trend") == "widening" and card.get("recent_break"):
        return "forced march"
    return "none"


# =============================================================================
# Scanner adapters (any number of scanners, compared side by side)
# =============================================================================

def _latest(base: Path, pattern: str) -> Optional[Path]:
    files = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_scanner(spec: dict, base: Path, asof: pd.Timestamp) -> dict:
    name = spec.get("name", spec.get("path", "scanner"))
    p = _latest(base, spec["path"])
    out = {"name": name, "file": None, "rows": [], "status": "no output found", "statuses": spec.get("statuses", ["qualified"])}
    if p is None:
        return out
    out["file"] = str(p.relative_to(base)) if p.is_relative_to(base) else str(p)
    age_h = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(p.stat().st_mtime, unit="s", tz="UTC")).total_seconds() / 3600
    max_age = float(spec.get("max_age_hours", 36))
    smap = dict(spec.get("symbol_map") or {})
    rows: list[dict] = []
    try:
        ad = spec.get("adapter", "scanner_json")
        if ad == "scanner_json":
            payload = json.loads(p.read_text(encoding="utf-8"))
            for r in payload.get("rows", []):
                rows.append({"symbol": r.get("symbol"), "direction": r.get("direction"), "status": r.get("section"),
                             "detail": r.get("setup") or "", "zone": r.get("zone_level")})
        elif ad == "generic_csv":
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    rows.append({"symbol": r.get("symbol"), "direction": r.get("direction"), "status": r.get("status"),
                                 "detail": r.get("detail", ""), "zone": r.get("zone")})
        elif ad == "generic_json":
            payload = json.loads(p.read_text(encoding="utf-8"))
            items = payload.get("rows", payload) if isinstance(payload, dict) else payload
            for r in items:
                rows.append({"symbol": r.get("symbol"), "direction": r.get("direction"), "status": r.get("status"),
                             "detail": r.get("detail", ""), "zone": r.get("zone")})
        else:
            out["status"] = f"unknown adapter {ad}"
            return out
    except Exception as exc:  # noqa: BLE001
        out["status"] = f"unreadable: {exc}"
        return out
    keep = {s.lower() for s in out["statuses"]}
    for r in rows:
        r["symbol"] = smap.get(r["symbol"], r["symbol"])
        d = str(r.get("direction", "")).lower()
        r["dir"] = +1 if d in ("long", "buy", "up", "1", "+1") else -1 if d in ("short", "sell", "down", "-1") else 0
    out["rows"] = [r for r in rows if str(r.get("status", "")).lower() in keep and r["dir"] != 0]
    out["status"] = "ok" if age_h <= max_age else f"stale ({age_h:.0f} h old, ignored)"
    if age_h > max_age:
        out["rows"] = []
    out["age_hours"] = round(age_h, 1)
    return out


ALIGN_TEXT = {"aligned": "Aligned. Full planned risk", "watch": "Watch. Story is early or wrong; wait for location",
              "tactical": "Tactical. Reduced risk, technical exit rules only", "no trade": "No trade",
              "conflict": "Conflict. No trade, or minimum size logged as a test"}


def alignment_cell(story_dir: int, scanner: dict, sym: str) -> dict:
    rows = [r for r in scanner.get("rows", []) if r["symbol"] == sym]
    dirs = {r["dir"] for r in rows}
    if not rows:
        cell = "watch" if story_dir != 0 else "no trade"
        return {"cell": cell, "tech_dir": 0, "detail": ""}
    if len(dirs) > 1:
        return {"cell": "conflict", "tech_dir": 0, "detail": "scanner shows both directions"}
    td = dirs.pop()
    det = "; ".join(f"{'long' if r['dir'] > 0 else 'short'} {r['status']} {r.get('detail') or ''}".strip() for r in rows)
    if story_dir == td:
        cell = "aligned"
    elif story_dir == 0:
        cell = "tactical"
    else:
        cell = "conflict"
    return {"cell": cell, "tech_dir": td, "detail": det}


def exposure_check(cards: list[dict], cap: float, single_player_cap: float) -> dict:
    """NetUSD = sum_i d_i Risk_i over candidates. d from the measured beta to the dollar index when the
    instrument has no dollar leg."""
    net = 0.0
    by_player: dict[str, float] = {}
    items = []
    for c in cards:
        if c.get("grade") not in ("A", "B"):
            continue
        risk = 1.0 if c["grade"] == "A" else 0.5
        pos = DIR.get(c.get("favored") or "range", 0)
        d = c.get("usd_sign")
        if d is None or pos == 0:
            continue
        contrib = d * pos * risk
        net += contrib
        p = (c.get("top_incentive") or {}).get("player")
        if p:
            by_player[p] = by_player.get(p, 0.0) + risk
        items.append({"symbol": c["symbol"], "usd": contrib})
    flags = []
    if abs(net) > cap:
        flags.append(f"net dollar exposure {net:+.1f} risk units exceeds cap {cap}")
    for p, v in by_player.items():
        if v > single_player_cap:
            flags.append(f"{v:.1f} risk units ride on {p}'s move (cap {single_player_cap})")
    return {"net_usd": round(net, 2), "items": items, "by_player": by_player, "flags": flags}
