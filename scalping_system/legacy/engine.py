"""Closed-bar indicators and independent scalping signals. No broker orders."""
from dataclasses import dataclass
import math
import numpy as np
import pandas as pd

@dataclass
class Config:
    tick_size: float = .01
    distance_unit: float = 1.0  # price units per sheet dollar; FX can use a pip
    point_value: float = 1.0  # account currency per 1.0 price move per quantity
    quantity_step: float = 1.0
    max_quantity: float = 1000
    risk_fraction: float = .005
    spread: float = 0.0
    slippage_ticks: float = 1.0
    commission: float = 0.0  # per quantity, per side
    timezone: str = 'America/New_York'
    session_start: str = '09:30'
    session_end: str = '16:00'
    base_minutes: int = 30
    base_width: float = .50
    pattern_minutes: int = 30
    pattern_tolerance: float = .50
    max_hold_minutes: int = 15
    rsi_period: int = 14
    stoch_period: int = 14
    stoch_smooth: int = 3
    def __post_init__(self):
        for name in ('tick_size','distance_unit','point_value','quantity_step','max_quantity'):
            if not math.isfinite(getattr(self,name)) or getattr(self,name) <= 0:
                raise ValueError(name + ' must be positive and finite')
        if not 0 < self.risk_fraction <= .02:
            raise ValueError('risk_fraction must be in (0, .02]')
        if min(self.spread,self.slippage_ticks,self.commission) < 0:
            raise ValueError('Costs cannot be negative')


def load_csv(path):
    d = pd.read_csv(path)
    d.columns = d.columns.str.lower()
    d['timestamp'] = pd.to_datetime(d['timestamp'], utc=True)
    d = d.set_index('timestamp')
    validate(d)
    return d


def validate(d):
    if not isinstance(d.index,pd.DatetimeIndex) or d.index.tz is None:
        raise ValueError('Timezone-aware DatetimeIndex required')
    if not d.index.is_monotonic_increasing or d.index.has_duplicates:
        raise ValueError('Bars must be sorted and unique')
    if not {'open','high','low','close','volume'} <= set(d.columns):
        raise ValueError('OHLCV required')
    a=d[['open','high','low','close','volume']]
    if not np.isfinite(a.to_numpy()).all() or (a.volume<0).any():
        raise ValueError('Invalid OHLCV values')
    if ((d.high < d[['open','close','low']].max(axis=1)) | (d.low > d[['open','close','high']].min(axis=1))).any():
        raise ValueError('Invalid candle bounds')
    if len(d)>1 and (d.index.to_series().diff().dropna()!=pd.Timedelta(minutes=1)).any():
        # Overnight/session gaps are allowed; missing in-session bars are not.
        same=d.index.to_series().dt.date.eq(d.index.to_series().shift().dt.date)
        gaps=d.index.to_series().diff()>pd.Timedelta(minutes=1)
        if (same & gaps).any():
            raise ValueError('Missing intraday bars: supply complete one-minute sessions')


def indicators(d,c):
    x=d.copy()
    for n in (9,15,30,65,200):
        x[f'ema{n}']=x.close.ewm(span=n,adjust=False,min_periods=n).mean()
    delta=x.close.diff(); up=delta.clip(lower=0); down=-delta.clip(upper=0)
    gain=up.ewm(alpha=1/c.rsi_period,adjust=False,min_periods=c.rsi_period).mean()
    loss=down.ewm(alpha=1/c.rsi_period,adjust=False,min_periods=c.rsi_period).mean()
    x['rsi']=100-100/(1+gain/loss.replace(0,np.nan))
    x.loc[(loss==0)&(gain>0),'rsi']=100
    x.loc[(loss==0)&(gain==0),'rsi']=50
    lo=x.low.rolling(c.stoch_period).min(); hi=x.high.rolling(c.stoch_period).max()
    x['stoch']=(100*(x.close-lo)/(hi-lo).replace(0,np.nan)).rolling(c.stoch_smooth).mean()
    dates=x.index.tz_convert(c.timezone).date
    pv=((x.high+x.low+x.close)/3*x.volume).groupby(dates).cumsum()
    vv=x.volume.groupby(dates).cumsum()
    x['vwap']=pv/vv.replace(0,np.nan)
    return x


