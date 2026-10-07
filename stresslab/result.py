"""PASS / WARN / FAIL result type shared by every check.

FAIL means the result cannot be trusted. WARN means a human should look.
"""
from __future__ import annotations

from dataclasses import dataclass

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}


@dataclass(frozen=True)
class Result:
    status: str
    reason: str

    def __post_init__(self):
        if self.status not in _RANK:
            raise ValueError(f"status must be PASS, WARN or FAIL, got {self.status!r}")


def worst(statuses) -> str:
    """Most severe status in an iterable (PASS if empty)."""
    out = PASS
    for s in statuses:
        if _RANK[s] > _RANK[out]:
            out = s
    return out
