"""Session-boundary profiles use only prior fully observed sessions."""
from statistics import median
import numpy as np
from ..contracts import InstrumentProfile, freeze, stable_id

def build_profile(completed_sessions, instrument, cutoff, config):
    d=completed_sessions
    completed=[]
    for sid,g in d.groupby('session_id',sort=False):
        # Input carries expected setup count; a truncated session is never a profile observation.
        expected=int(g.expected_session_bars.iloc[0]) if 'expected_session_bars' in g else None
        if (g.session_end.iloc[0] <= cutoff and (g.available_at<=cutoff).all() and
            not g.missing_before.any() and expected is not None and len(g)==expected):
            completed.append(sid)
    completed=completed[-config['profile_sessions']:]
    prior=d[d.session_id.isin(completed)]
    buckets={}
    for bucket,g in prior.groupby('bucket'):
        # One observation per session and bucket, not one count per five-minute bar.
        by=g.groupby('session_id')[['atr','spread','volume']].median()
        excursions=g.groupby('session_id').apply(lambda x: float(x.high.max()-x.low.min()),include_groups=False)
        values={}
        for col,label in [('atr','atr'),('spread','spread'),('volume','activity')]:
            valid=by[col].replace([np.inf,-np.inf],np.nan).dropna()
            if col=='atr': valid=valid[valid>0]
            values[label]=float(valid.median()) if len(valid) else None
            values[label+'_count']=len(valid)
        values['excursion']=float(excursions.median()) if len(excursions) else None
        values['ready']=values['atr_count']>=config['min_bucket_observations']
        buckets[str(int(bucket))]=values
    ready=bool(buckets) and len(completed)==config['profile_sessions'] and all(b['ready'] for b in buckets.values())
    traits=[]
    ratios=[b['spread']/b['atr'] for b in buckets.values() if b['spread'] is not None and b['atr']]
    if ratios and median(ratios)>.1: traits.append('large_measured_spread_relative_to_atr')
    if len(prior) and prior.er.median()>.45: traits.append('persistent_measured_directional_movement')
    activities=[b['activity'] for b in buckets.values() if b['activity'] is not None]
    if activities and sum(activities)>0 and max(activities)/sum(activities)>.4:
        traits.append('high_measured_session_concentration')
    identity=dict(instrument=instrument.instrument_id,cutoff=str(cutoff),sessions=completed,buckets=buckets,version='1.0')
    return InstrumentProfile(stable_id(identity),instrument.instrument_id,'1.0',cutoff,tuple(completed),freeze(buckets),ready,
        str(prior.index.min()) if len(prior) else None,str(prior.index.max()) if len(prior) else None,
        instrument.volume_kind,min(1,len(completed)/config['profile_sessions']),tuple(traits))
