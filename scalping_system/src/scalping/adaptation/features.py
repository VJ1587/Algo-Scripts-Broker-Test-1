"""One feature snapshot shared by exports and decisions; causal quote joins."""
import math
import pandas as pd

def last_quote(quotes, as_of, session_start=None):
    if quotes is None or not len(quotes): return None
    position=quotes.index.searchsorted(as_of,side='right')-1
    if position<0: return None
    if session_start is not None and quotes.index[position]<session_start: return None
    return quotes.iloc[position]

def compute_behavior_features(closed_bars, profile, quotes, as_of):
    if not len(closed_bars): return {'quality_reasons':('NO_DATA',)}
    r=closed_bars.iloc[-1]
    if r.available_at>as_of: raise ValueError('Future feature bar')
    b=profile.buckets.get(str(int(r.bucket)),{})
    baseline=b.get('atr') if b.get('ready') and profile.ready else None
    atr=float(r.atr)
    quote=last_quote(quotes,as_of,r.session_start)
    spread=float(quote.ask-quote.bid) if quote is not None else float(r.spread)
    age=(as_of-quote.name).total_seconds() if quote is not None else None
    reasons=[]
    if not all(math.isfinite(float(v)) for v in (atr,r.er,r.slope,r.close)) or atr<=0:
        reasons.append('INVALID_FEATURES')
    if bool(r.missing_before): reasons.append('MISSING_BAR')
    if quote is not None and (not math.isfinite(spread) or spread<0): reasons.append('INVALID_QUOTE')
    activity=b.get('activity')
    return dict(timestamp=str(as_of),session_id=r.session_id,bucket=int(r.bucket),atr=atr,baseline=baseline,
        volatility_ratio=atr/baseline if baseline else None,er=float(r.er),slope=float(r.slope),adx=float(r.adx),
        spread=spread,spread_atr=spread/atr if atr>0 else None,quote_age=age,
        cost_provenance='quote' if quote is not None else 'OHLC_estimated',
        relative_activity=float(r.volume)/activity if activity and pd.notna(r.volume) else None,
        volume_kind=profile.volume_kind,gap_atr=float(r.gap_atr),
        bid=float(quote.bid) if quote is not None else float(r.close)-spread/2,
        ask=float(quote.ask) if quote is not None else float(r.close)+spread/2,
        session_phase=float((as_of-r.session_start)/(r.session_end-r.session_start)),quality_reasons=tuple(reasons))
