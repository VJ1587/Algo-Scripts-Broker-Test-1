import numpy as np
import pytest
from conftest import setup_bars, mirror, later
from scalping.strategies.regular_ma.strategy import Strategy, select_level

def fixture():
    b=setup_bars(); b.loc[b.index[-1],['close','ema15','ema30','ema65','ema200','stoch']]=[100.,101.,100.5,98.8,98.8,30.]
    return b

@pytest.mark.parametrize('side',[1,-1])
def test_level_and_direction_confirmation(make_context,side):
    b=fixture(); b=b if side==1 else mirror(b)
    ctx=make_context('regular_ma',bars=b,regime='range'); strategy=Strategy()
    d,s=strategy.evaluate(ctx); assert not d.proposal and s.candidate['evidence']['level']=='ema65'
    ctx=later(ctx,opening=100.,closing=100.+side*.1)
    d,s=strategy.evaluate(ctx,s); assert d.proposal and d.proposal.side==side
    assert float(d.proposal.entry)==pytest.approx(98.8 if side==1 else 101.2)

def test_wrong_side_approach_and_unknown_vwap(make_context):
    b=fixture(); r=b.iloc[-1].copy(); r['vwap']=np.nan
    assert select_level(r,1,1,1.5,'directional')==('ema65',98.8)
    for name in ('ema65','ema200'): b.loc[b.index[-1],name]=101.2
    d,_=Strategy().evaluate(make_context('regular_ma',bars=b,regime='range'))
    assert not d.proposal and 'NO_LEVEL_INTERACTION' in d.reasons

def test_wrong_candle_does_not_confirm(make_context):
    ctx=make_context('regular_ma',bars=fixture(),regime='range'); strategy=Strategy(); _,s=strategy.evaluate(ctx)
    d,s=strategy.evaluate(later(ctx,opening=100,closing=99.9),s)
    assert not d.proposal and s.confirmation_count==0
