"""Check that each MT5 product's generated rule values match the Python source config.

These are generation/contract checks, not MQL5 runtime signal-parity tests.
"""
from pathlib import Path
import importlib.util
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mt5_builder', ROOT / 'build_scripts.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
CFG = yaml.safe_load((ROOT.parent / 'scalp_config.yaml').read_text())
PRODUCTS = ('ES', 'NQ', 'CL', 'GC')


def test_profile_snapshot_matches_entire_source_config():
    profile = builder.profiles()
    assert profile['source_config_version'] == CFG['version']
    assert profile['methods'] == CFG['setups']
    for product in PRODUCTS:
        assert profile['products'][product]['tick'] == CFG['instruments'][product]['tick']


@pytest.mark.parametrize('method', tuple(builder.METHODS))
def test_generated_each_method_has_source_product_thresholds_and_targets(method):
    key, mapping = builder.METHODS[method]
    source = builder.futures_method(ROOT / 'templates' / 'scalp' / (method + '.mql'))
    assert len(re.findall(r'void\s+Scan\(', source)) == 1
    for param, field in mapping.items():
        args = ','.join(str(CFG['setups'][key][field][p]) for p in PRODUCTS)
        assert f'Resolved({param},{args})' in source
        assert f'input double {param}=0;' in source
    if key != 'range_break':
        target_field = 'momo_targets_ticks' if method == 'ORB_MOMO' else 'trend_targets_ticks' if method == 'ORB_Trend' else 'targets_ticks'
        values = CFG['setups'][key][target_field]
        for index in (0, 1):
            name = ('InpTarget1Ticks' if key == 'orb' else 'InpTargetTicks') if index == 0 else 'InpTarget2Ticks'
            args = ','.join(str(values[p][index] if len(values[p]) > index else 0) for p in PRODUCTS)
            assert f'Resolved({name},{args})' in source
    if method == 'ORB_Trend':
        assert 'InpProduct==PRODUCT_ES_MES ? 2.0*f.a[f.k]' in source


def test_profile_selection_is_required_and_tick_distances_match_source():
    source = builder.profile_helpers()
    assert 'InpProduct=PRODUCT_SELECT' in source
    assert 'if(InpProduct==PRODUCT_SELECT)' in source
    assert 'return false;' in source
    expected = ','.join(str(CFG['instruments'][p]['tick']) for p in PRODUCTS)
    assert f'Resolved(InpRuleTick,{expected})' in source
    assert 'InitializeProfile()' in builder.scalp_start()


def test_delivery_has_one_file_per_method_and_no_gc_only_outputs():
    files = list((ROOT / 'Scripts').glob('Futures_Scalp_*.mq5'))
    assert len(files) == 8
    assert not list((ROOT / 'Scripts').glob('Gold_Scalp_*'))
    for file in files:
        source = file.read_text()
        assert '#include' not in source
        assert len(re.findall(r'int\s+OnStart\(', source)) == 1
        assert not re.search(r'\bOrderSend(?:Async)?\s*\(', source)
