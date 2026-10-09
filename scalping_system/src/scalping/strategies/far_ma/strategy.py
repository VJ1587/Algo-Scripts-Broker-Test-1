"""Oscillator extension -> later directional one-minute bar -> breakout stop."""
from dataclasses import dataclass, field, replace
import math
from ...contracts import freeze, OrderType
from ...strategy_support import CommonConfig, preflight, rejected, arm, emit, directional_confirmation, distance

@dataclass(frozen=True)
class Config(CommonConfig):
    strategy_id: str = 'far_ma'
    coefficients: object = field(default_factory=lambda: freeze(dict(extension=1.5,stop=.5,target1=.5,target2=1.5)))
    required_predicates: tuple = ('nearest_mean_extension','rsi_extreme','stoch_extreme','directional_candle','breakout_stop')

def extension_setup(r,threshold):
    means=[r[f'ema{n}'] for n in (9,15,30,65,200)]
    if not all(math.isfinite(v) for v in means): return None
    mean=min(means,key=lambda v:abs(v-r.close))
    if mean-r.close>=threshold and r.rsi<=20 and r.stoch<20: return 1,mean
    if r.close-mean>=threshold and r.rsi>=80 and r.stoch>80: return -1,mean
    return None

class Strategy:
    Config=Config
    def evaluate(self,ctx,state=None):
        state,error=preflight(ctx,state)
        if error: return rejected(ctx,state,error,'not_ready' if not ctx.policy.ready else 'rejected')
        if state.candidate:
            c=state.candidate; r=ctx.execution.iloc[-1]
            if (r.high>=c['mean'] if c['side']==1 else r.low<=c['mean']):
                return rejected(ctx,replace(state,phase='INVALIDATED',candidate=freeze({})),'MEAN_TOUCHED')
            state,confirmed=directional_confirmation(ctx,state)
            if confirmed:
                trigger=r.high+float(ctx.instrument.tick_size) if c['side']==1 else r.low-float(ctx.instrument.tick_size)
                return emit(ctx,state,trigger)
            return rejected(ctx,state,'AWAIT_CONFIRMATION')
        if not len(ctx.setup): return rejected(ctx,state,'INSUFFICIENT_HISTORY')
        r=ctx.setup.iloc[-1]; found=extension_setup(r,float(distance(ctx.policy,ctx.instrument,'extension')))
        if not found: return rejected(ctx,state,'NO_EXTENSION_EXTREMES')
        side,mean=found
        state,error=arm(ctx,state,side,r.close,r.low if side==1 else r.high,OrderType.STOP,
            {'extreme_rsi':r.rsi,'extreme_stoch':r.stoch,'mean':mean},extra={'mean':mean})
        return rejected(ctx,state,error or 'ARMED')