def signals(d,c,method):
    validate(d)
    if method not in ('trend','regular_ma','far_ma','base','double'):
        raise ValueError('Unknown method')
    # Input timestamps denote bar OPEN. Resampled labels denote bar CLOSE.
    bars=d.resample('5min',label='right',closed='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    counts=d.close.resample('5min',label='right',closed='left').count()
    bars=bars.loc[counts==5]
    x=indicators(bars,c); u=c.distance_unit
    x['side']=0; x['limit']=np.nan; x['stop']=np.nan
    x['target1']=np.nan; x['target2']=np.nan
    for i in range(200,len(x)):
        r=x.iloc[i]; prev=x.iloc[i-1]; side=0; entry=stop=t1=t2=np.nan
        if method=='trend':
            long=r.ema9>r.ema15>r.ema30 and prev.ema9<=prev.ema15
            short=r.ema9<r.ema15<r.ema30 and prev.ema9>=prev.ema15
            # Crossing the 9/15 with a full ordered stack defines a new trend.
            side=1 if long else -1 if short else 0
            if side and max(r.ema9,r.ema15,r.ema30)-min(r.ema9,r.ema15,r.ema30)<=.5*u:
                entry=r.ema9; stop=r.ema30-side*.5*u; t1=entry+side*u; t2=entry+side*2*u
            else: side=0
        elif method=='regular_ma':
            for s in (1,-1):
                levels=[r.ema65,r.ema200,r.vwap]
                valid=[v for v in levels if np.isfinite(v) and s*(v-r.close)>=u]
                if valid and (r.stoch<50 if s==1 else r.stoch>50):
                    side=s; entry=min(valid,key=lambda v:abs(v-r.close)); stop=entry-s*.5*u
                    t1=entry+s*.5*u; t2=entry+s*1.5*u; break
        elif method=='far_ma':
            for s in (1,-1):
                extreme=(r.rsi<=20 and r.stoch<20) if s==1 else (r.rsi>=80 and r.stoch>80)
                if s*(r.ema65-r.close)>=1.5*u and extreme:
                    side=s; entry=r.close; stop=(r.low-.5*u if s==1 else r.high+.5*u)
                    t1=entry+s*.5*u; t2=entry+s*1.5*u; break
        elif method=='base':
            n=max(1,math.ceil(c.base_minutes/5)); hist=x.iloc[i-n:i]
            if len(hist)==n and hist.high.max()-hist.low.min()<=c.base_width*u:
                if r.close>hist.high.max(): side=1; entry=r.close; stop=hist.low.min()-.5*u
                elif r.close<hist.low.min(): side=-1; entry=r.close; stop=hist.high.max()+.5*u
                if side: t1=entry+side*.5*u; t2=entry+side*1.5*u
        else:
            # Confirm second pivot only once its right-hand candle has closed.
            j=i-1
            for s in (1,-1):
                col='low' if s==1 else 'high'
                pivot=x.iloc[j][col]
                if not (pivot<x.iloc[j-1][col] and pivot<r[col] if s==1 else pivot>x.iloc[j-1][col] and pivot>r[col]): continue
                for k in range(j-math.ceil(c.pattern_minutes/5),1,-1):
                    if k<1: break
                    first=x.iloc[k][col]
                    is_pivot=(first<x.iloc[k-1][col] and first<x.iloc[k+1][col]) if s==1 else (first>x.iloc[k-1][col] and first>x.iloc[k+1][col])
                    middle=x.iloc[k+1:j]
                    if is_pivot and abs(first-pivot)<=c.pattern_tolerance*u and len(middle) and ((middle.high.max()-first>=u) if s==1 else (first-middle.low.min()>=u)):
                        side=s; entry=pivot; stop=pivot-s*.5*u; t1=entry+s*.5*u; t2=entry+s*1.5*u; break
                if side: break
        if side and side*(entry-stop)>0:
            # Quantize order prices to valid ticks.
            vals=[round(v/c.tick_size)*c.tick_size for v in (entry,stop,t1,t2)]
            if side*(vals[0]-vals[1])>0:
                x.loc[x.index[i],['side','limit','stop','target1','target2']]=[side,*vals]
    return x


def backtest(d,sig,c,equity=10000):
    """One position; next-bar fills; stop-first ambiguity; fixed stops; no pyramids."""
    cash=equity; position=None; pending=None; trades=[]; curve=[]
    slip=c.slippage_ticks*c.tick_size+c.spread/2
    for ts,r in d.iterrows():
        local=ts.tz_convert(c.timezone); clock=local.strftime('%H:%M')
        in_session=c.session_start<=clock<c.session_end
        if not in_session: pending=None
        if pending and ts>pending['expires']: pending=None
        if not position and pending and in_session:
            q=pending; side=q['side']; level=q['limit']
            touched=r.low<=level if side==1 else r.high>=level
            if touched:
                raw=min(r.open,level) if side==1 else max(r.open,level)
                entry=raw+side*slip
                risk=side*(entry-q['stop'])
                if risk>0 and side*(q['target1']-entry)>0:
                    unit_risk=(risk+slip)*c.point_value+2*c.commission
                    qty=math.floor(min(cash*c.risk_fraction/unit_risk,c.max_quantity)/c.quantity_step)*c.quantity_step
                    if qty>=2*c.quantity_step:
                        cash-=qty*c.commission
                        position={**q,'entry':entry,'qty':qty,'initial_qty':qty,'entered':ts,'pnl':-qty*c.commission,'partial':False}
                pending=None
        if position:
            q=position; side=q['side']; exit_price=None; reason=None
            stopped=r.low<=q['stop'] if side==1 else r.high>=q['stop']
            timeout=ts-q['entered']>=pd.Timedelta(minutes=c.max_hold_minutes)
            last_session=(local+pd.Timedelta(minutes=1)).strftime('%H:%M')>=c.session_end
            if stopped:
                exit_price=(min(r.open,q['stop']) if side==1 else max(r.open,q['stop']))-side*slip; reason='stop'
            elif timeout or last_session or ts==d.index[-1]:
                exit_price=r.close-side*slip; reason='time_or_session'
            else:
                for key in ('target1','target2'):
                    if key=='target1' and q['partial']: continue
                    hit=r.high>=q[key] if side==1 else r.low<=q[key]
                    if hit:
                        qty=math.floor(q['initial_qty']/2/c.quantity_step)*c.quantity_step if key=='target1' else q['qty']
                        price=q[key]-side*slip
                        pnl=side*(price-q['entry'])*qty*c.point_value-qty*c.commission
                        cash+=pnl; q['pnl']+=pnl; q['qty']-=qty; q['partial']=True
                        if q['qty']==0: reason='targets'; break
            if exit_price is not None:
                pnl=side*(exit_price-q['entry'])*q['qty']*c.point_value-q['qty']*c.commission
                cash+=pnl; q['pnl']+=pnl; q['qty']=0
            if reason:
                trades.append({'entry_time':q['entered'],'exit_time':ts,'side':side,'entry':q['entry'],'quantity':q['initial_qty'],'pnl':q['pnl'],'reason':reason})
                position=None
        mtm=cash if not position else cash+position['side']*(r.close-position['entry'])*position['qty']*c.point_value
        curve.append({'timestamp':ts,'equity':mtm})
        # Signal at boundary is available for this minute only AFTER preceding
        # five-minute bar closed. Schedule now; earliest fill is next minute.
        if ts in sig.index and not position and not pending and in_session and ts!=d.index[-1]:
            q=sig.loc[ts]
            if q.side:
                pending=q[['side','limit','stop','target1','target2']].to_dict()
                pending['expires']=ts+pd.Timedelta(minutes=15)
    t=pd.DataFrame(trades,columns=['entry_time','exit_time','side','entry','quantity','pnl','reason'])
    e=pd.DataFrame(curve)
    dd=(e.equity/e.equity.cummax()-1).min() if len(e) else 0
    wins=t.loc[t.pnl>0,'pnl'].sum(); losses=-t.loc[t.pnl<0,'pnl'].sum()
    return t,e,{'trades':len(t),'net_pnl':cash-equity,'win_rate':float((t.pnl>0).mean()) if len(t) else 0,'profit_factor':float(wins/losses) if losses else None,'max_drawdown':float(dd)}
