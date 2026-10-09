from dataclasses import replace
import yaml
import pandas as pd
import pytest
from scalping.instrument import load_instrument, eligible
from scalping.data import load_bars
from conftest import ROOT

def test_real_template_fails_closed():
    with pytest.raises((ValueError,TypeError)): load_instrument(ROOT/'docs/REAL_INSTRUMENT_TEMPLATE.yaml')

def test_stock_eligibility_dated_and_synthetic_flag(instrument):
    t=pd.Timestamp('2026-01-05T15:00Z')
    assert eligible(instrument,t,True)[0]
    assert not eligible(instrument,t,False)[0]
    assert not eligible(instrument,pd.Timestamp('2027-01-05T15:00Z'),True)[0]

def test_empty_and_bad_bar_inputs(instrument,tmp_path):
    p=tmp_path/'empty.csv'; p.write_text('timestamp,open,high,low,close,volume\n')
    assert load_bars(p,instrument).empty
    d=pd.read_csv(ROOT/'examples/generated/stock_bars.csv').iloc[:2]
    d['timestamp']=['2026-01-05T14:30:00','2026-01-05T14:31:00']; d.to_csv(p,index=False)
    with pytest.raises(ValueError): load_bars(p,instrument)
    for values in ({'low':101,'high':99},{'volume':-1},{'open':float('inf')}):
        d=pd.read_csv(ROOT/'examples/generated/stock_bars.csv').iloc[:2]
        for k,v in values.items(): d[k]=v
        d.to_csv(p,index=False)
        with pytest.raises(ValueError): load_bars(p,instrument)

def test_negative_futures_allowed_not_stock(instrument,tmp_path):
    future=load_instrument(ROOT/'configs/instruments/DEMO_FUTURE.yaml')
    p=tmp_path/'negative.csv'
    pd.DataFrame([dict(timestamp='2026-01-05T14:30Z',open=-5,high=-4,low=-6,close=-5,volume=0)]).to_csv(p,index=False)
    assert load_bars(p,future).close.iloc[0]==-5
    with pytest.raises(ValueError): load_bars(p,instrument)
