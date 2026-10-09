"""Offline chronological training selection with embargo and full candidate log."""
from itertools import product
from pathlib import Path
import json
import pandas as pd
from ..contracts import primitive, stable_id

def chronological_splits(data, train_end, validation_end, embargo_minutes=30):
    if embargo_minutes<30: raise ValueError('Embargo must cover pending + hold horizon (30 minutes)')
    a,b=pd.Timestamp(train_end),pd.Timestamp(validation_end)
    if not a<b: raise ValueError('Chronological boundaries required')
    delta=pd.Timedelta(minutes=embargo_minutes)
    return data.loc[data.index<=a],data.loc[(data.index>a+delta)&(data.index<=b)],data.loc[data.index>b+delta]

def calibrate(data, grid, evaluator, target_metric, train_end, validation_end, output, synthetic=False, embargo_minutes=30):
    if not target_metric or not grid: raise ValueError('Explicit metric and candidate grid required')
    train,validation,test=chronological_splits(data,train_end,validation_end,embargo_minutes)
    if any(not len(x) for x in (train,validation,test)): raise ValueError('Insufficient chronological data')
    attempts=[]
    for values in product(*(grid[k] for k in sorted(grid))):
        candidate=dict(zip(sorted(grid),values))
        metrics=evaluator(train.copy(),candidate)
        score=metrics.get(target_metric)
        if score is None or not pd.notna(score): raise ValueError('Undefined target metric')
        attempts.append(dict(candidate=candidate,training_metrics=metrics,score=score))
    best=max(attempts,key=lambda x:x['score'])['candidate']
    result=dict(config=best,attempts=attempts,validation=evaluator(validation.copy(),best),test=evaluator(test.copy(),best),
        target_metric=target_metric,embargo_minutes=embargo_minutes,synthetic=synthetic,
        status='engineering_fixture_performance_pending' if synthetic else 'held_out_research',config_id=stable_id(best))
    destination=Path(output); destination.mkdir(parents=True,exist_ok=False)
    (destination/'calibrated_config.json').write_text(json.dumps(primitive(best),indent=2))
    (destination/'comparison_manifest.json').write_text(json.dumps(primitive(result),indent=2))
    return result
