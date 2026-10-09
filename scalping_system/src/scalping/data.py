"""One-minute open timestamps and fully closed session-relative resampling."""
import numpy as np
import pandas as pd
from .sessions import SessionCalendar

def load_bars(path, instrument):
    d = pd.read_csv(path)
    if 'timestamp' not in d:
        raise ValueError("timestamp required")
    parsed = [pd.Timestamp(t) for t in d.pop('timestamp')]
    if any(t.tzinfo is None for t in parsed):
        raise ValueError("Input timestamps require UTC/explicit offsets")
    d.index = pd.DatetimeIndex(parsed).tz_convert('UTC') if parsed else pd.DatetimeIndex([],tz='UTC')
    for col in ('open','high','low','close','volume'):
        if col in d: d[col]=pd.to_numeric(d[col],errors='raise').astype(float)
    validate_bars(d, instrument)
    cal = SessionCalendar.load(instrument.session_calendar_path)
    d['open_time'] = d.index
    d['close_time'] = d.index + pd.Timedelta(minutes=1)
    if 'available_at' in d:
        d['available_at'] = pd.to_datetime(d.available_at,utc=True)
        if (d.available_at < d.close_time).any():
            raise ValueError("Bar available before close")
    else:
        d['available_at'] = d.close_time
    if 'complete' not in d:
        d['complete'] = True
    d['session_id'] = [s.session_id if (s:=cal.session_at(t)) else None for t in d.index]
    if d.session_id.isna().any():
        raise ValueError("Bars outside supplied session calendar")
    d['session_start'] = pd.to_datetime([cal.session_at(t).start for t in d.index],utc=True)
    d['session_end'] = pd.to_datetime([cal.session_at(t).end for t in d.index],utc=True)
    d['bucket'] = ((d.index-d.session_start).dt.total_seconds()//1800).astype(int)
    expected = cal.expected_bar_opens(d.index.min(),d.index.max()+pd.Timedelta(minutes=1)) if len(d) else d.index
    missing = expected.difference(d.index)
    d['missing_before'] = False
    for t in missing:
        later = d.index[d.index > t]
        if len(later) and cal.session_at(later[0]).session_id == cal.session_at(t).session_id:
            d.loc[later[0],'missing_before'] = True
    return d

def validate_bars(d, instrument):
    if not isinstance(d.index,pd.DatetimeIndex) or d.index.tz is None or d.index.has_duplicates or not d.index.is_monotonic_increasing:
        raise ValueError("Sorted unique aware bar index required")
    if not {'open','high','low','close'} <= set(d):
        raise ValueError("OHLC required")
    if not np.isfinite(d[['open','high','low','close']].to_numpy(dtype=float)).all():
        raise ValueError("Nonfinite OHLC")
    if (d.high < d[['open','close','low']].max(axis=1)).any() or (d.low > d[['open','close','high']].min(axis=1)).any():
        raise ValueError("Invalid candle bounds")
    if instrument.asset_class != 'future' and (d[['open','high','low','close']] <= 0).any().any():
        raise ValueError("Nonpositive non-futures prices")
    if 'volume' not in d:
        d['volume'] = np.nan
    if (d.volume.dropna()<0).any() or np.isinf(d.volume).any():
        raise ValueError("Invalid volume")

def closed_resample(bars, timeframe, as_of, session_calendar):
    n = int(pd.Timedelta(timeframe).total_seconds()/60)
    if n <= 0:
        raise ValueError("Invalid timeframe")
    d = bars.loc[(bars.available_at <= as_of) & bars.complete].copy()
    result = []
    for sid, group in d.groupby('session_id',sort=False):
        session = next(s for s in session_calendar.sessions if s.session_id==sid)
        previous_close = session.start
        offsets = ((group.index-session.start).total_seconds()//(60*n)).astype(int)
        for offset,g in group.groupby(offsets,sort=True):
            opening = session.start + pd.Timedelta(minutes=int(offset)*n)
            closing = opening + pd.Timedelta(minutes=n)
            expected = pd.date_range(opening,closing,freq='min',inclusive='left')
            # Never aggregate across a break, missing component or incomplete candle.
            if closing > as_of or closing > session.end or len(g)!=n or not g.index.equals(expected) or not all(session.active(t) for t in expected):
                continue
            volume = g.volume.sum() if g.volume.notna().all() else np.nan
            missing_gap=any(session.active(t) and t not in d.index for t in pd.date_range(previous_close,opening,freq='min',inclusive='left'))
            result.append(dict(timestamp=closing,open=g.open.iloc[0],high=g.high.max(),low=g.low.min(),close=g.close.iloc[-1],volume=volume,
                open_time=opening,close_time=closing,available_at=g.available_at.max(),session_id=sid,session_start=session.start,session_end=session.end,
                bucket=int((opening-session.start).total_seconds()//1800),complete=True,missing_before=bool(g.missing_before.any()) or missing_gap,
                spread=float(g.spread.median()) if 'spread' in g else np.nan))
            previous_close = closing
    columns=['open','high','low','close','volume','open_time','close_time','available_at','session_id','session_start','session_end','bucket','complete','missing_before','spread']
    if not result:
        return pd.DataFrame(columns=columns,index=pd.DatetimeIndex([],tz='UTC',name='timestamp'))
    return pd.DataFrame(result).set_index('timestamp').sort_index()
