from dataclasses import replace
import importlib
import subprocess
import sys
from scalping.contracts import dumps, freeze
from scalping.strategy_support import load_config
from conftest import ROOT, setup_bars, later
import pytest

METHODS=('trend','regular_ma','far_ma','base','double')

def test_parameter_mutation_cannot_change_other_outputs(make_context):
    trend=setup_bars(); trend.loc[trend.index[-2],['ema9','ema15']]=[99.8,99.9]
    trend.loc[trend.index[-1],['ema9','ema15','ema30','close']]=[100,99.9,99.8,100.2]
    regular=setup_bars(); regular.loc[regular.index[-1],['close','ema15','ema30','ema65','ema200','stoch']]=[100,101,100.5,98.8,98.8,30]
    far=setup_bars(); far.loc[far.index[-1],['rsi','stoch']]=[20,19]
    base=setup_bars(8); base.loc[base.index[-1],['open','high','low','close']]=[100,100.4,99.9,100.3]
    double=setup_bars(12); double['low']=99.5; double['high']=100.5
    double.loc[double.index[2],'low']=99; double.loc[double.index[5],'high']=101
    double.loc[double.index[10],'low']=99.1; double.loc[double.index[11],['open','high','close']]=[100,101.5,101.3]
    fixtures=dict(trend=trend,regular_ma=regular,far_ma=far,base=base,double=double)
    contexts={m:make_context(m,bars=fixtures[m],regime='directional' if m=='trend' else 'range') for m in METHODS}
    strategies={m:importlib.import_module(f'scalping.strategies.{m}').Strategy() for m in METHODS}
    def result(method):
        d,s=strategies[method].evaluate(contexts[method])
        if not d.proposal:
            d,s=strategies[method].evaluate(later(contexts[method],opening=100,closing=100.1),s)
        assert d.proposal,method
        return dumps((d,s))
    before={m:result(m) for m in METHODS}
    altered=replace(contexts['trend'].config,coefficients=freeze(dict(contexts['trend'].config.coefficients,target1=1.25)))
    assert altered!=contexts['trend'].config
    for m in METHODS[1:]: assert result(m)==before[m]

@pytest.mark.parametrize('method',METHODS)
def test_import_one_strategy_requires_no_other_strategy(method):
    code=f"import sys; import scalping.strategies.{method}; assert all('scalping.strategies.'+m not in sys.modules for m in {METHODS!r} if m!={method!r})"
    subprocess.run([sys.executable,'-c',code],check=True)

@pytest.mark.parametrize('method',METHODS)
def test_all_config_files_valid_and_immutable(method):
    module=importlib.import_module(f'scalping.strategies.{method}')
    config=load_config(module.Config,ROOT/f'src/scalping/strategies/{method}/config.yaml')
    assert config.strategy_id==method
    with pytest.raises(TypeError): config.coefficients['stop']=2
    with pytest.raises(ValueError): replace(config,risk_fraction=.03)
    with pytest.raises(ValueError): replace(config,pyramiding=True)
