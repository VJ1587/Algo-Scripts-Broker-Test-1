"""Research CLI with strict metadata and static, side-effect-free registry."""
import argparse
from dataclasses import replace
from pathlib import Path
import sys
import json
import math
import uuid
import pandas as pd
import yaml
from .strategies.trend import Strategy as Trend
from .strategies.regular_ma import Strategy as Regular
from .strategies.far_ma import Strategy as Far
from .strategies.base import Strategy as Base
from .strategies.double import Strategy as Double
from .contracts import Context, freeze, primitive, Conversion, StrategyState, stable_id
from .instrument import load_instrument, decimal, eligible
from .sessions import SessionCalendar
from .data import load_bars, closed_resample
from .indicators import compute_features_input
from .strategy_support import load_config
from .adaptation.profile import build_profile
from .adaptation.features import compute_behavior_features, last_quote
from .adaptation.regime import update_regime, validate_config, DEFAULTS
from .adaptation.policy import resolve_policy
from .execution.costs import estimate_costs
from .execution.simulator import Simulator
from .execution.sizing import size_order
from .contracts import AccountSnapshot
from .reporting import write_run, file_hash

REGISTRY={'trend':Trend,'regular_ma':Regular,'far_ma':Far,'base':Base,'double':Double}
ROOT=Path(__file__).resolve().parents[2]

def auxiliary(path):
    if not path: return None
    d=pd.read_csv(path)
    times=[pd.Timestamp(t) for t in d.pop('timestamp')]
    if any(t.tzinfo is None for t in times): raise ValueError('Aware auxiliary timestamps required')
    d.index=pd.DatetimeIndex(times).tz_convert('UTC')
    if d.index.has_duplicates or not d.index.is_monotonic_increasing: raise ValueError('Unsorted/duplicate auxiliary data')
    return d

def conversion_at(rates,instrument,timestamp):
    if rates is None: return None
    r=last_quote(rates,timestamp)
    if r is None: return None
    return Conversion(r.name,str(r.from_currency),str(r.to_currency),decimal(r.rate))

