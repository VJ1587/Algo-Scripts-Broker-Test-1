"""EMA cross, ordered stack and nonmarketable pullback limit."""
from dataclasses import dataclass, field
from ...contracts import freeze, OrderType
from ...strategy_support import CommonConfig, preflight, rejected, arm, emit, directional_confirmation, distance

@dataclass(frozen=True)
class Config(CommonConfig):
    strategy_id: str = 'trend'
    coefficients: object = field(default_factory=lambda: freeze(dict(separation=.5,stop=.5,target1=1.,target2=2.)))
    required_predicates: tuple = ('cross','stack','separation','pullback_limit')

def cross_side(previous, current):
    if current.ema9>current.ema15>current.ema30 and previous.ema9<=previous.ema15: return 1
    if current.ema9<current.ema15<current.ema30 and previous.ema9>=previous.ema15: return -1
    return 0

class Strategy:
    Config=Config
    def evaluate(self,ctx,state=None):
        state,error=preflight(ctx,state)
        if error: return rejected(ctx,state,error,'not_ready' if not ctx.policy.ready else 'rejected')
        if state.candidate:
            state,confirmed=directional_confirmation(ctx,state)
            return emit(ctx,state) if confirmed else rejected(ctx,state,'AWAIT_CONFIRMATION')
        if len(ctx.setup)<2: return rejected(ctx,state,'INSUFFICIENT_HISTORY')
        r=ctx.setup.iloc[-1]; side=cross_side(ctx.setup.iloc[-2],r)
        if not side: return rejected(ctx,state,'NO_CROSS_STACK')
        if max(r.ema9,r.ema15,r.ema30)-min(r.ema9,r.ema15,r.ema30)>float(distance(ctx.policy,ctx.instrument,'separation')):
            return rejected(ctx,state,'EMA_SEPARATION')
        state,error=arm(ctx,state,side,r.ema9,r.ema30,OrderType.LIMIT,{'cross':str(ctx.setup.index[-1]),'stack':True})
        if error: return rejected(ctx,state,error)
        if ctx.policy.confirmation_bars>1: return rejected(ctx,state,'ARMED')
        return emit(ctx,state)
