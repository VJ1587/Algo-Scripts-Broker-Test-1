"""Versioned config validation and order/lifecycle primitives, no setup logic."""
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from datetime import timedelta
from collections.abc import Mapping
import math
import yaml
from .contracts import StrategyState, Decision, SignalProposal, OrderType, freeze, stable_id
from .instrument import decimal, round_price
from .execution.costs import estimate_costs, round_trip_price
from .adaptation.regime import validate_config

@dataclass(frozen=True)
class CommonConfig:
    strategy_id: str
    schema_version: int = 1
    version: str = '1.0'
    setup_timeframe: str = '5min'
    execution_timeframe: str = '1min'
    mode: str = 'adaptive'
    ema_periods: tuple = (9,15,30,65,200)
    rsi_period: int = 14
    stoch_period: int = 14
    stoch_smooth: int = 3
    extreme_levels: tuple = (20,80)
    coefficients: Mapping = field(default_factory=lambda: freeze(dict(stop=.5,target1=.5,target2=1.5)))
    required_predicates: tuple = ()
    confirmation_bars: int = 1
    strong_confirmation_bars: int = 2
    expiry: int = 15
    max_hold: int = 15
    target_fractions: tuple = (.5,.5)
    risk_fraction: float = .005
    risk_cap: float = .02
    pyramiding: bool = False
    partial_size_policy: str = 'skip'
    adaptation: Mapping = field(default_factory=lambda: freeze({}))
    adaptation_override_bounds: Mapping = field(default_factory=lambda: freeze(dict(scale_min=.5,scale_max=2)))
    legacy_distance_unit: float = 1.
    max_stop_atr: float = 5.

    def __post_init__(self):
        object.__setattr__(self,'coefficients',freeze(self.coefficients))
        object.__setattr__(self,'adaptation',freeze(validate_config(dict(self.adaptation))))
        object.__setattr__(self,'adaptation_override_bounds',freeze(self.adaptation_override_bounds))
        for name in ('ema_periods','extreme_levels','target_fractions','required_predicates'):
            object.__setattr__(self,name,tuple(getattr(self,name)))
        for f in fields(self):
            v=getattr(self,f.name)
            if isinstance(v,float) and not math.isfinite(v): raise ValueError(f'Nonfinite {f.name}')
        if self.mode not in ('legacy_static','fixed_atr','adaptive'): raise ValueError('Invalid mode')
        if not 0<self.risk_fraction<=self.risk_cap<=.02: raise ValueError('Risk cap exceeded')
        if self.pyramiding or self.partial_size_policy!='skip': raise ValueError('Unsupported sizing/pyramiding policy')
        if not 0<self.expiry<=15 or not 0<self.max_hold<=15: raise ValueError('Timing bounds exceeded')
        if self.confirmation_bars!=1 or self.strong_confirmation_bars!=2: raise ValueError('Versioned confirmation counts required')
        if self.execution_timeframe!='1min' or self.setup_timeframe not in ('5min','15min','30min'): raise ValueError('Unsupported timeframe')
        if self.setup_timeframe!='5min' and (self.strategy_id!='regular_ma' or self.version=='1.0'): raise ValueError('Named regular-MA version required')
        if self.ema_periods!=(9,15,30,65,200) or (self.rsi_period,self.stoch_period,self.stoch_smooth)!=(14,14,3) or self.extreme_levels!=(20,80):
            raise ValueError('Alternative indicators require a named implementation')
        if len(self.target_fractions)!=2 or any(x<=0 or not math.isfinite(x) for x in self.target_fractions) or abs(sum(self.target_fractions)-1)>1e-9:
            raise ValueError('Two valid target fractions required')
        if self.legacy_distance_unit<=0 or self.max_stop_atr<=0: raise ValueError('Invalid distance')
        for k,v in self.coefficients.items():
            if not math.isfinite(v) or v<=0: raise ValueError(f'Invalid coefficient {k}')
        if not {'stop','target1','target2'}<=set(self.coefficients): raise ValueError('Missing distances')
        if self.coefficients['target2']<=self.coefficients['target1']: raise ValueError('Targets must increase')
        if dict(self.adaptation_override_bounds)!=dict(scale_min=.5,scale_max=2): raise ValueError('Hard adaptation bounds')
        if self.adaptation['scale_min']<.5 or self.adaptation['scale_max']>2: raise ValueError('Scale override exceeds hard bounds')
        if self.schema_version!=1: raise ValueError('Schema version')
        for name in ('schema_version','confirmation_bars','strong_confirmation_bars','expiry','max_hold'):
            if type(getattr(self,name)) is not int: raise ValueError(f'Integer {name} required')
        if not isinstance(self.version,str) or not self.version: raise ValueError('Named version required')
        expected_id=next(f for f in fields(self) if f.name=='strategy_id').default
        if self.strategy_id!=expected_id: raise ValueError('Strategy ID mismatch')
        schema=next(f for f in fields(self) if f.name=='coefficients').default_factory()
        if set(self.coefficients)!=set(schema): raise ValueError('Unknown/missing strategy coefficient')
        if 'approach_min' in self.coefficients and self.coefficients['approach_min']>self.coefficients['approach_max']:
            raise ValueError('Invalid approach zone')
        if not self.required_predicates or tuple(self.required_predicates)!=tuple(next(f for f in fields(self) if f.name=='required_predicates').default):
            raise ValueError('Mandatory predicates cannot be removed')

