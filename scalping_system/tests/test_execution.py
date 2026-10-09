from dataclasses import replace
from decimal import Decimal as D
import pandas as pd
import pytest
from scalping.contracts import OrderType, AccountSnapshot
from scalping.sessions import SessionCalendar, Session
from scalping.execution.simulator import Simulator
from scalping.execution.costs import estimate_costs
from scalping.execution.sizing import size_order
from scalping.instrument import load_instrument
from scalping.strategies.trend import Strategy
from conftest import setup_bars, context, ROOT

def proposal(instrument,kind=OrderType.LIMIT,entry=100,stop=99,targets=(101,102)):
    b=setup_bars(); b.loc[b.index[-2],['ema9','ema15']]=[99.8,99.9]
    b.loc[b.index[-1],['ema9','ema15','ema30','close']]=[100.,99.9,99.8,100.2]
    p=Strategy().evaluate(context(instrument,'trend',bars=b,regime='directional'))[0].proposal
    return replace(p,order_type=kind,entry=D(entry),stop=D(stop),targets=tuple(D(t) for t in targets))

def bar(ts,opening=100,high=100.2,low=99.8,close=100):
    t=pd.Timestamp(ts)
    return pd.Series(dict(open_time=t,close_time=t+pd.Timedelta(minutes=1),available_at=t+pd.Timedelta(minutes=1),open=opening,high=high,low=low,close=close))

def simulator(instrument,**kwargs):
    return Simulator(instrument,SessionCalendar.load(instrument.session_calendar_path),**kwargs)

def process(sim,b,signals=()):
    sim.process(b,signals,estimate_costs(sim.instrument,b.open_time))

def test_stop_and_limit_distinction_and_no_same_bar_fill(instrument):
    p=proposal(instrument); t=p.decision_time
    b=bar(t,opening=100.5,low=100.3,high=100.7,close=100.5)
    limit=simulator(instrument); process(limit,b,[p]); assert not limit.position and limit.pending
    stop=simulator(instrument); process(stop,b,[replace(p,order_type=OrderType.STOP)]); assert stop.position
    assert stop.position['entry']>p.entry
    with pytest.raises(ValueError): process(simulator(instrument),bar(t-pd.Timedelta(minutes=1)),[p])
    latency=simulator(instrument,latency_seconds=1); process(latency,bar(t),[p]); assert not latency.position
    process(latency,bar(t+pd.Timedelta(minutes=1))); assert latency.position

def test_limit_never_worse_and_target_limits_have_no_adverse_slip(instrument):
    p=proposal(instrument); sim=simulator(instrument); t=p.decision_time
    process(sim,bar(t,opening=99.8,high=100.3,low=99.7),[p])
    assert sim.position and sim.position['entry']<=p.entry
    entry=sim.position['entry']; qty=sim.position['initial_qty']
    process(sim,bar(t+pd.Timedelta(minutes=1),opening=100.8,high=102.1,low=100.7,close=102))
    assert not sim.position and sim.trades[-1]['reason']=='targets'
    first=D(int(qty/2)); expected=(D(101)-entry)*first+(D(102)-entry)*(qty-first)
    assert sim.trades[-1]['pnl']==pytest.approx(float(expected))

def test_stop_gap_ambiguity_and_initial_loss_drawdown(instrument):
    p=proposal(instrument); sim=simulator(instrument); t=p.decision_time
    process(sim,bar(t),[p]); assert sim.position
    process(sim,bar(t+pd.Timedelta(minutes=1),opening=98,high=102.2,low=97.5,close=99))
    assert sim.trades[-1]['reason']=='stop' and sim.trades[-1]['pnl'] < -sim.trades[-1]['planned_risk']
    assert sim.ambiguities==1 and sim.metrics()['max_drawdown']<0

def test_partial_quantities_and_fees(instrument):
    instrument=replace(instrument,cost_model={**instrument.cost_model,'fee_per_unit_per_side':.01})
    p=proposal(instrument); sim=simulator(instrument); t=p.decision_time
    process(sim,bar(t),[p]); initial=sim.position['initial_qty']
    process(sim,bar(t+pd.Timedelta(minutes=1),opening=100.5,high=101.1,low=100.4,close=100.7))
    assert sim.position and sim.position['qty']>0 and sim.position['qty']<initial
    process(sim,bar(t+pd.Timedelta(minutes=2),opening=101.5,high=102.1,low=101.4,close=102))
    assert sim.trades[-1]['quantity']==float(initial) and sim.metrics()['fees_cash']==pytest.approx(float(initial*D('.02')))

