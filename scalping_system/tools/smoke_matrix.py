"""Run all 15 cross-asset adaptive examples and inspect their artifact schemas."""
from pathlib import Path
import subprocess
import sys
import json
import pandas as pd

root=Path(__file__).resolve().parents[1]
records=[]
for asset,symbol in [('stock','DEMO_STOCK'),('fx','DEMO_FX'),('future','DEMO_FUTURE')]:
    for strategy in ('trend','regular_ma','far_ma','base','double'):
        args=[sys.executable,'-m',f'scalping.strategies.{strategy}.cli','run','--data',f'examples/generated/{asset}_bars.csv',
            '--instrument',f'configs/instruments/{symbol}.yaml','--mode','adaptive','--synthetic','--output','results/smoke']
        result=subprocess.run(args,cwd=root,text=True,capture_output=True)
        if result.returncode:
            print(result.stderr); raise SystemExit(result.returncode)
        report=json.loads(result.stdout.strip().splitlines()[-1]); folder=root/report['output']
        manifest=json.loads((folder/'run_manifest.json').read_text())
        features=pd.read_csv(folder/'indicators.csv')
        assert {'atr','baseline','regime','effective_atr','volatility_flag','liquidity_flag','distance_stop_ticks'}<=set(features)
        assert manifest['synthetic'] and manifest['strategy']==strategy and manifest['profiles']
        rows=[json.loads(l) for l in (folder/'signals.jsonl').read_text().splitlines()]
        accepted=[r for r in rows if r['status']=='accepted']
        assert all(r['sizing']['accepted'] and r['proposal']['signal_id'] and r['proposal']['source_available_at'] for r in accepted)
        assert any(p['ready'] for p in manifest['profiles'])
        record=dict(strategy=strategy,asset=asset,accepted=len(accepted),trades=report['metrics']['trades'],output=report['output'],exit_code=0)
        records.append(record); print(json.dumps(record),flush=True)
(root/'docs/smoke_results.json').write_text(json.dumps(records,indent=2))
