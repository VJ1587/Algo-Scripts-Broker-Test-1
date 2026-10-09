import pytest
from conftest import setup_bars, mirror, later
from scalping.strategies.base import Strategy

def fixture():
    b=setup_bars(8); b.loc[b.index[-1],['open','high','low','close']]=[100.,100.4,99.9,100.3]
    return b

@pytest.mark.parametrize('side',[1,-1])
def test_six_prior_bars_exclude_breakout(make_context,side):
    b=fixture(); b=b if side==1 else mirror(b)
    d,_=Strategy().evaluate(make_context('base',bars=b,regime='range'))
    assert d.proposal and d.proposal.side==side and d.proposal.order_type.value=='market'
    assert float(d.proposal.structural_invalidation)==pytest.approx(99.9 if side==1 else 100.1)

@pytest.mark.parametrize('wide,false_body',[(True,False),(False,True)])
def test_wide_or_false_breakout(make_context,wide,false_body):
    b=fixture()
    if wide: b.loc[b.index[-2],'high']=100.6
    if false_body: b.loc[b.index[-1],'open']=100.35
    d,_=Strategy().evaluate(make_context('base',bars=b,regime='range'))
    assert not d.proposal

def test_transition_two_setup_closes(make_context):
    b=fixture(); ctx=make_context('base',bars=b,regime='transition'); strategy=Strategy()
    d,s=strategy.evaluate(ctx); assert not d.proposal and s.confirmation_count==1
    row=b.iloc[-1:].copy(); row.index=row.index+__import__('pandas').Timedelta(minutes=5)
    for col in ('available_at','open_time','close_time'): row[col]=row[col]+__import__('pandas').Timedelta(minutes=5)
    row['open']=100.3; row['close']=100.35
    updated=__import__('pandas').concat([b,row])
    d,s=strategy.evaluate(later(ctx,minutes=5,opening=100.3,closing=100.35,setup=updated),s)
    assert d.proposal