def test_streaming_restart_and_idempotent_delivery(instrument):
    p=proposal(instrument); t=p.decision_time; full=simulator(instrument)
    process(full,bar(t),[p]); restarted=Simulator.from_json(full.to_json(),instrument,full.calendar)
    b=bar(t+pd.Timedelta(minutes=1),opening=101,high=102.1,low=100.9,close=102)
    process(full,b,[p]); process(restarted,b,[p]); process(restarted,b,[p])
    assert full.to_json()==restarted.to_json()
    assert len(full.trades)==1

def test_session_close_and_unpriceable_final(instrument):
    p=proposal(instrument); t=pd.Timestamp('2026-01-05T20:59Z')
    p=replace(p,decision_time=t,source_available_at=t,expiry=t+pd.Timedelta(minutes=1))
    sim=simulator(instrument); process(sim,bar(t),[p])
    assert sim.trades[-1]['reason']=='session_close' and not sim.position and not sim.pending
    p=proposal(instrument); sim=simulator(instrument); process(sim,bar(p.decision_time),[p]); sim.finish()
    assert sim.trades[-1]['pnl'] is None and 'UNPRICEABLE_FINAL_LIQUIDATION'==sim.trades[-1]['reason']
    assert sim.metrics()['trades']==0

def test_missing_end_of_session_is_not_carried(instrument):
    p=proposal(instrument); sim=simulator(instrument); process(sim,bar(p.decision_time),[p])
    process(sim,bar('2026-01-06T14:30Z'))
    assert not sim.position and sim.trades[-1]['reason']=='UNPRICEABLE_SESSION_LIQUIDATION'

def test_empty_data_metrics(instrument):
    sim=simulator(instrument); trades,curve,metrics=sim.finish()
    assert trades==[] and curve[0]['equity']==10000 and metrics['no_trade'] and metrics['profit_factor'] is None

def test_final_setup_cannot_propose_at_session_close(instrument):
    b=setup_bars(); b.loc[b.index[-2],['ema9','ema15']]=[99.8,99.9]
    b.loc[b.index[-1],['ema9','ema15','ema30','close']]=[100,99.9,99.8,100.2]
    ctx=context(instrument,'trend',bars=b,regime='directional')
    ctx=replace(ctx,session_close=ctx.as_of)
    decision,_=Strategy().evaluate(ctx)
    assert not decision.proposal and 'SESSION_CLOSED' in decision.reasons

def test_overnight_break_and_dst():
    # US local open stays 18:00 across the spring DST transition; dates are explicit.
    a=Session('before',pd.Timestamp('2026-03-07T00:00Z'),pd.Timestamp('2026-03-07T23:00Z'),((pd.Timestamp('2026-03-07T12:00Z'),pd.Timestamp('2026-03-07T12:30Z')),))
    b=Session('after',pd.Timestamp('2026-03-08T23:00Z'),pd.Timestamp('2026-03-09T22:00Z'))
    cal=SessionCalendar([a,b],'America/Chicago')
    assert a.start.tz_convert('America/Chicago').hour==b.start.tz_convert('America/Chicago').hour==18
    assert cal.session_at(pd.Timestamp('2026-03-07T00:30Z')).session_id=='before'
    assert cal.session_at(pd.Timestamp('2026-03-07T12:15Z')) is None
    assert cal.session_at(pd.Timestamp('2026-03-07T12:15Z'),True).session_id=='before'
    expected=cal.expected_bar_opens(a.start,a.end)
    assert len(expected)==23*60-30 and pd.Timestamp('2026-03-07T12:15Z') not in expected
    assert cal.next_close(pd.Timestamp('2026-03-09T04:00Z'))==b.end

def test_futures_margin_separate_from_stop_risk():
    future=load_instrument(ROOT/'configs/instruments/DEMO_FUTURE.yaml')
    p=proposal(future)
    p=replace(p,instrument_id=future.instrument_id,entry=D(5000),stop=D(4999),targets=(D(5001),D(5002)))
    account=AccountSnapshot(p.decision_time,D(100000),'USD',D(15000))
    s=size_order(p,account,future,estimate_costs(future,p.decision_time))
    assert not s.accepted and s.quantity<=1 and s.required_margin<=15000

def test_market_gap_resizes_and_anchors_targets_once(instrument):
    p=proposal(instrument,OrderType.MARKET); t=p.decision_time
    normal=simulator(instrument); gap=simulator(instrument)
    process(normal,bar(t),[p])
    process(gap,bar(t,opening=102,high=102.2,low=101.8,close=102),[p])
    assert normal.position and gap.position
    assert gap.position['initial_qty']<normal.position['initial_qty']
    assert gap.position['planned_risk']<=50
    targets=gap.position['targets']
    assert targets==tuple(gap.position['entry']+D(distance) for distance in (1,2))
    process(gap,bar(t+pd.Timedelta(minutes=1),opening=102,high=102.4,low=101.8,close=102.2))
    assert gap.position['targets']==targets and gap.position['stop']==p.stop
