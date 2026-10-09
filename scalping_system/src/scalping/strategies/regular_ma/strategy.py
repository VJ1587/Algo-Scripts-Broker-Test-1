"""Bounded approach zone, deterministic level choice and execution confirmation."""
from dataclasses import dataclass, field
import math
from ...contracts import freeze, OrderType
from ...strategy_support import CommonConfig, preflight, rejected, arm, emit, directional_confirmation, distance

@dataclass(frozen=True)
class Config(CommonConfig):
    strategy_id: str = 'regular_ma'
    coefficients: object = field(default_factory=lambda: freeze(dict(approach_min=1.,approach_max=1.5,stop=.5,target1=.5,target2=1.5)))
    required_predicates: tuple = ('approach_zone','oscillator','alignment_or_range','directional_candle')

def select_level(r,side,minimum,maximum,regime):
    if not math.isfinite(r.stoch): return None
    if regime!='range' and not all(math.isfinite(v) for v in (r.ema15,r.ema30)): return None
    if (r.stoch>=50 if side==1 else r.stoch<=50): return None
    if regime!='range' and side*(r.ema15-r.ema30)<=0: return None
    candidates=[]
    for rank,name in enumerate(('ema65','ema200','vwap')):
        v=r[name]
        if math.isfinite(v) and minimum<=side*(r.close-v)<=maximum:
            candidates.append((abs(r.close-v),rank,name,v))
    return min(candidates)[2:] if candidates else None

class Strategy:
    Config=Config
    def evaluate(self,ctx,state=None):
        state,error=preflight(ctx,state)
        if error: return rejected(ctx,state,error,'not_ready' if not ctx.policy.ready else 'rejected')
        if state.candidate:
            state,confirmed=directional_confirmation(ctx,state)
            return emit(ctx,state) if confirmed else rejected(ctx,state,'AWAIT_CONFIRMATION')
        if not len(ctx.setup): return rejected(ctx,state,'INSUFFICIENT_HISTORY')
        r=ctx.setup.iloc[-1]
        for side in (1,-1):
            chosen=select_level(r,side,float(distance(ctx.policy,ctx.instrument,'approach_min')),float(distance(ctx.policy,ctx.instrument,'approach_max')),ctx.policy.regime)
            if chosen:
                name,level=chosen
                state,error=arm(ctx,state,side,level,level,OrderType.LIMIT,{'level':name,'approach':abs(r.close-level)})
                return rejected(ctx,state,error or 'ARMED')
        return rejected(ctx,state,'NO_LEVEL_INTERACTION')
