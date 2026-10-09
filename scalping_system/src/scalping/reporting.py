"""Unique, reproducible output bundles, including decisions that did not trade."""
from pathlib import Path
from collections import Counter
import hashlib
import json
import uuid
import pandas as pd
from .contracts import primitive, stable_id, dumps

def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write_run(output,strategy,instrument,config,data_path,indicators,decisions,simulator,profiles,extra=None):
    config_hash=stable_id(config); data_hash=file_hash(data_path); run_id=uuid.uuid4().hex[:12]
    destination=Path(output)/f'{strategy}_{instrument.symbol}_{config_hash}_{data_hash[:12]}_{run_id}'
    destination.mkdir(parents=True,exist_ok=False)
    for name in ('timestamp','atr','baseline','regime','volatility_flag','liquidity_flag','effective_atr','distance_stop_ticks'):
        if name not in indicators: indicators[name]=pd.Series(dtype='object')
    indicators.to_csv(destination/'indicators.csv',index=False)
    with (destination/'signals.jsonl').open('w',encoding='utf-8') as stream:
        for row in decisions: stream.write(dumps(row)+'\n')
    trades,curve,metrics=simulator.finish()
    pd.DataFrame(trades,columns=['signal_id','entry_time','exit_time','side','entry','quantity','pnl','reason','completed','planned_risk','holding_minutes','regime','session','strategy','instrument']).to_csv(destination/'trades.csv',index=False)
    pd.DataFrame(curve).to_csv(destination/'equity.csv',index=False)
    reasons=Counter(reason for d in decisions for reason in d.get('reasons',()))
    metrics['rejected_signal_rate']=sum(d['status']!='accepted' for d in decisions)/len(decisions) if decisions else 0
    metrics['rejection_reasons']=dict(reasons)
    groups={}
    for d in decisions:
        key='|'.join((strategy,instrument.symbol,str(d.get('session_id')),str(d.get('regime'))))
        row=groups.setdefault(key,dict(decisions=0,accepted=0,trades=0,pnl=0.))
        row['decisions']+=1; row['accepted']+=d['status']=='accepted'
    for t in trades:
        key='|'.join((strategy,instrument.symbol,str(t['session']),t['regime']))
        row=groups.setdefault(key,dict(decisions=0,accepted=0,trades=0,pnl=0.))
        if t['completed']: row['trades']+=1; row['pnl']+=t['pnl']
    metrics['diagnostics_by_strategy_instrument_session_regime']=groups
    (destination/'metrics.json').write_text(json.dumps(primitive(metrics),indent=2,allow_nan=False))
    manifest=dict(schema_version=1,run_id=run_id,strategy=strategy,instrument=instrument,config=config,config_hash=config_hash,
        data_hash=data_hash,data_path=str(Path(data_path).resolve()),profiles=profiles,execution_diagnostics=simulator.diagnostics,
        limitations=['OHLC path and queue priority unknown','synthetic results are not performance evidence','no broker adapter','no aggregate account allocation'],**(extra or {}))
    (destination/'run_manifest.json').write_text(json.dumps(primitive(manifest),indent=2,allow_nan=False))
    return destination,metrics
