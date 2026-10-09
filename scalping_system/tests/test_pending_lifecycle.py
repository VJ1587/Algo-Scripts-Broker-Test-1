from dataclasses import replace
from conftest import setup_bars, later
from scalping.contracts import freeze
from scalping.strategies.trend import Strategy
from test_execution import proposal, simulator, bar, process

def test_order_proposed_state_waits_while_execution_owns_live_order(make_context):
    b=setup_bars(); b.loc[b.index[-2],['ema9','ema15']]=[99.8,99.9]
    b.loc[b.index[-1],['ema9','ema15','ema30','close']]=[100,99.9,99.8,100.2]
    ctx=make_context('trend',bars=b,regime='directional'); strategy=Strategy(); d,s=strategy.evaluate(ctx)
    assert d.proposal
    ctx=later(ctx,opening=100.2,closing=100.3)
    ctx=replace(ctx,features=freeze(dict(ctx.features,order_live=True)))
    d,s=strategy.evaluate(ctx,s)
    assert not d.proposal and s.phase=='ORDER_PROPOSED' and 'ORDER_LIVE' in d.reasons

def test_quality_cancel_does_not_change_a_filled_position(instrument):
    p=proposal(instrument); sim=simulator(instrument)
    process(sim,bar(p.decision_time,opening=100.5,high=100.6,low=100.3,close=100.5),[p])
    assert sim.pending
    sim.cancel_pending('QUALITY_PAUSE'); assert sim.pending is None and sim.position is None
    p=replace(p,signal_id='new_order'); process(sim,bar(p.decision_time+__import__('pandas').Timedelta(minutes=1)),[p])
    assert sim.position
    before=sim.position['stop']; sim.cancel_pending('QUALITY_PAUSE'); assert sim.position['stop']==before
