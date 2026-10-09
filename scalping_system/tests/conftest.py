"""Deterministic, explicit bull/bear/near-miss fixtures; no performance claims."""
from pathlib import Path
from dataclasses import replace
import importlib
import pytest
import pandas as pd
import numpy as np
from scalping.instrument import load_instrument
from scalping.contracts import Context, freeze, InstrumentProfile, RegimeState
from scalping.adaptation.policy import resolve_policy

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def instrument():
    return load_instrument(ROOT/'configs/instruments/DEMO_STOCK.yaml')

def setup_bars(n=40):
    index=pd.date_range('2026-01-05T14:35Z',periods=n,freq='5min')
    d=pd.DataFrame(dict(open=100.,high=100.1,low=99.9,close=100.,volume=1000.,ema9=102.,ema15=103.,ema30=104.,ema65=105.,ema200=106.,
        rsi=50.,stoch=50.,vwap=np.nan,atr=1.,er=.5,slope=.1,adx=20.,spread=.01,session_id='2026-01-05',bucket=0,complete=True,missing_before=False),index=index)
    d['open_time']=index-pd.Timedelta(minutes=5); d['close_time']=index; d['available_at']=index
    d['session_start']=pd.Timestamp('2026-01-05T14:30Z'); d['session_end']=pd.Timestamp('2026-01-05T21:00Z')
    return d

def context(instrument,method,bars=None,execution=None,regime='range',config=None,time=None):
    module=importlib.import_module(f'scalping.strategies.{method}.strategy')
    config=config or module.Config(mode='fixed_atr')
    bars=setup_bars() if bars is None else bars
    time=time or bars.available_at.iloc[-1]
    if execution is None:
        execution=bars.iloc[-1:].copy(); execution.index=pd.DatetimeIndex([time])
    feature=dict(atr=1.,baseline=1.,er=.5,slope=.1,spread=.01,spread_atr=.01,quote_age=0,quality_reasons=(),
        volatility_ratio=1.,bid=float(bars.close.iloc[-1])-.005,ask=float(bars.close.iloc[-1])+.005,relative_activity=1.,cost_provenance='quote')
    profile=InstrumentProfile('fixture',instrument.instrument_id,'1.0',bars.session_start.iloc[0],tuple(str(i) for i in range(20)),freeze({'0':dict(atr=1,ready=True)}),True)
    r=RegimeState(label=regime,directional_sign=1 if regime=='directional' else 0,volatility_flag='normal')
    policy=resolve_policy(method,feature,profile,r,config,instrument)
    return Context(bars,execution,instrument,freeze(feature),policy,config,time,pd.Timestamp('2026-01-05T21:00Z'))

def later(ctx,minutes=1,opening=100,closing=100.1,high=None,low=None,setup=None):
    time=ctx.as_of+pd.Timedelta(minutes=minutes)
    e=ctx.execution.iloc[-1:].copy()
    e['open']=opening; e['close']=closing; e['high']=high if high is not None else max(opening,closing)+.01
    e['low']=low if low is not None else min(opening,closing)-.01
    e['available_at']=time; e['close_time']=time; e['open_time']=time-pd.Timedelta(minutes=1)
    e.index=pd.DatetimeIndex([time])
    return replace(ctx,execution=e,setup=ctx.setup if setup is None else setup,as_of=time,
        features=freeze(dict(ctx.features,bid=closing-.005,ask=closing+.005)))

def mirror(bars):
    d=bars.copy()
    for name in ('open','close','ema9','ema15','ema30','ema65','ema200','vwap'): d[name]=200-d[name]
    d['high']=200-bars.low; d['low']=200-bars.high
    d['rsi']=100-bars.rsi; d['stoch']=100-bars.stoch
    return d

@pytest.fixture
def make_context(instrument):
    return lambda method,**kwargs:context(instrument,method,**kwargs)
