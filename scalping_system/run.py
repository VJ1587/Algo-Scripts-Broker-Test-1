"""Backward-compatible launch names. Modern metadata required for adaptive runs."""
import sys
from scalping.cli import main as modern_main

def main(default=None):
    args=sys.argv[1:]
    if '--legacy' in args:
        import argparse, json
        from pathlib import Path
        from engine import Config, load_csv, signals, backtest
        from scalping.reporting import file_hash
        from scalping.contracts import stable_id
        import uuid
        parser=argparse.ArgumentParser()
        parser.add_argument('--legacy',action='store_true'); parser.add_argument('--data',required=True)
        parser.add_argument('--config',required=True); parser.add_argument('--strategy',default=default,required=default is None)
        parser.add_argument('--output',default='results'); parser.add_argument('--equity',type=float,default=10000)
        a=parser.parse_args(args); c=Config(**json.loads(Path(a.config).read_text()))
        d=load_csv(a.data); s=signals(d,c,a.strategy); t,e,m=backtest(d,s,c,a.equity)
        out=Path(a.output)/f'{a.strategy}_legacy_{stable_id(c)}_{file_hash(a.data)[:12]}_{uuid.uuid4().hex[:12]}'
        out.mkdir(parents=True,exist_ok=False)
        s.to_csv(out/'indicators.csv'); t.to_csv(out/'trades.csv',index=False); e.to_csv(out/'equity.csv',index=False)
        (out/'metrics.json').write_text(json.dumps(dict(m,mode='legacy_original'),indent=2))
        (out/'run_manifest.json').write_text(json.dumps(dict(mode='legacy_original',uncorrected_execution=True,data_hash=file_hash(a.data))))
        print(out); return 0
    return modern_main(['run',*args],default_strategy=default)

if __name__=='__main__':
    raise SystemExit(main())
