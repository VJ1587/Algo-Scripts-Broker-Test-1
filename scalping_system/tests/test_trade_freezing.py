from dataclasses import replace
from decimal import Decimal as D
import pandas as pd
from scalping.contracts import freeze
from scalping.execution.simulator import Simulator
from scalping.strategies.far_ma import Strategy
from conftest import setup_bars, later
from test_execution import proposal, simulator, bar, process

def test_volatility_after_fill_cannot_change_stop_targets_budget_profile(instrument):
    p=proposal(instrument); sim=simulator(instrument)
    process(sim,bar(p.decision_time),[p]); q=sim.position
    original=(q['stop'],q['targets'],q['planned_risk'],q['proposal'].policy.profile_id,q['proposal'].max_hold)
    other=replace(p,signal_id='new_signal',stop=D(90),targets=(D(110),D(120)),policy=replace(p.policy,profile_id='different',effective_atr=10))
    process(sim,bar(p.decision_time+pd.Timedelta(minutes=1)),[other])
    q=sim.position
    assert (q['stop'],q['targets'],q['planned_risk'],q['proposal'].policy.profile_id,q['proposal'].max_hold)==original
    restored=Simulator.from_json(sim.to_json(),instrument,sim.calendar)
    assert restored.to_json()==sim.to_json()

def test_armed_geometry_and_timeout_do_not_rescale(make_context):
    b=setup_bars(); b.loc[b.index[-1],['rsi','stoch']]=[20,19]
    ctx=make_context('far_ma',bars=b,regime='range'); strategy=Strategy(); _,s=strategy.evaluate(ctx)
    frozen=s.frozen_policy; stop=s.candidate['stop']
    ctx=later(ctx,opening=100,closing=100.1)
    ctx=replace(ctx,policy=replace(ctx.policy,effective_atr=2,distances=freeze({k:v*2 for k,v in ctx.policy.distances.items()}),profile_id='next'))
    d,_=strategy.evaluate(ctx,s)
    assert d.proposal.policy==frozen and float(d.proposal.stop)==stop and d.proposal.max_hold==frozen.hold_timeout
