"""Config, ledger and instrument definitions."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml")

EVIDENCE_LABELS = ("confirmed fact", "derived intelligence", "working assumption", "unverified", "unknown")
# [Doc: data model] scores computed from these inputs are flagged and excluded from likelihoods by default
EXCLUDED_EVIDENCE = ("unverified", "unknown")
MAJORS = ("USD", "EUR", "JPY", "GBP", "CHF", "AUD", "NZD", "CAD")


@dataclass
class Inst:
    symbol: str
    kind: str                       # fx | commodity | index | yield | vol | spread | solo
    sources: list[dict] = field(default_factory=list)
    base: Optional[str] = None
    quote: Optional[str] = None
    grid: Optional[str] = None
    cot: Optional[str] = None
    profile: bool = True            # gets a personality card (drivers such as VIX do not)
    commodity: Optional[str] = None  # driver symbol for the resource beta
    label: str = ""

    @property
    def is_fx(self) -> bool:
        return self.kind == "fx"

    @property
    def additive(self) -> bool:
        """Yields, volatility and spreads move in points, so returns are differences, not logs."""
        return self.kind in ("yield", "vol", "spread")

    @property
    def currencies(self) -> list[str]:
        if self.is_fx:
            return [self.base, self.quote]
        if self.kind == "solo":
            return [self.base]
        return ["USD"]


def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def load_config(path: Path) -> dict:
    cfg = load_yaml(path)
    for key in ("config_version", "paths", "instruments", "grids", "traits", "levels", "likelihood", "grading"):
        if key not in cfg:
            raise ValueError(f"personality config missing '{key}'")
    return cfg


def build_instruments(cfg: dict) -> dict[str, Inst]:
    out: dict[str, Inst] = {}
    for sym, spec in (cfg.get("instruments") or {}).items():
        out[sym] = _inst(sym, spec, profile=True)
    for sym, spec in (cfg.get("drivers") or {}).items():
        if sym in out:
            raise ValueError(f"driver {sym} duplicates an instrument")
        out[sym] = _inst(sym, spec, profile=False)
    for sym, inst in out.items():
        if inst.grid and inst.grid not in cfg["grids"]:
            raise ValueError(f"{sym}: unknown grid '{inst.grid}'")
    return out


def _inst(sym: str, spec: dict, profile: bool) -> Inst:
    spec = dict(spec or {})
    kind = spec.get("kind", "fx")
    base, quote = spec.get("base"), spec.get("quote")
    if kind == "fx" and not (base and quote):
        base, quote = sym[:3], sym[3:]
    return Inst(symbol=sym, kind=kind, sources=list(spec.get("sources") or []), base=base, quote=quote,
                grid=spec.get("grid"), cot=spec.get("cot"), profile=bool(spec.get("profile", profile)),
                commodity=spec.get("commodity"), label=spec.get("label", sym))


def usd_sign(inst: Inst) -> Optional[int]:
    """+1 if a long position gains when the dollar rises (USDJPY), -1 if it loses (EURUSD), None if unknown."""
    if inst.is_fx:
        if inst.base == "USD":
            return +1
        if inst.quote == "USD":
            return -1
        return 0
    return None


def evidence_ok(label: Optional[str]) -> bool:
    return (label or "unknown") not in EXCLUDED_EVIDENCE


def check_evidence(label: Optional[str], where: str) -> str:
    lab = (label or "unknown").strip().lower()
    if lab not in EVIDENCE_LABELS:
        raise ValueError(f"{where}: evidence label '{label}' is not one of {EVIDENCE_LABELS}")
    return lab
