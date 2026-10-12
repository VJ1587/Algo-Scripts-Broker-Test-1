"""Trade plan and risk-to-reward grade for each potential setup (cfg-0.14.0).

Spec: Development/planning/specs/trade-plan-rr-grade_spec.md. Pure functions: the scanner passes in prices
and candidate levels, this module picks the entry range, stop and TP1 and grades R:R. No orders, no sizes.
"""
from __future__ import annotations

import math
from typing import Optional

LONG, SHORT = "long", "short"

# [OWNER cfg-0.14.0] R:R measured on TP1 from the worst entry: 3:1+ = A, 2:1+ = B, 1:1+ = C, below 1:1 fails
RR_GRADES = ((3.0, "A"), (2.0, "B"), (1.0, "C"))
FAIL = "below 1:1"


def width_key(asset: str, quote: Optional[str]) -> str:
    """Config key for the entry width and stop buffer: fx, jpy, gold, spx or oil."""
    if asset == "fx":
        return "jpy" if quote == "JPY" else "fx"
    return asset


def rr_grade(rr: Optional[float]) -> str:
    if rr is None:
        return ""
    for floor, grade in RR_GRADES:
        if rr >= floor - 1e-9:
            return grade
    return FAIL


def build(direction: str, ref_price: float, width: float, buffer: float,
          stops: list[tuple[str, Optional[float]]], targets: list[tuple[str, Optional[float]]],
          tp_rule: str = "nearest") -> dict:
    """Entry range anchored at ref_price on the favourable side; worst entry = ref_price.

    stops: (source, level) in priority order; the first level beyond the whole entry range wins, then the
    buffer is added beyond it. targets: (source, level) candidates beyond the worst entry; tp_rule "nearest"
    takes the closest one (reversal: first reaction level), "first" takes the first in the given priority
    order (trend pullback: the trade's own objective). 'note' says why when a plan is incomplete.
    """
    if tp_rule not in ("nearest", "first"):
        raise ValueError(f"tp_rule must be 'nearest' or 'first', got {tp_rule!r}")
    sgn = 1 if direction == LONG else -1
    worst = ref_price
    best = ref_price - sgn * width
    entry_low, entry_high = min(worst, best), max(worst, best)
    plan = {"entry_low": entry_low, "entry_high": entry_high, "worst_entry": worst, "stop": None,
            "stop_source": "", "tp1": None, "tp1_source": "", "risk": None, "reward": None, "rr": None,
            "rr_grade": "", "note": ""}
    # stop must sit beyond the far edge of the range (long: below entry_low), else a fill there is already out
    for src, lvl in stops:
        if lvl is not None and sgn * (best - lvl) > 0:
            plan["stop"], plan["stop_source"] = lvl - sgn * buffer, src
            break
    beyond = [(src, lvl) for src, lvl in targets if lvl is not None and sgn * (lvl - worst) > 0]
    if beyond:
        src, lvl = beyond[0] if tp_rule == "first" else min(beyond, key=lambda t: abs(t[1] - worst))
        plan["tp1"], plan["tp1_source"] = lvl, src
    if plan["stop"] is None:
        plan["note"] = "no structural stop beyond the entry range"
        return plan
    if plan["tp1"] is None:
        plan["note"] = "no target level beyond the entry"
        return plan
    plan["risk"] = abs(worst - plan["stop"])
    plan["reward"] = abs(plan["tp1"] - worst)
    plan["rr"] = round(plan["reward"] / plan["risk"], 2)
    plan["rr_grade"] = rr_grade(plan["rr"])
    if plan["rr_grade"] == FAIL:
        plan["note"] = "TP1 pays less than the risk: not a trade"
    return plan


TREND_PULLBACK, KEY_LEVEL_REVERSAL = "Trend pullback", "Key level reversal"


def scanner_trade_type(direction: str, c1: bool, daily_bias: str) -> str:
    """[OWNER cfg-0.14.1] With the trend (C1, or the Daily bias points the trade's way): a pullback whose
    objective is the impulse extension. Otherwise: a reversal at a key level, aiming at the first reaction."""
    return TREND_PULLBACK if c1 or daily_bias == direction else KEY_LEVEL_REVERSAL


def next_grid_level(price: float, direction: str, grid_mid: float) -> float:
    """Next mid or major level strictly beyond price in the trade direction."""
    k = price / grid_mid
    n = math.floor(k + 1e-9) + 1 if direction == LONG else math.ceil(k - 1e-9) - 1
    return round(n * grid_mid, 10)


def last_swing(seq, kind: str, beyond: float, direction_sign: int) -> Optional[float]:
    """Most recent confirmed pivot of this kind ('H' or 'L') whose price is beyond `beyond`.

    direction_sign +1 means price must be above `beyond`, -1 below. seq is oldest first."""
    for p in reversed(seq):
        if p.kind == kind and direction_sign * (p.price - beyond) > 0:
            return p.price
    return None
