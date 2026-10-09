import json
import subprocess
import sys
from pathlib import Path
import pandas as pd
import pytest
from conftest import ROOT

def test_cli_outputs_and_no_trade_schema(tmp_path):
    short=tmp_path/'short.csv'
    pd.read_csv(ROOT/'examples/generated/stock_bars.csv').iloc[:20].to_csv(short,index=False)
    args=[sys.executable,'-m','scalping.cli','run','--strategy','trend','--data',str(short),'--instrument',str(ROOT/'configs/instruments/DEMO_STOCK.yaml'),'--mode','adaptive','--synthetic','--output',str(tmp_path/'results')]
    subprocess.run(args,cwd=ROOT,check=True,capture_output=True,text=True)
    folder=next((tmp_path/'results').iterdir())
    assert {p.name for p in folder.iterdir()}=={'indicators.csv','signals.jsonl','trades.csv','equity.csv','metrics.json','run_manifest.json'}
    manifest=json.loads((folder/'run_manifest.json').read_text()); assert manifest['synthetic'] and manifest['data_hash']
    assert json.loads((folder/'metrics.json').read_text())['no_trade']
    decisions=[json.loads(l) for l in (folder/'signals.jsonl').read_text().splitlines()]
    assert all(d['status']=='not_ready' for d in decisions)
    subprocess.run(args,cwd=ROOT,check=True,capture_output=True,text=True)
    assert len(list((tmp_path/'results').iterdir()))==2
    assert subprocess.run(args+['--actionable'],cwd=ROOT,capture_output=True).returncode==2

def test_strict_unknown_config(tmp_path):
    from scalping.strategies.trend import Config
    from scalping.strategy_support import load_config
    path=tmp_path/'bad.yaml'; path.write_text('bogus: 1\n')
    with pytest.raises(ValueError): load_config(Config,path)
