import argparse,json
from pathlib import Path
from engine import Config,load_csv,signals,backtest

def main(default=None):
    a=argparse.ArgumentParser()
    a.add_argument('--data',required=True); a.add_argument('--config',required=True)
    a.add_argument('--strategy',default=default,choices=['trend','regular_ma','far_ma','base','double'],required=default is None)
    a.add_argument('--output',default='results'); a.add_argument('--equity',type=float,default=10000)
    args=a.parse_args(); c=Config(**json.loads(Path(args.config).read_text()))
    d=load_csv(args.data); s=signals(d,c,args.strategy)
    t,e,m=backtest(d,s,c,args.equity); out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    s.to_csv(out/'indicators_signals.csv'); t.to_csv(out/'trades.csv',index=False)
    e.to_csv(out/'equity.csv',index=False); (out/'metrics.json').write_text(json.dumps(m,indent=2))
    print(json.dumps(m,indent=2))
if __name__=='__main__': main()
