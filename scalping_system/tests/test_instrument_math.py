from dataclasses import replace
from decimal import Decimal as D
import pytest
import pandas as pd
from scalping.instrument import round_price, round_quantity, cash_per_price_unit, load_instrument
from scalping.contracts import Conversion, AssetClass, AccountSnapshot
from scalping.execution.costs import estimate_costs
from scalping.execution.sizing import size_order
from conftest import ROOT, setup_bars, context
from scalping.strategies.trend import Strategy

def proposal(instrument):
    b=setup_bars(); b.loc[b.index[-2],['ema9','ema15']]=[99.8,99.9]
    b.loc[b.index[-1],['ema9','ema15','ema30','close']]=[100.,99.9,99.8,100.2]
    return Strategy().evaluate(context(instrument,'trend',bars=b,regime='directional'))[0].proposal

@pytest.mark.parametrize('price,up,down',[('100.005','100.01','100.00'),('-3.11','-3.00','-3.25')])
def test_directional_tick_rounding(price,up,down):
    tick='.01' if price[0]!='-' else '.25'
    assert round_price(price,tick,'up')==D(up)
    assert round_price(price,tick,'down')==D(down)
    assert round_quantity(5999,1000)==5000

def test_fx_pip_tick_lot_and_conversion():
    fx=load_instrument(ROOT/'configs/instruments/DEMO_FX.yaml')
    assert fx.pip_size/fx.tick_size==10
    t=pd.Timestamp('2026-01-05T15:00Z')
    conversion=Conversion(t,'USD','EUR',D('.9'))
    assert cash_per_price_unit(fx,conversion,t)==D('.9')
    assert cash_per_price_unit(replace(fx,quantity_unit='lots'),conversion,t)==D('90000')
    for bad in (None,replace(conversion,timestamp=t-pd.Timedelta(seconds=301)),replace(conversion,timestamp=t+pd.Timedelta(seconds=1)),replace(conversion,from_currency='JPY')):
        with pytest.raises(ValueError): cash_per_price_unit(fx,bad,t)
    future=load_instrument(ROOT/'configs/instruments/DEMO_FUTURE.yaml')
    assert cash_per_price_unit(future,None)*future.tick_size==D('12.50')

def test_wider_stop_never_increases_quantity(instrument):
    p=proposal(instrument); t=p.decision_time
    account=AccountSnapshot(t,D(10000),'USD',D(100000))
    costs=estimate_costs(instrument,t)
    a=size_order(p,account,instrument,costs)
    b=size_order(replace(p,stop=p.stop-D(1)),account,instrument,costs)
    assert a.accepted and b.accepted and b.quantity<=a.quantity
    assert a.planned_cash_risk<=50
    reduced=size_order(p,replace(account,approved_cash_risk=D(10)),instrument,costs)
    assert reduced.planned_cash_risk<=10

def test_minimum_size_buying_power_and_partial_feasibility(instrument):
    p=proposal(instrument); costs=estimate_costs(instrument,p.decision_time)
    account=AccountSnapshot(p.decision_time,D(10),'USD',D(100000))
    assert not size_order(p,account,instrument,costs).accepted
    account=replace(account,equity=D(10000),available_buying_power=None)
    assert 'MISSING_BUYING_POWER' in size_order(p,account,instrument,costs).reasons
    assert size_order(p,account,instrument,costs,research=True).accepted
    assert not size_order(p,replace(account,available_buying_power=D(100)),instrument,costs).accepted
    assert not size_order(p,replace(account,account_currency='JPY'),instrument,costs,research=True).accepted
