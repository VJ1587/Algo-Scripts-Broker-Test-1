"""Exit-first hysteresis; quality pause is orthogonal and immediate."""
from dataclasses import replace
import math
from ..contracts import RegimeState

DEFAULTS=dict(profile_sessions=20,min_bucket_observations=10,bucket_minutes=30,atr_period=14,er_period=20,slope_lag=5,
    directional_enter_er=.45,directional_enter_slope=.05,directional_exit_er=.35,directional_exit_slope=.035,
    range_enter_er=.25,range_enter_slope=.025,range_exit_er=.35,range_exit_slope=.04,persistence=3,
    volatility_low=.75,volatility_elevated=1.5,volatility_extreme=2.5,scale_min=.5,scale_max=2.,
    max_spread_ratio=.15,min_target_cost_ratio=3.,max_quote_age_seconds=60,mode='adaptive',require_complete_sessions=True)

def validate_config(config):
    unknown=set(config)-set(DEFAULTS)
    if unknown: raise ValueError(f'Unknown adaptation keys: {unknown}')
    c={**DEFAULTS,**config}
    for k,v in c.items():
        if isinstance(v,(int,float)) and (not math.isfinite(v) or v<=0): raise ValueError(f'Invalid {k}')
    if not (c['range_enter_er']<c['directional_exit_er']<=c['range_exit_er']<c['directional_enter_er']<=1 and
            c['range_enter_slope']<c['directional_exit_slope']<c['range_exit_slope']<c['directional_enter_slope'] and
            c['volatility_low']<c['volatility_elevated']<c['volatility_extreme'] and c['scale_min']<=1<=c['scale_max']):
        raise ValueError('Overlapping/invalid thresholds')
    for k in ('profile_sessions','min_bucket_observations','bucket_minutes','atr_period','er_period','slope_lag','persistence'):
        if not isinstance(c[k],int): raise ValueError(f'Integer {k} required')
    if c['min_bucket_observations']>c['profile_sessions'] or c['mode'] not in ('adaptive','fixed_atr','legacy_static'):
        raise ValueError('Invalid profile/mode')
    if (c['bucket_minutes'],c['atr_period'],c['er_period'],c['slope_lag']) != (30,14,20,5):
        raise ValueError('Alternative indicator versions require a named implementation')
    return c

def update_regime(features, prior_state=None, config=None):
    c=validate_config(config or {}); p=prior_state or RegimeState()
    f=features; reasons=list(f.get('quality_reasons',()))
    if f.get('quote_age') is not None and f['quote_age']>c['max_quote_age_seconds']: reasons.append('STALE_QUOTE')
    ratio=f.get('spread_atr')
    if ratio is None or not math.isfinite(ratio) or ratio<0: reasons.append('UNKNOWN_COSTS')
    elif ratio>c['max_spread_ratio']: reasons.append('ABNORMAL_SPREAD')
    v=f.get('volatility_ratio')
    vol='unknown' if v is None else 'extreme' if v>c['volatility_extreme'] else 'elevated' if v>c['volatility_elevated'] else 'low' if v<c['volatility_low'] else 'normal'
    if vol=='extreme': reasons.append('EXTREME_VOLATILITY')
    if reasons:
        return replace(p,quality_reasons=tuple(reasons),volatility_flag=vol,liquidity_flag='paused',consecutive_count=0,candidate_label=p.label)
    er,slope=f['er'],abs(f['slope'])
    # Existing labels exit to transition first; opposing entry needs a new streak.
    if p.label=='directional':
        candidate='transition' if er<c['directional_exit_er'] or slope<c['directional_exit_slope'] or (p.directional_sign and f['slope']*p.directional_sign<=0) else 'directional'
    elif p.label=='range':
        candidate='transition' if er>c['range_exit_er'] or slope>c['range_exit_slope'] else 'range'
    else:
        candidate='directional' if er>=c['directional_enter_er'] and slope>=c['directional_enter_slope'] else 'range' if er<=c['range_enter_er'] and slope<=c['range_enter_slope'] else 'transition'
    count=(p.consecutive_count+1 if candidate==p.candidate_label else 1) if candidate!=p.label else 0
    label=candidate if count>=c['persistence'] else p.label
    changed=f.get('timestamp') if label!=p.label else p.changed_at
    sign=(p.directional_sign if p.label=='directional' and p.directional_sign else (1 if f['slope']>0 else -1 if f['slope']<0 else 0)) if label=='directional' else 0
    return RegimeState(label,sign,candidate,0 if label==candidate else count,vol,'known' if f.get('relative_activity') is not None else 'unknown_activity',changed,())
