"""Causal indicators. Wilder seed = arithmetic mean of first n valid inputs.

Missing observations reset Wilder warmup; EMA seeds from first close, is hidden
until n observations. RSI seeds from n price differences, ADX from n valid DX.
"""
import numpy as np
import pandas as pd

def wilder(series, period=14):
    result = np.full(len(series),np.nan)
    seed=[]; prior=None
    for i,x in enumerate(series):
        if not np.isfinite(x):
            seed=[]; prior=None
        elif prior is None:
            seed.append(float(x))
            if len(seed)==period:
                prior=sum(seed)/period; result[i]=prior
        else:
            prior=(prior*(period-1)+x)/period; result[i]=prior
    return pd.Series(result,index=series.index)

def ema(close, period):
    return close.ewm(span=period,adjust=False,min_periods=period).mean()

def atr(bars, period=14):
    prev=bars.close.shift()
    tr=pd.concat([bars.high-bars.low,(bars.high-prev).abs(),(bars.low-prev).abs()],axis=1).max(axis=1)
    return wilder(tr,period)

def rsi(close, period=14):
    change=close.diff()
    gain=wilder(change.clip(lower=0),period)
    loss=wilder(-change.clip(upper=0),period)
    out=100-100/(1+gain/loss.replace(0,np.nan))
    out.loc[(gain>0)&(loss==0)]=100
    out.loc[(gain==0)&(loss==0)]=50
    return out

def adx(bars, period=14):
    up=bars.high.diff(); down=-bars.low.diff()
    plus=up.where((up>down)&(up>0),0.0); minus=down.where((down>up)&(down>0),0.0)
    plus.iloc[:1]=np.nan; minus.iloc[:1]=np.nan
    a=atr(bars,period)
    p=100*wilder(plus,period)/a.replace(0,np.nan)
    m=100*wilder(minus,period)/a.replace(0,np.nan)
    dx=100*(p-m).abs()/(p+m).replace(0,np.nan)
    dx.loc[(p+m)==0]=0
    return wilder(dx,period)

def stochastic(bars, period=14, smooth=3):
    low=bars.low.rolling(period).min(); high=bars.high.rolling(period).max()
    raw=100*(bars.close-low)/(high-low).replace(0,np.nan)
    raw.loc[(high-low)==0]=50
    k=raw.rolling(smooth).mean()
    return k,k.rolling(smooth).mean()

def vwap(bars, volume_kind='traded'):
    if volume_kind != 'traded':
        return pd.Series(np.nan,index=bars.index)
    typical=(bars.high+bars.low+bars.close)/3
    known=bars.volume.notna().groupby(bars.session_id).cummin()
    numerator=(typical*bars.volume.fillna(0)).groupby(bars.session_id).cumsum()
    denominator=bars.volume.fillna(0).groupby(bars.session_id).cumsum()
    return (numerator/denominator.replace(0,np.nan)).where(known)

def compute_features_input(bars, config=None, volume_kind='traded'):
    x=bars.copy()
    for n in (9,15,30,65,200):
        x[f'ema{n}']=ema(x.close,n)
    x['atr']=atr(x,14); x['rsi']=rsi(x.close,14); x['adx']=adx(x,14)
    x['stoch'],x['stoch_d']=stochastic(x)
    x['vwap']=vwap(x,volume_kind)
    denom=x.close.diff().abs().rolling(20).sum()
    x['er']=((x.close-x.close.shift(20)).abs()/denom.replace(0,np.nan)).where(denom!=0,0)
    x['slope']=(x.ema65-x.ema65.shift(5))/(5*x.atr.replace(0,np.nan))
    x['gap_atr']=(x.open-x.close.shift()).abs()/x.atr.replace(0,np.nan)
    x['volume_kind']=volume_kind
    return x
