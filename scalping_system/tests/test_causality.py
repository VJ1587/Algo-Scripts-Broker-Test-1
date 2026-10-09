from pathlib import Path
from dataclasses import replace
import numpy as np
import pandas as pd
import pytest
from scalping.indicators import compute_features_input, wilder, atr, rsi, stochastic, vwap, adx
from scalping.data import load_bars, closed_resample
from scalping.sessions import SessionCalendar, Session
from scalping.adaptation.profile import build_profile
from scalping.adaptation.regime import DEFAULTS
from scalping.contracts import dumps
from conftest import ROOT, setup_bars

def test_independent_indicator_seed_and_reference():
    s=pd.Series([1.,2.,3.,4.,5.])
    assert wilder(s,3).tolist()[2:]==pytest.approx([2.,8/3,31/9])
    b=setup_bars(40)
    b['open']=100.; b['high']=102.; b['low']=98.; b['close']=100.
    assert atr(b).iloc[13]==4
    assert (atr(b).iloc[13:]==4).all()
    assert rsi(b.close).iloc[14]==50
    assert rsi(pd.Series(np.arange(30,dtype=float))).iloc[14]==100
    k,d=stochastic(b)
    assert k.iloc[15]==50 and d.iloc[17]==50
    assert adx(b).dropna().eq(0).all() and len(adx(b).dropna())>0
    assert compute_features_input(b).ema9.iloc[8]==100
    mixed=pd.Series([1.,2.,3.,2.,4.])
    assert rsi(mixed,3).iloc[3]==pytest.approx(200/3)
    assert rsi(mixed,3).iloc[4]==pytest.approx(250/3)
    directional=setup_bars(40)
    directional['close']=np.arange(40,dtype=float)+100
    directional['high']=directional.close+1; directional['low']=directional.close-1
    assert adx(directional).iloc[27]==pytest.approx(100)
    assert stochastic(directional)[0].iloc[15]==pytest.approx(100*14/15)
    independent=100.
    for price in directional.close.iloc[1:9]: independent=(price*.2+independent*.8)
    assert compute_features_input(directional).ema9.iloc[8]==pytest.approx(independent)
    b['close']=101.; b['high']=102.; b['low']=99.
    assert vwap(b).iloc[-1]==pytest.approx((102+99+101)/3)
    b['volume']=0.; assert vwap(b).isna().all()
    b['volume']=np.nan; assert vwap(b).isna().all()
    b['volume']=100.; assert vwap(b,'tick').isna().all()
    b['high']=100.; b['low']=100.; b['close']=100.
    assert stochastic(b)[0].dropna().eq(50).all()

def test_feature_prefix_and_future_price_volume_perturbations():
    b=setup_bars(260)
    b['close']=100+np.sin(np.arange(260)/10)
    b['high']=b.close+.2; b['low']=b.close-.2; b['open']=b.close
    full=compute_features_input(b); prefix=compute_features_input(b.iloc[:210])
    pd.testing.assert_frame_equal(prefix,full.iloc[:210])
    future=b.copy(); future.iloc[210:,future.columns.get_loc('close')]+=5
    future.iloc[210:,future.columns.get_loc('volume')]*=10
    pd.testing.assert_frame_equal(full.iloc[:210],compute_features_input(future).iloc[:210])

def test_profile_excludes_current_and_future_sessions(instrument):
    cal=SessionCalendar.load(instrument.session_calendar_path)
    bars=load_bars(ROOT/'examples/generated/stock_bars.csv',instrument)
    bars['spread']=.01
    setup=closed_resample(bars,'5min',bars.available_at.max(),cal)
    setup['expected_session_bars']=78
    features=compute_features_input(setup)
    cutoff=cal.sessions[23].start
    p=build_profile(features,instrument,cutoff,DEFAULTS)
    assert p.ready and len(p.completed_sessions)==20
    assert cal.sessions[23].session_id not in p.completed_sessions
    assert all(next(s for s in cal.sessions if s.session_id==sid).end<=cutoff for sid in p.completed_sessions)
    prefix=features.loc[features.available_at<cutoff]
    assert dumps(build_profile(prefix,instrument,cutoff,DEFAULTS))==dumps(p)
    mutated=features.copy(); mutated.loc[mutated.available_at>=cutoff,['atr','spread','volume']]=10000
    assert dumps(build_profile(mutated,instrument,cutoff,DEFAULTS))==dumps(p)
    assert p.buckets['0']['atr_count']==20

def test_right_edge_availability_and_missing_components(instrument,tmp_path):
    frame=pd.DataFrame(dict(timestamp=pd.date_range('2026-01-05T14:30Z',periods=7,freq='min').astype(str),open=100.,high=101.,low=99.,close=100.,volume=100))
    path=tmp_path/'bars.csv'; frame.to_csv(path,index=False)
    bars=load_bars(path,instrument); cal=SessionCalendar.load(instrument.session_calendar_path)
    assert len(closed_resample(bars,'5min',pd.Timestamp('2026-01-05T14:34:59Z'),cal))==0
    result=closed_resample(bars,'5min',pd.Timestamp('2026-01-05T14:35Z'),cal)
    assert len(result)==1 and result.index[0]==pd.Timestamp('2026-01-05T14:35Z')
    assert result.available_at.iloc[0]==result.index[0]
    missing=bars.drop(bars.index[2]); assert not len(closed_resample(missing,'5min',bars.available_at.max(),cal))
    incomplete=bars.copy(); incomplete.loc[incomplete.index[4],'complete']=False
    assert not len(closed_resample(incomplete,'5min',bars.available_at.max(),cal))

def test_delayed_availability_not_earlier_than_close(instrument,tmp_path):
    frame=pd.read_csv(ROOT/'examples/generated/stock_bars.csv').iloc[:5]
    frame['available_at']=pd.to_datetime(frame.timestamp,utc=True)+pd.Timedelta(minutes=2)
    path=tmp_path/'bars.csv'; frame.to_csv(path,index=False)
    bars=load_bars(path,instrument); cal=SessionCalendar.load(instrument.session_calendar_path)
    assert not len(closed_resample(bars,'5min',pd.Timestamp('2026-01-05T14:35Z'),cal))
    assert len(closed_resample(bars,'5min',pd.Timestamp('2026-01-05T14:36Z'),cal))==1
    frame['available_at']=frame.timestamp; frame.to_csv(path,index=False)
    with pytest.raises(ValueError): load_bars(path,instrument)