def load_config(cls, path=None, mode=None, adaptation=None):
    raw=yaml.safe_load(Path(path).read_text()) if path else {}
    if raw is None: raw={}
    if set(raw)-{f.name for f in fields(cls)}: raise ValueError('Unknown strategy keys')
    if mode: raw['mode']=mode
    if adaptation: raw['adaptation']={**adaptation,**raw.get('adaptation',{})}
    return cls(**raw)

def distance(policy, instrument, name):
    return decimal(policy.distances[name])*instrument.tick_size

def rejected(ctx,state,reason,status='rejected'):
    return Decision(status,None,tuple(reason.split(',')),freeze(ctx.features)),state

def preflight(ctx,state):
    if state is None: state=StrategyState(ctx.config.strategy_id,ctx.config.version)
    if state.strategy_id!=ctx.config.strategy_id or state.version!=ctx.config.version: raise ValueError('State strategy/version mismatch')
    event=str(ctx.as_of)
    if state.last_processed_bar_id==event: return state,'DUPLICATE_EVENT'
    state=replace(state,last_processed_bar_id=event)
    if ctx.as_of>=ctx.session_close:
        return replace(state,phase='EXPIRED' if state.candidate else 'IDLE',candidate=freeze({}),confirmation_count=0),'SESSION_CLOSED'
    if state.phase=='ORDER_PROPOSED' and ctx.features.get('order_live',False):
        return state,'ORDER_LIVE'
    if state.phase in ('EXPIRED','INVALIDATED','ORDER_PROPOSED'):
        state=replace(state,phase='IDLE',candidate=freeze({}),confirmation_count=0,frozen_policy=None)
    if state.candidate:
        c=state.candidate
        if ctx.as_of>=__import__('pandas').Timestamp(c['expiry']):
            return replace(state,phase='EXPIRED',candidate=freeze({}),confirmation_count=0),'EXPIRED'
        if len(ctx.execution):
            r=ctx.execution.iloc[-1]
            if (r.low<=c['stop'] if c['side']==1 else r.high>=c['stop']):
                return replace(state,phase='INVALIDATED',candidate=freeze({}),confirmation_count=0),'STRUCTURAL_INVALIDATION'
    if not ctx.policy.ready:
        return replace(state,phase='INVALIDATED' if state.candidate else 'IDLE',candidate=freeze({}),confirmation_count=0),','.join(ctx.policy.reasons)
    if state.candidate and state.candidate['side'] not in ctx.policy.allowed_sides:
        return replace(state,phase='INVALIDATED',candidate=freeze({}),confirmation_count=0),'REGIME_CANCELLED'
    return state,None

def arm(ctx,state,side,entry,structure,order_type,evidence,extra=None):
    setup=str(ctx.setup.index[-1])
    key=stable_id(dict(strategy=ctx.config.strategy_id,setup=setup,side=side,evidence=evidence))
    if key in state.consumed_setups: return state,'DUPLICATE_SETUP'
    if side not in ctx.policy.allowed_sides: return state,'REGIME_DIRECTION'
    if ctx.policy.regime=='directional' and ctx.config.strategy_id in ('trend','regular_ma','base'):
        r=ctx.setup.iloc[-1]
        if not (side*(r.ema9-r.ema15)>0 and side*(r.ema15-r.ema30)>0 and side*ctx.features['slope']>0):
            return state,'REGIME_STACK_ALIGNMENT'
    if side<0 and not ctx.instrument.short_allowed: return state,'SHORT_UNAVAILABLE'
    stop=decimal(structure)-side*distance(ctx.policy,ctx.instrument,'stop')
    stop=round_price(stop,ctx.instrument.tick_size,'down' if side==1 else 'up')
    expiry=min(ctx.as_of+timedelta(minutes=ctx.policy.expires_after),ctx.session_close)
    c=dict(side=side,entry=float(entry),structure=float(structure),stop=float(stop),order_type=order_type.value,
        setup=setup,armed_at=str(ctx.as_of),expiry=str(expiry),evidence=evidence,key=key,
        risk_fraction=ctx.config.risk_fraction,target_fractions=ctx.config.target_fractions,config_id=stable_id(ctx.config),
        min_target_cost_ratio=ctx.config.adaptation['min_target_cost_ratio'],max_stop_atr=ctx.config.max_stop_atr,session_close=str(ctx.session_close),**(extra or {}))
    return replace(state,phase='ARMED',candidate=freeze(c),confirmation_count=0,frozen_policy=ctx.policy,
        consumed_setups=state.consumed_setups+(key,)),None

