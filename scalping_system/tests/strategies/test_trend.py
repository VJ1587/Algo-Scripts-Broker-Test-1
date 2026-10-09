import pytest
from dataclasses import replace
from conftest import setup_bars, mirror, later
from scalping.strategies.trend import Strategy

def fixture():
    b=setup_bars()
    b.loc[b.index[-2],['ema9','ema15']]=[99.8,99.9]
    b.loc[b.index[-1],['ema9','ema15','ema30','close']]=[100.,99.9,99.8,100.2]
    return b

@pytest.mark.parametrize('side',[1,-1])
def test_valid_cross_and_pullback(make_context,side):
    b=fixture(); b=b if side==1 else mirror(b)
    ctx=make_context('trend',bars=b,regime='transition')
    # Transition freezes two future directional bars, retaining original crossing.
    strategy=Strategy(); d,s=strategy.evaluate(ctx)
    assert not d.proposal and s.phase=='ARMED'
    ctx=later(ctx,opening=100.2 if side==1 else 99.8,closing=100.3 if side==1 else 99.7)
    d,s=strategy.evaluate(ctx,s); assert not d.proposal
    ctx=later(ctx,opening=100.3 if side==1 else 99.7,closing=100.4 if side==1 else 99.6)
    d,s=strategy.evaluate(ctx,s)
    assert d.proposal and d.proposal.side==side and d.proposal.order_type.value=='limit'
    assert side*(d.proposal.structural_invalidation-d.proposal.stop)>0

@pytest.mark.parametrize('changes,reason',[({'ema30':100.1},'NO_CROSS_STACK'),({'ema30':99.},'EMA_SEPARATION'),({'close':99.8},'NO_PULLBACK_LIMIT')])
def test_near_miss(make_context,changes,reason):
    b=fixture()
    for k,v in changes.items(): b.loc[b.index[-1],k]=v
    d,s=Strategy().evaluate(make_context('trend',bars=b,regime='directional'))
    assert d.proposal is None and reason in d.reasons

def test_range_pause(make_context):
    d,_=Strategy().evaluate(make_context('trend',bars=fixture(),regime='range'))
    assert not d.proposal and d.status=='not_ready'
