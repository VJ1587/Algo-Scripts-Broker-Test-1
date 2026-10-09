"""Immutable records, stable IDs and JSON persistence."""
from dataclasses import dataclass, field, asdict, is_dataclass
from enum import Enum
from decimal import Decimal
from datetime import datetime
from types import MappingProxyType
from collections.abc import Mapping
import hashlib
import json
import math

class AssetClass(str, Enum):
    STOCK = "stock"
    FX = "fx"
    FUTURE = "future"

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"

class Regime(str, Enum):
    DIRECTIONAL = "directional"
    RANGE = "range"
    TRANSITION = "transition"

def freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value

def primitive(value):
    if is_dataclass(value):
        return {k: primitive(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {str(k): primitive(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [primitive(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        return primitive(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value

def dumps(value):
    return json.dumps(primitive(value), sort_keys=True, allow_nan=False, separators=(",", ":"))

def stable_id(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()[:20]

def aware(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp required")
    return value

@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    venue: str
    asset_class: AssetClass
    tick_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    multiplier: Decimal
    pnl_currency: str
    account_currency: str
    timezone: str
    session_calendar_path: str
    price_basis: str
    volume_kind: str
    short_allowed: bool
    supported_orders: tuple
    metadata_as_of: str
    synthetic: bool
    settlement: str
    cost_model: Mapping
    lot_size: Decimal | None = None
    pip_size: Decimal | None = None
    expiry: str | None = None
    margin_per_unit: Decimal | None = None
    eligibility_manifest_path: str | None = None
    quantity_unit: str = "units"
    roll_policy: str | None = None
    executable_prices: bool = True
    quotes_path: str | None = None
    conversion_path: str | None = None
    corporate_action_basis: str | None = None

    def __post_init__(self):
        object.__setattr__(self,'cost_model',freeze(self.cost_model))
        object.__setattr__(self,'supported_orders',tuple(self.supported_orders))

    @property
    def instrument_id(self):
        return f"{self.venue}:{self.symbol}"

@dataclass(frozen=True)
class Bar:
    open_time: datetime
    close_time: datetime
    available_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    session_id: str
    complete: bool

@dataclass(frozen=True)
class InstrumentProfile:
    profile_id: str
    instrument_id: str
    version: str
    cutoff: datetime
    completed_sessions: tuple
    buckets: Mapping
    ready: bool
    input_start: str | None = None
    input_end: str | None = None
    volume_kind: str = "unknown"
    confidence: float = 0
    traits: tuple = ()

    def __post_init__(self):
        object.__setattr__(self,'buckets',freeze(self.buckets))
        object.__setattr__(self,'completed_sessions',tuple(self.completed_sessions))
        object.__setattr__(self,'traits',tuple(self.traits))

@dataclass(frozen=True)
class RegimeState:
    label: str = "transition"
    directional_sign: int = 0
    candidate_label: str = "transition"
    consecutive_count: int = 0
    volatility_flag: str = "unknown"
    liquidity_flag: str = "unknown"
    changed_at: str | None = None
    quality_reasons: tuple = ()

@dataclass(frozen=True)
class PolicySnapshot:
    policy_id: str
    mode: str
    profile_id: str
    regime: str
    effective_atr: float | None
    distances: Mapping
    allowed_sides: tuple
    confirmation_bars: int
    expires_after: int
    hold_timeout: int
    risk_multiplier: float
    ready: bool
    reasons: tuple = ()

    def __post_init__(self):
        object.__setattr__(self,'distances',freeze(self.distances))
        object.__setattr__(self,'allowed_sides',tuple(self.allowed_sides))
        object.__setattr__(self,'reasons',tuple(self.reasons))
        if not 0<=self.risk_multiplier<=1: raise ValueError('Risk escalation forbidden')

@dataclass(frozen=True)
class StrategyState:
    strategy_id: str
    version: str = "1.0"
    last_processed_bar_id: str | None = None
    phase: str = "IDLE"
    candidate: Mapping = field(default_factory=lambda: freeze({}))
    confirmation_count: int = 0
    frozen_policy: PolicySnapshot | None = None
    last_emitted_signal_id: str | None = None
    consumed_setups: tuple = ()

    def __post_init__(self):
        object.__setattr__(self,'candidate',freeze(self.candidate))
        object.__setattr__(self,'consumed_setups',tuple(self.consumed_setups))

    def to_json(self):
        return dumps(self)

    @classmethod
    def from_json(cls, text):
        d = json.loads(text)
        d['candidate'] = freeze(d['candidate'])
        d['consumed_setups'] = tuple(d.get('consumed_setups', ()))
        if d['frozen_policy']:
            p = d['frozen_policy']
            p['distances'] = freeze(p['distances'])
            p['allowed_sides'] = tuple(p['allowed_sides'])
            p['reasons'] = tuple(p['reasons'])
            d['frozen_policy'] = PolicySnapshot(**p)
        return cls(**d)

@dataclass(frozen=True)
class SignalProposal:
    signal_id: str
    strategy_id: str
    version: str
    instrument_id: str
    decision_time: datetime
    source_available_at: datetime
    side: int
    order_type: OrderType
    entry: Decimal
    structural_invalidation: Decimal
    stop: Decimal
    targets: tuple
    fractions: tuple
    expiry: datetime
    max_hold: int
    policy: PolicySnapshot
    risk_fraction: float
    evidence: Mapping
    reasons: tuple = ()

    def __post_init__(self):
        for t in (self.decision_time,self.source_available_at,self.expiry): aware(t)
        if self.source_available_at>self.decision_time or self.expiry<=self.decision_time:
            raise ValueError('Invalid signal availability/expiry')
        if self.side not in (-1,1) or not 0<self.risk_fraction<=.02: raise ValueError('Invalid signal side/risk')
        object.__setattr__(self,'targets',tuple(self.targets))
        object.__setattr__(self,'fractions',tuple(self.fractions))
        object.__setattr__(self,'evidence',freeze(self.evidence))

@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: datetime
    equity: Decimal
    account_currency: str
    available_buying_power: Decimal | None
    approved_cash_risk: Decimal | None = None
    source: str = "research"

@dataclass(frozen=True)
class Conversion:
    timestamp: datetime
    from_currency: str
    to_currency: str
    rate: Decimal

@dataclass(frozen=True)
class Costs:
    spread: Decimal
    slippage: Decimal
    fee_per_side: Decimal
    timestamp: datetime
    provenance: str
    price_basis: str

    def __post_init__(self):
        aware(self.timestamp)
        if any(not v.is_finite() or v<0 for v in (self.spread,self.slippage,self.fee_per_side)):
            raise ValueError('Invalid/nonfinite costs')

@dataclass(frozen=True)
class SizingDecision:
    accepted: bool
    quantity: Decimal
    planned_cash_risk: Decimal
    required_margin: Decimal
    costs: Costs
    reasons: tuple = ()

@dataclass(frozen=True)
class Decision:
    status: str
    proposal: SignalProposal | None = None
    reasons: tuple = ()
    features: Mapping = field(default_factory=lambda: freeze({}))

@dataclass(frozen=True)
class Context:
    setup: object
    execution: object
    instrument: InstrumentSpec
    features: Mapping
    policy: PolicySnapshot
    config: object
    as_of: datetime
    session_close: datetime

    def __post_init__(self):
        aware(self.as_of)
        for bars in (self.setup, self.execution):
            if len(bars) and (bars.available_at > self.as_of).any():
                raise ValueError("Future bars in evaluation context")
            if len(bars) and ('complete' in bars and not bars.complete.all()):
                raise ValueError('Incomplete bars in evaluation context')
            if len(bars) and (bars.available_at<bars.close_time).any():
                raise ValueError('Bar available before close')
