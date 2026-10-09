"""Strict confirmed pivots, timed excursion and neckline confirmation."""
from dataclasses import dataclass, field, replace
from ...contracts import freeze, OrderType
from ...strategy_support import CommonConfig, preflight, rejected, arm, emit, directional_confirmation, distance

@dataclass(frozen=True)
class Config(CommonConfig):
    strategy_id: str = 'double'
    coefficients: object = field(default_factory=lambda: freeze(dict(tolerance=.5,excursion=1.,stop=.5,target1=.5,target2=1.5)))
    required_predicates: tuple = ('two_strict_pivots','timed_separation','excursion','neckline')
    separation_min: int = 30
    separation_max: int = 120

    def __post_init__(self):
        super().__post_init__()
        if self.separation_min<30 or self.separation_max>120 or self.separation_min>self.separation_max:
            raise ValueError('Pivot timing bounds')

def is_pivot(bars,j,side):
    if not 0<j<len(bars)-1: return False
    col='low' if side==1 else 'high'
    value=bars.iloc[j][col]
    return (value<bars.iloc[j-1][col] and value<bars.iloc[j+1][col]) if side==1 else (value>bars.iloc[j-1][col] and value>bars.iloc[j+1][col])

def pivot_pair(bars,side,tolerance,excursion,minimum=30,maximum=120):
    j=len(bars)-2
    if not is_pivot(bars,j,side): return None
    col='low' if side==1 else 'high'; second=bars.iloc[j][col]
    for k in range(j-1,0,-1):
        elapsed=(bars.index[j]-bars.index[k]).total_seconds()/60
        if elapsed<minimum: continue
        if elapsed>maximum: break
        if bars.iloc[k:j+2].session_id.nunique()!=1: continue
        window=bars.iloc[k:j+2]
        if window.missing_before.any() or (window.index.to_series().diff().dropna().dt.total_seconds()!=300).any(): continue
        if not is_pivot(bars,k,side): continue
        first=bars.iloc[k][col]; middle=bars.iloc[k+1:j]
        neckline=middle.high.max() if side==1 else middle.low.min()
        if abs(first-second)<=tolerance and side*(neckline-first)>=excursion:
            return dict(first=str(bars.index[k]),second=str(bars.index[j]),neckline=float(neckline),structure=float(min(first,second) if side==1 else max(first,second)))
    return None

class Strategy:
    Config=Config
    def evaluate(self,ctx,state=None):
        state,error=preflight(ctx,state)
        if error: return rejected(ctx,state,error,'not_ready' if not ctx.policy.ready else 'rejected')
        if state.candidate:
            state,confirmed=directional_confirmation(ctx,state,execution=False,boundary=state.candidate['neckline'])
            return emit(ctx,state,ctx.setup.close.iloc[-1]) if confirmed else rejected(ctx,state,'AWAIT_NECKLINE')
        for side in (1,-1):
            pair=pivot_pair(ctx.setup,side,float(distance(ctx.policy,ctx.instrument,'tolerance')),float(distance(ctx.policy,ctx.instrument,'excursion')),ctx.config.separation_min,ctx.config.separation_max)
            if not pair: continue
            state,error=arm(ctx,state,side,ctx.setup.close.iloc[-1],pair['structure'],OrderType.MARKET,pair,extra={'neckline':pair['neckline']})
            if error: return rejected(ctx,state,error)
            r=ctx.setup.iloc[-1]
            if side*(r.close-pair['neckline'])>0 and side*(r.close-r.open)>0:
                state=replace(state,confirmation_count=1)
                if ctx.policy.confirmation_bars==1: return emit(ctx,state)
            return rejected(ctx,state,'ARMED_AWAIT_NECKLINE')
        return rejected(ctx,state,'NO_CONFIRMED_PIVOT_PAIR')
