"""Deterministic engineering fixture, never market performance evidence."""
import argparse
from pathlib import Path
import numpy as np
import pandas as pd
import yaml

def generate(output,seed=42):
    out=Path(output); out.mkdir(parents=True,exist_ok=True)
    root=Path(__file__).resolve().parents[1]
    (root/'configs/instruments').mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(seed)
    sessions=[]
    for day in pd.bdate_range('2026-01-05',periods=26):
        start=pd.Timestamp(str(day.date())+'T14:30:00Z')
        sessions.append(dict(id=str(day.date()),start=start.isoformat(),end=(start+pd.Timedelta(minutes=390)).isoformat(),breaks=[]))
    calendar=dict(timezone='America/New_York',synthetic=True,sessions=sessions)
    (out/'sessions.yaml').write_text(yaml.safe_dump(calendar,sort_keys=False))
    (root/'configs/sessions').mkdir(parents=True,exist_ok=True)
    (root/'configs/sessions/synthetic.yaml').write_text(yaml.safe_dump(calendar,sort_keys=False))
    manifest=dict(synthetic=True,as_of='2026-01-01',large_cap_min_usd=10_000_000_000,mega_cap_min_usd=200_000_000_000,
        manual_allowlist=['DEMO_STOCK'],members=[dict(symbol='DEMO_STOCK',**{'from':'2026-01-01','through':'2026-12-31'},verified=True,blue_chip=True,market_cap_usd=300_000_000_000)])
    (root/'configs/instruments/synthetic_eligibility.yaml').write_text(yaml.safe_dump(manifest))
    configs=[]
    for asset,symbol,price,tick,multiplier,step,noise in [('stock','DEMO_STOCK',100,.01,1,1,.10),('fx','DEMO_FX',1.1,.00001,1,1000,.00012),('future','DEMO_FUTURE',5000,.25,50,1,1.5)]:
        rows=[]; quotes=[]; rate=[]; previous=price
        for day,s in enumerate(sessions):
            opening=pd.Timestamp(s['start'])
            for minute in range(390):
                ts=opening+pd.Timedelta(minutes=minute)
                factor=(.6,1,1.8,1)[(minute//100)%4]
                drift=(.18 if minute<120 else -.12 if minute<240 else 0)*noise
                change=rng.normal(drift,noise*factor)
                close=previous+change
                wick=abs(rng.normal(noise*.35,noise*.1))
                volume=int(1000*(1+minute/390)+rng.integers(0,500))
                rows.append(dict(timestamp=ts.isoformat(),open=previous,high=max(previous,close)+wick,low=min(previous,close)-wick,close=close,volume=volume))
                spread=tick
                quotes.append(dict(timestamp=ts.isoformat(),bid=previous-spread/2,ask=previous+spread/2))
                rate.append(dict(timestamp=ts.isoformat(),from_currency='USD',to_currency='EUR',rate=.9))
                previous=close
        pd.DataFrame(rows).to_csv(out/f'{asset}_bars.csv',index=False)
        pd.DataFrame(quotes).to_csv(out/f'{asset}_quotes.csv',index=False)
        pd.DataFrame(rate).to_csv(out/f'{asset}_conversion.csv',index=False)
        try: relative=out.resolve().relative_to(root).as_posix()
        except ValueError: relative=out.resolve().as_posix()
        spec=dict(symbol=symbol,venue='SYNTHETIC',asset_class=asset,tick_size=tick,quantity_step=step,min_quantity=step,
            max_quantity=1000000 if asset=='fx' else 1000 if asset=='stock' else 20,multiplier=multiplier,
            pnl_currency='USD',account_currency='EUR' if asset=='fx' else 'USD',timezone='America/New_York',
            session_calendar_path='configs/sessions/synthetic.yaml',price_basis='mid',volume_kind='tick' if asset=='fx' else 'traded',
            short_allowed=True,supported_orders=['market','limit','stop'],metadata_as_of='2026-01-01',synthetic=True,
            settlement='synthetic engineering cash settlement',cost_model=dict(spread=tick,slippage_ticks=.1,fee_per_unit_per_side=0),
            margin_per_unit=15000 if asset=='future' else None,lot_size=100000 if asset=='fx' else None,pip_size=.0001 if asset=='fx' else None,
            quantity_unit='base_units' if asset=='fx' else 'contracts' if asset=='future' else 'shares',
            expiry='2026-12-31' if asset=='future' else None,roll_policy='actual single synthetic contract; no roll' if asset=='future' else None,
            eligibility_manifest_path='configs/instruments/synthetic_eligibility.yaml' if asset=='stock' else None,
            corporate_action_basis='unadjusted synthetic; no actions' if asset=='stock' else None,
            quotes_path=f'{relative}/{asset}_quotes.csv',conversion_path=f'{relative}/{asset}_conversion.csv' if asset=='fx' else None)
        (root/f'configs/instruments/{symbol}.yaml').write_text(yaml.safe_dump(spec,sort_keys=False))
        exported=dict(spec)
        for key in ('session_calendar_path','eligibility_manifest_path','quotes_path','conversion_path'):
            if exported.get(key): exported[key]=str((root/exported[key]).resolve())
        (out/f'{symbol}.yaml').write_text(yaml.safe_dump(exported,sort_keys=False))
        configs.append(symbol)
    print(f'Generated 26 synthetic full sessions for {configs}; seed={seed}; not performance evidence.')

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',default='examples/generated'); parser.add_argument('--seed',type=int,default=42)
    args=parser.parse_args(); generate(args.output,args.seed)