def run(args):
    instrument=load_instrument(args.instrument)
    if args.cost_multiplier!=1:
        if not math.isfinite(args.cost_multiplier) or args.cost_multiplier<1: raise ValueError('Cost multiplier must be finite and >=1')
        instrument=replace(instrument,cost_model=freeze({k:float(v)*args.cost_multiplier for k,v in instrument.cost_model.items()}))
    calendar=SessionCalendar.load(instrument.session_calendar_path)
    adaptation_path=ROOT/'configs/adaptation.yaml'
    adaptation=validate_config(yaml.safe_load(adaptation_path.read_text()))
    strategy=REGISTRY[args.strategy]()
    path=args.config or Path(__file__).parent/'strategies'/args.strategy/'config.yaml'
    config=load_config(strategy.Config,path,args.mode,adaptation)
    bars=load_bars(args.data,instrument)
    quotes=auxiliary(args.quotes or instrument.quotes_path)
    rates=auxiliary(args.conversions or instrument.conversion_path)
    if instrument.synthetic and not args.synthetic: raise ValueError('--synthetic required for synthetic instrument')
    if quotes is not None:
        if not {'bid','ask'}<=set(quotes) or not (quotes.ask>=quotes.bid).all(): raise ValueError('Invalid quote schema')
        if not all(math.isfinite(float(v)) for v in quotes[['bid','ask']].to_numpy().flat): raise ValueError('Nonfinite quote')
        if args.cost_multiplier!=1:
            mid=(quotes.ask+quotes.bid)/2; half=(quotes.ask-quotes.bid)/2*args.cost_multiplier
            quotes=quotes.copy(); quotes['bid']=mid-half; quotes['ask']=mid+half
    bars['spread']=float(instrument.cost_model['spread'])
    # Causal backward quote join supplies historical profile spreads, never future interpolation.
    if quotes is not None and len(bars):
        joined=pd.merge_asof(pd.DataFrame({'timestamp':bars.close_time}),quotes.reset_index(names='timestamp'),on='timestamp',direction='backward',tolerance=pd.Timedelta(seconds=adaptation['max_quote_age_seconds']))
        bars['spread']=(joined.ask-joined.bid).to_numpy()
    as_of=bars.available_at.max() if len(bars) else pd.Timestamp('1970-01-01',tz='UTC')
    setup=closed_resample(bars,config.setup_timeframe,as_of,calendar)
    expected_counts={s.session_id:sum(all(s.active(t) for t in pd.date_range(o,o+pd.Timedelta(config.setup_timeframe),freq='min',inclusive='left')) and o+pd.Timedelta(config.setup_timeframe)<=s.end
        for o in pd.date_range(s.start,s.end,freq=config.setup_timeframe,inclusive='left')) for s in calendar.sessions}
    setup['expected_session_bars']=setup.session_id.map(expected_counts)
    inputs=compute_features_input(setup,config,instrument.volume_kind)
    comparison_start=None
    if args.command=='compare':
        for session in calendar.sessions:
            if build_profile(inputs,instrument,session.start,adaptation).ready:
                comparison_start=session.start; break
        if comparison_start is None: raise ValueError('No common profile-ready comparison range')
    simulator=Simulator(instrument,calendar,args.equity,args.buying_power,args.approved_risk,not args.actionable,args.latency_seconds)
    state=StrategyState(args.strategy,config.version); regime=None; profile=None; session_id=None; last_setup=None
    decisions=[]; indicator_rows=[]; profiles=[]; pending_signals=[]; feature=None; policy=None; actionable_not_ready=False
    for k,(opening,bar) in enumerate(bars.iterrows()):
        if not bar.complete:
            simulator.cancel_pending('INCOMPLETE_EXECUTION_BAR')
            continue
        quote=last_quote(quotes,opening,bar.session_start)
        quote_ready=quotes is None or (quote is not None and (opening-quote.name).total_seconds()<=adaptation['max_quote_age_seconds'])
        if not quote_ready:
            simulator.cancel_pending('STALE_EXECUTION_QUOTE')
            # Existing positions still use explicitly labelled OHLC-estimated exits.
            quote=None
        costs=estimate_costs(instrument,opening,quote)
        conversion=conversion_at(rates,instrument,opening)
        ready=[s for s in pending_signals if s.source_available_at<=opening]
        pending_signals=[s for s in pending_signals if s.source_available_at>opening]
        if not quote_ready or bar.missing_before:
            simulator.cancel_pending('STALE_QUOTE_OR_MISSING_EXECUTION_DATA')
            ready=[]
        simulator.process(bar,ready,costs,conversion,quote)
        # Advance market-event time, not a possibly delayed feed timestamp.
        # Late bars become visible on a later clock event via as-of filtering.
        time=bar.close_time
        closed=inputs.loc[inputs.available_at<=time]
        execution=bars.iloc[:k+1].loc[lambda d:(d.available_at<=time)&d.complete]
        if not len(closed) or closed.session_id.iloc[-1]!=bar.session_id: continue
        if session_id!=bar.session_id:
            session_id=bar.session_id
            profile=build_profile(inputs,instrument,bar.session_start,adaptation)
            profiles.append(profile)
        new_setup=closed.index[-1]!=last_setup
        if new_setup:
            feature=compute_behavior_features(closed,profile,quotes,time)
            feature['conversion']=conversion_at(rates,instrument,time)
            regime=update_regime(feature,regime,adaptation)
            policy=resolve_policy(args.strategy,feature,profile,regime,config,instrument)
            row=dict(primitive(closed.iloc[-1].to_dict()),**primitive(feature),profile_id=profile.profile_id,regime=regime.label,
                volatility_flag=regime.volatility_flag,liquidity_flag=regime.liquidity_flag,effective_atr=policy.effective_atr,
                scale_raw=feature['atr']/feature['baseline'] if feature['baseline'] else None,
                **{f'distance_{n}_ticks':v for n,v in policy.distances.items()})
            indicator_rows.append(row); last_setup=closed.index[-1]
        elif state.phase not in ('ARMED','CONFIRMING'): continue
        # Quote/data quality is also checked during execution confirmation without re-evaluating regime hysteresis.
        current=dict(feature)
        q=last_quote(quotes,time,bar.session_start)
        if q is not None:
            current.update(bid=float(q.bid),ask=float(q.ask),spread=float(q.ask-q.bid),quote_age=(time-q.name).total_seconds())
        current['conversion']=conversion_at(rates,instrument,time)
        current['quality_reasons']=tuple(current.get('quality_reasons',()))+(('MISSING_BAR',) if bar.missing_before else ())
        instant=replace(regime,quality_reasons=tuple(regime.quality_reasons)+current['quality_reasons'])
        if current.get('quote_age') is not None and current['quote_age']>adaptation['max_quote_age_seconds']:
            instant=replace(instant,quality_reasons=instant.quality_reasons+('STALE_QUOTE',))
        current_policy=resolve_policy(args.strategy,current,profile,instant,config,instrument)
        if simulator.pending and (not current_policy.ready or simulator.pending.side not in current_policy.allowed_sides):
            simulator.cancel_pending('REGIME_OR_QUALITY_CANCELLED')
        current['order_live']=bool(simulator.pending or simulator.position)
        ctx=Context(closed,execution,instrument,freeze(current),current_policy,config,time,bar.session_end)
        if comparison_start is not None and time<comparison_start:
            continue
        decision,state=strategy.evaluate(ctx,state)
        allowed,reason=eligible(instrument,time,args.synthetic)
        if decision.proposal and not allowed:
            decision=replace(decision,status='rejected',proposal=None,reasons=(reason,))
        sizing=None
        if decision.proposal:
            account=AccountSnapshot(time,simulator.cash,instrument.account_currency,simulator.buying_power,simulator.approved_cash_risk)
            sizing=size_order(decision.proposal,account,instrument,estimate_costs(instrument,time,q),current['conversion'],not args.actionable)
            if not sizing.accepted: decision=replace(decision,status='rejected',proposal=None,reasons=sizing.reasons)
            else: pending_signals.append(decision.proposal)
        if args.actionable and (decision.status=='not_ready' or (decision.reasons and any('BUYING_POWER' in r or 'FX_CONVERSION' in r for r in decision.reasons))): actionable_not_ready=True
        decisions.append(dict(timestamp=str(time),session_id=session_id,regime=regime.label,status=decision.status,reasons=decision.reasons,
            proposal=decision.proposal,sizing=sizing,features=decision.features,actionable=bool(args.actionable and decision.proposal),research=not args.actionable,
            strategy_state=state.to_json()))
    hashes={name:file_hash(path) for name,path in [('calendar',instrument.session_calendar_path),('quotes',args.quotes or instrument.quotes_path),('conversion',args.conversions or instrument.conversion_path)] if path}
    destination,metrics=write_run(args.output,args.strategy,instrument,config,args.data,pd.DataFrame(indicator_rows),decisions,simulator,profiles,
        dict(auxiliary_hashes=hashes,synthetic=args.synthetic,actionable=args.actionable,state=json.loads(state.to_json()),python=sys.version,
             cost_multiplier=args.cost_multiplier,comparison_start=str(comparison_start) if comparison_start is not None else None))
    print(json.dumps(dict(output=str(destination),metrics={k:v for k,v in metrics.items() if k!='diagnostics_by_strategy_instrument_session_regime'})))
    if args.actionable and (actionable_not_ready or args.buying_power is None or not len(bars)):
        return 2
    if args.command=='compare':
        args.comparison_records.append(dict(mode=config.mode,cost_multiplier=args.cost_multiplier,output=str(destination),metrics=metrics,comparison_start=str(comparison_start)))
    return 0

