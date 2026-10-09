import pytest
from conftest import setup_bars, mirror, later
from scalping.strategies.double.strategy import Strategy, pivot_pair, is_pivot

def fixture():
    b=setup_bars(12); b['low']=99.5; b['high']=100.5
    b.loc[b.index[2],'low']=99.
    b.loc[b.index[5],'high']=101.
    b.loc[b.index[10],'low']=99.1
    b.loc[b.index[11],['open','high','close']]=[100.,101.5,101.3]
    return b

@pytest.mark.parametrize('side',[1,-1])
def test_confirmed_pivots_neckline_and_dedup(make_context,side):
    b=fixture(); b=b if side==1 else mirror(b)
    ctx=make_context('double',bars=b,regime='range'); strategy=Strategy(); d,s=strategy.evaluate(ctx)
    assert d.proposal and d.proposal.side==side
    duplicate,_=strategy.evaluate(later(ctx),s)
    assert not duplicate.proposal
    assert float(d.proposal.stop)==pytest.approx(98.5 if side==1 else 101.5)

def test_pivot_unknown_until_right_close():
    b=fixture()
    assert not is_pivot(b.iloc[:-1],10,1)
    assert is_pivot(b,10,1)
    assert pivot_pair(b,1,.5,1.)
    assert not pivot_pair(b,1,.5,1.,minimum=45)
    assert not pivot_pair(b,1,.5,1.,maximum=35)

@pytest.mark.parametrize('failure',['tolerance','excursion','neckline'])
def test_near_miss(make_context,failure):
    b=fixture()
    if failure=='tolerance': b.loc[b.index[10],'low']=98.4
    if failure=='excursion': b['high']=99.8; b['open']=99.7; b['close']=99.7
    if failure=='neckline': b.loc[b.index[-1],['high','close']]=[100.5,100.2]
    d,_=Strategy().evaluate(make_context('double',bars=b,regime='range'))
    assert not d.proposal
