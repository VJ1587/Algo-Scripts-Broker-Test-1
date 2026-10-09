"""Prior six-bar consolidation, confirmed directional escape, market proposal."""
from dataclasses import dataclass, field
from ...contracts import freeze, OrderType
from ...strategy_support import CommonConfig, preflight, rejected, arm, emit, directional_confirmation, distance

@dataclass(frozen=True)
class Config(CommonConfig):
    strategy_id: str = 'base'
    coefficients: object = field(default_factory=lambda: freeze(dict(width=.5,breakout=0.000001,stop=.5,target1=.5,target2=1.5)))
    required_predicates: tuple = ('six_completed_prior_bars','narrow_range','directional_escape')
    base_minutes: int = 30

    def __post_init__(self):
        super().__post_init__()
        if self.base_minutes!=30: raise ValueError('Base minimum is 30 minutes in this version')

def base_setup(bars,width,tick):
    if len(bars)<7: return None
    history=bars.iloc[-7:-1]; r=bars.iloc[-1]
    if history.session_id.nunique()!=1 or r.session_id!=history.session_id.iloc[0]: return None
    if (history.close_time.iloc[-1]-history.open_time.iloc[0]).total_seconds()!=1800: return None
    if (history.index.to_series().diff().dropna().dt.total_seconds()!=300).any(): return None
    high,low=history.high.max(),history.low.min()
    if high-low>width: return None
    if r.close>high+tick and r.close>r.open: return 1,high,low
    if r.close<low-tick and r.close<r.open: return -1,low,high
    return None

class Strategy:
    Config=Config
    def evaluate(self,ctx,state=None):
        state,error=preflight(ctx,state)
        if error: return rejected(ctx,state,error,'not_ready' if not ctx.policy.ready else 'rejected')
        if state.candidate:
            state,confirmed=directional_confirmation(ctx,state,execution=False,boundary=state.candidate['boundary'])
            return emit(ctx,state,ctx.setup.close.iloc[-1]) if confirmed else rejected(ctx,state,'AWAIT_BREAKOUT_CONFIRMATION')
        found=base_setup(ctx.setup,float(distance(ctx.policy,ctx.instrument,'width')),float(distance(ctx.policy,ctx.instrument,'breakout')))
        if not found: return rejected(ctx,state,'NO_NARROW_BASE_ESCAPE')
        side,boundary,structure=found
        state,error=arm(ctx,state,side,ctx.setup.close.iloc[-1],structure,OrderType.MARKET,
            {'base_boundary':boundary,'base_minutes':30},extra={'boundary':boundary})
        if error: return rejected(ctx,state,error)
        if ctx.policy.confirmation_bars==1: return emit(ctx,state)
        from dataclasses import replace
        state=replace(state,confirmation_count=1)
        return rejected(ctx,state,'ARMED')