def directional_confirmation(ctx,state,execution=True,boundary=None):
    bars=ctx.execution if execution else ctx.setup
    if not len(bars): return state,False
    r=bars.iloc[-1]; c=state.candidate
    if r.available_at<=__import__('pandas').Timestamp(c['armed_at']): return state,False
    if c.get('last_confirmation')==str(r.available_at): return state,False
    valid=c['side']*(r.close-r.open)>0
    if boundary is not None: valid=valid and c['side']*(r.close-boundary)>0
    count=state.confirmation_count+1 if valid else 0
    candidate=dict(c,last_confirmation=str(r.available_at))
    return replace(state,phase='CONFIRMING',candidate=freeze(candidate),confirmation_count=count),count>=state.frozen_policy.confirmation_bars

def emit(ctx,state,entry=None):
    c=state.candidate; p=state.frozen_policy; s=c['side']; i=ctx.instrument
    entry=decimal(c['entry'] if entry is None else entry)
    kind=OrderType(c['order_type'])
    entry=round_price(entry,i.tick_size,'down' if (s==1)==(kind==OrderType.LIMIT) else 'up')
    stop=decimal(c['stop'])
    if s*(entry-stop)<i.tick_size or abs(entry-stop)>decimal(c['max_stop_atr']*p.effective_atr):
        return rejected(ctx,replace(state,phase='INVALIDATED'),'INVALID_OR_DISTANT_STOP')
    if kind==OrderType.LIMIT:
        if not s*(decimal(ctx.features['ask'] if s==1 else ctx.features['bid'])-entry)>0:
            return rejected(ctx,replace(state,phase='INVALIDATED'),'NO_PULLBACK_LIMIT')
    targets=tuple(round_price(entry+s*distance(p,i,k),i.tick_size,'down' if s==1 else 'up') for k in ('target1','target2'))
    costs=estimate_costs(i,ctx.as_of)
    # Use current measured spread, including quotes, rather than configured fallback.
    costs=replace(costs,spread=decimal(ctx.features['spread']),provenance=ctx.features.get('cost_provenance','OHLC_estimated'))
    try: cost=round_trip_price(costs,i,ctx.features.get('conversion'))
    except ValueError: return rejected(ctx,replace(state,phase='INVALIDATED'),'FX_CONVERSION')
    if s*(targets[0]-entry)<=decimal(c['min_target_cost_ratio'])*cost:
        return rejected(ctx,replace(state,phase='INVALIDATED'),'TARGET_COST_FILTER')
    if ctx.config.strategy_id=='far_ma' and s*(decimal(c['mean'])-entry)<s*(targets[0]-entry)+cost:
        return rejected(ctx,replace(state,phase='INVALIDATED'),'TARGET_MEAN_FILTER')
    identity=dict(strategy=ctx.config.strategy_id,version=ctx.config.version,config=c['config_id'],instrument=i.instrument_id,
        setup=c['key'],decision=str(ctx.as_of))
    proposal=SignalProposal(stable_id(identity),ctx.config.strategy_id,ctx.config.version,i.instrument_id,ctx.as_of,ctx.as_of,s,kind,entry,
        decimal(c['structure']),stop,targets,tuple(c['target_fractions']),__import__('pandas').Timestamp(c['expiry']),p.hold_timeout,p,
        c['risk_fraction'],freeze(dict(c['evidence'],min_target_cost_ratio=c['min_target_cost_ratio'],session_close=c['session_close'],config_id=c['config_id'])))
    return Decision('accepted',proposal,(),freeze(ctx.features)),replace(state,phase='ORDER_PROPOSED',last_emitted_signal_id=proposal.signal_id)
