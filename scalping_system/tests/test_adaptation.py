from dataclasses import replace, FrozenInstanceError
import pytest
from scalping.adaptation.regime import update_regime, validate_config
from scalping.adaptation.policy import resolve_policy
from scalping.contracts import RegimeState, InstrumentProfile, freeze, StrategyState
from scalping.strategies.trend import Config

def feature(er=.5,slope=.1,v=1):
    return dict(er=er,slope=slope,volatility_ratio=v,spread=.01,spread_atr=.01,relative_activity=None,quote_age=0,quality_reasons=(),atr=1.,baseline=1.)

def test_three_bar_hysteresis_and_exit_order():
    s=RegimeState()
    for _ in range(2): s=update_regime(feature(),s); assert s.label=='transition'
    s=update_regime(feature(),s); assert s.label=='directional'
    s=update_regime(feature(er=.1,slope=.01),s); assert s.label=='directional'
    s=update_regime(feature(er=.1,slope=.01),s); assert s.label=='directional'
    s=update_regime(feature(er=.1,slope=.01),s); assert s.label=='transition'
    for _ in range(3): s=update_regime(feature(er=.1,slope=.01),s)
    assert s.label=='range'

@pytest.mark.parametrize('er,slope,label',[(.45,.05,'directional'),(.449,.05,'transition'),(.45,.049,'transition'),(.25,.025,'range'),(.251,.025,'transition'),(.25,.026,'transition')])
def test_entry_boundaries(er,slope,label):
    s=RegimeState()
    for _ in range(3): s=update_regime(feature(er,slope),s)
    assert s.label==label

@pytest.mark.parametrize('initial,er,slope,expected', [('directional',.35,.035,'directional'),('directional',.349,.04,'transition'),('directional',.4,.034,'transition'),('range',.35,.04,'range'),('range',.351,.03,'transition'),('range',.3,.041,'transition')])
def test_exit_boundaries(initial,er,slope,expected):
    s=RegimeState(label=initial,directional_sign=1)
    for _ in range(3): s=update_regime(feature(er,slope),s)
    assert s.label==expected

@pytest.mark.parametrize('v,flag',[(.749,'low'),(.75,'normal'),(1.5,'normal'),(1.501,'elevated'),(2.5,'elevated'),(2.501,'extreme')])
def test_volatility_boundaries(v,flag):
    s=update_regime(feature(v=v))
    assert s.volatility_flag==flag
    assert ('EXTREME_VOLATILITY' in s.quality_reasons)==(v>2.5)

def test_immediate_pause():
    for override in ({'quality_reasons':('MISSING_BAR',)},{'quote_age':61},{'spread_atr':.151}):
        state=update_regime(dict(feature(),**override),RegimeState(label='directional'))
        assert state.liquidity_flag=='paused' and state.label=='directional'
    assert not update_regime(dict(feature(),spread_atr=.15)).quality_reasons

def test_direction_sign_does_not_flip_before_hysteretic_exit():
    state=RegimeState(label='directional',directional_sign=1)
    for _ in range(2):
        state=update_regime(feature(slope=-.1),state)
        assert state.label=='directional' and state.directional_sign==1
    state=update_regime(feature(slope=-.1),state)
    assert state.label=='transition' and state.directional_sign==0

@pytest.mark.parametrize('atr,expected',[(.1,.5),(1,1),(1.8,1.8),(2.4,2)])
def test_one_scale_and_bounds(instrument,atr,expected):
    profile=InstrumentProfile('p',instrument.instrument_id,'1',None,(),freeze({}),True)
    f=dict(feature(),atr=atr,volatility_ratio=atr)
    p=resolve_policy('trend',f,profile,RegimeState(label='directional',directional_sign=1),Config(),instrument)
    assert p.effective_atr==pytest.approx(expected)
    assert p.distances['target1']==int(expected/.01)

def test_not_ready_and_extreme_precede_clipping(instrument):
    profile=InstrumentProfile('p',instrument.instrument_id,'1',None,(),freeze({}),False)
    p=resolve_policy('trend',feature(),profile,RegimeState(),Config(),instrument)
    assert not p.ready and 'NOT_READY_PROFILE' in p.reasons
    profile=replace(profile,ready=True)
    f=dict(feature(),atr=3,volatility_ratio=3)
    p=resolve_policy('trend',f,profile,update_regime(f),Config(),instrument)
    assert not p.ready and p.effective_atr==2

def test_fixed_atr_warmup_is_not_ready_without_decimal_nan(instrument):
    profile=InstrumentProfile('p',instrument.instrument_id,'1',None,(),freeze({}),False)
    f=dict(feature(),atr=float('nan'),baseline=None)
    p=resolve_policy('trend',f,profile,RegimeState(),Config(mode='fixed_atr'),instrument)
    assert not p.ready and p.effective_atr is None and not p.distances

def test_frozen_policy_and_state_roundtrip(make_context):
    p=make_context('far_ma').policy
    with pytest.raises(TypeError): p.distances['stop']=1
    with pytest.raises(FrozenInstanceError): p.hold_timeout=40
    s=StrategyState('far_ma',candidate=freeze({'side':1,'mean':101}),frozen_policy=p)
    assert s.to_json()==StrategyState.from_json(s.to_json()).to_json()

@pytest.mark.parametrize('bad',[{'range_enter_er':.6},{'scale_min':3},{'volatility_low':2},{'persistence':1.5},{'max_spread_ratio':float('nan')},{'bogus':1}])
def test_invalid_adaptation(bad):
    with pytest.raises(ValueError): validate_config(bad)