def main(argv=None,default_strategy=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('run','compare','validate-config'):
        p=sub.add_parser(name)
        p.add_argument('--strategy',choices=REGISTRY,default=default_strategy,required=default_strategy is None)
        p.add_argument('--instrument',required=True); p.add_argument('--config')
        if name!='validate-config':
            p.add_argument('--data',required=True); p.add_argument('--output',default='results')
            p.add_argument('--mode',choices=('adaptive','fixed_atr','legacy_static'),default=None)
            p.add_argument('--synthetic',action='store_true'); p.add_argument('--actionable',action='store_true')
            p.add_argument('--equity',type=float,default=10000); p.add_argument('--buying-power',type=float)
            p.add_argument('--approved-risk',type=float); p.add_argument('--quotes'); p.add_argument('--conversions')
            p.add_argument('--latency-seconds',type=float,default=0)
            p.add_argument('--cost-multiplier',type=float,default=1)
    args=parser.parse_args(argv)
    try:
        if args.command=='validate-config':
            instrument=load_instrument(args.instrument); SessionCalendar.load(instrument.session_calendar_path)
            path=args.config or Path(__file__).parent/'strategies'/args.strategy/'config.yaml'
            config=load_config(REGISTRY[args.strategy].Config,path)
            print(json.dumps(dict(valid=True,strategy=config.strategy_id,instrument=instrument.instrument_id,synthetic=instrument.synthetic)))
            return 0
        if args.command=='compare':
            results=[]; args.comparison_records=[]
            for multiplier in (1.,2.):
                for mode in ('legacy_static','fixed_atr','adaptive'):
                    args.mode=mode; args.cost_multiplier=multiplier; results.append(run(args))
            folder=Path(args.output); folder.mkdir(parents=True,exist_ok=True)
            (folder/f'comparison_{uuid.uuid4().hex[:12]}.json').write_text(json.dumps(primitive(dict(records=args.comparison_records,
                synthetic=args.synthetic,performance_validation='pending',execution='identical modern simulator, common profile-ready range')),indent=2))
            return max(results)
        return run(args)
    except (ValueError,TypeError,KeyError,OSError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr); return 2

if __name__=='__main__':
    raise SystemExit(main())
