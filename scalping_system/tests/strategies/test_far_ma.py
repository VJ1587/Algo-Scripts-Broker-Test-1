from dataclasses import replace
import pytest
from conftest import setup_bars, mirror, later
from scalping.strategies.far_ma import Strategy
from scalping.contracts import freeze, StrategyState

def fixture():
    b=setup_bars(); b.loc[b.index[-1],['rsi','stoch']]=[20.,19.]
    return b

@pytest.mark.parametrize('side',[1,-1])
def test_setup_waits_for_future_one_minute_breakout(make_context,side):
    b=fixture(); b=b if side==1 else mirror(b)
    ctx=make_context('far_ma',bars=b,regime='range'); strategy=Strategy(); d,s=strategy.evaluate(ctx)
    assert not d.proposal and s.phase=='ARMED'
    s=StrategyState.from_json(s.to_json())
    ctx=later(ctx,opening=100.,closing=100.+side*.1)
    d,s=strategy.evaluate(ctx,s)
    assert d.proposal and d.proposal.order_type.value=='stop'
    trigger=float(d.proposal.entry)
    assert trigger==pytest.approx(100.12 if side==1 else 99.88)
    assert d.proposal.source_available_at==ctx.as_of
    duplicate,_=strategy.evaluate(ctx,s); assert not duplicate.proposal

@pytest.mark.parametrize('change,reason',[({'rsi':21},'NO_EXTENSION_EXTREMES'),({'stoch':20},'NO_EXTENSION_EXTREMES'),({'ema9':101.4},'NO_EXTENSION_EXTREMES')])
def test_mandatory_extremes_and_extension(make_context,change,reason):
    b=fixture()
    for k,v in change.items(): b.loc[b.index[-1],k]=v
    d,_=Strategy().evaluate(make_context('far_ma',bars=b,regime='range'))
    assert not d.proposal and reason in d.reasons

def test_expiry_structure_and_mean_invalidation(make_context):
    ctx=make_context('far_ma',bars=fixture(),regime='range'); strategy=Strategy(); _,s=strategy.evaluate(ctx)
    for later_ctx,reason in [(later(ctx,minutes=15),'EXPIRED'),(later(ctx,low=99.),'STRUCTURAL_INVALIDATION'),(later(ctx,high=102.1),'MEAN_TOUCHED')]:
        d,_=strategy.evaluate(later_ctx,s); assert reason in d.reasons

def test_mean_too_close_to_trigger(make_context):
    ctx=make_context('far_ma',bars=fixture(),regime='range'); strategy=Strategy(); _,s=strategy.evaluate(ctx)
    ctx=later(ctx,opening=100.,closing=101.6,high=101.7,low=99.9)
    d,_=strategy.evaluate(ctx,s); assert 'TARGET_MEAN_FILTER' in d.reasons

def test_directional_countertrend_blocked(make_context):
    b=mirror(fixture()); ctx=make_context('far_ma',bars=b,regime='directional')
    d,_=Strategy().evaluate(ctx); assert 'REGIME_DIRECTION' in d.reasons
