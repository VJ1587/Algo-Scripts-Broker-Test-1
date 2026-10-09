"""Resolve one coherent price scale; never inspect outcomes."""
import math
from ..contracts import PolicySnapshot, freeze, stable_id
from ..instrument import round_price
from .regime import validate_config

def resolve_policy(strategy_id, features, profile, regime, config, instrument):
    c=validate_config(dict(config.adaptation))
    mode=config.mode; f=features; reasons=list(regime.quality_reasons)
    atr=f.get('atr'); baseline=f.get('baseline')
    effective=None
    if mode=='legacy_static': effective=config.legacy_distance_unit
    elif mode=='fixed_atr': effective=atr
    elif baseline and profile.ready and atr and math.isfinite(atr):
        effective=baseline*min(c['scale_max'],max(c['scale_min'],atr/baseline))
    else: reasons.append('NOT_READY_PROFILE')
    if effective is None or not math.isfinite(effective) or effective<=0:
        reasons.append('NOT_READY_ATR'); effective=None
    if effective and math.isfinite(f.get('spread',float('nan'))):
        if f['spread']/effective>c['max_spread_ratio']: reasons.append('SPREAD_EFFECTIVE_ATR')
    else: reasons.append('UNKNOWN_COSTS')
    allowed=(1,-1)
    if strategy_id=='trend' and regime.label=='range': reasons.append('RANGE_TREND_PAUSE')
    if regime.label=='directional' and strategy_id in ('trend','regular_ma','base'):
        allowed=(regime.directional_sign,)
    if regime.label=='directional' and strategy_id in ('far_ma','double'):
        allowed=(regime.directional_sign,)
    distances={k:max(1,int(round_price(v*effective,instrument.tick_size,'up')/instrument.tick_size)) for k,v in config.coefficients.items()} if effective else {}
    confirmations=config.strong_confirmation_bars if regime.label=='transition' else config.confirmation_bars
    hold=min(config.max_hold,10 if regime.volatility_flag=='elevated' else 15)
    identity=dict(mode=mode,profile=profile.profile_id,regime=regime.label,distances=distances,allowed=allowed,
        confirmations=confirmations,expiry=config.expiry,hold=hold,config=stable_id(config))
    return PolicySnapshot(stable_id(identity),mode,profile.profile_id,regime.label,effective,freeze(distances),allowed,confirmations,config.expiry,hold,1.,not reasons,tuple(dict.fromkeys(reasons)))
