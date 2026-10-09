"""Strict metadata, exact tick arithmetic and causal currency conversion."""
from dataclasses import fields
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from datetime import timedelta
import yaml
from zoneinfo import ZoneInfo
from .contracts import InstrumentSpec, AssetClass, OrderType, freeze, aware

def decimal(value):
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("Nonfinite decimal")
    return d

def round_price(price, tick, direction):
    p,t = decimal(price), decimal(tick)
    if t <= 0 or direction not in ('up','down'):
        raise ValueError("Invalid tick/direction")
    return (p/t).to_integral_value(rounding=ROUND_CEILING if direction=='up' else ROUND_FLOOR)*t

def round_quantity(quantity, step):
    return round_price(quantity, step, 'down')

def load_instrument(path):
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text())
    names = {f.name for f in fields(InstrumentSpec)}
    if set(raw)-names:
        raise ValueError(f"Unknown instrument fields: {set(raw)-names}")
    for n in ('tick_size','quantity_step','min_quantity','max_quantity','multiplier','lot_size','pip_size','margin_per_unit'):
        if raw.get(n) is not None:
            raw[n] = decimal(raw[n])
            if raw[n] <= 0:
                raise ValueError(f"{n} must be positive")
    raw['asset_class'] = AssetClass(raw['asset_class'])
    for n in ('metadata_as_of','expiry'):
        if raw.get(n) is not None: raw[n]=str(raw[n])
    raw['supported_orders'] = tuple(OrderType(x) for x in raw['supported_orders'])
    raw['cost_model'] = freeze(raw['cost_model'])
    # Relative paths refer to project root (configs/instruments lives two levels down).
    root = path.parent.parent.parent if path.parent.name == 'instruments' else path.parent
    for n in ('session_calendar_path','eligibility_manifest_path','quotes_path','conversion_path'):
        if raw.get(n):
            raw[n] = str((root / raw[n]).resolve())
    spec = InstrumentSpec(**raw)
    validate_instrument(spec)
    return spec

def validate_instrument(s):
    ZoneInfo(s.timezone)
    for n in ('symbol','venue','settlement','metadata_as_of','pnl_currency','account_currency','session_calendar_path'):
        if not getattr(s,n):
            raise ValueError(f"Missing {n}")
    if s.min_quantity > s.max_quantity or s.min_quantity % s.quantity_step:
        raise ValueError("Invalid quantity limits")
    if s.price_basis not in ('mid','bid','ask','trade') or s.volume_kind not in ('traded','tick','unknown'):
        raise ValueError("Invalid price/volume provenance")
    if not s.supported_orders or not s.executable_prices:
        raise ValueError("Nonexecutable instrument")
    if s.synthetic and s.venue != 'SYNTHETIC':
        raise ValueError("Synthetic venue required")
    if type(s.synthetic) is not bool or type(s.short_allowed) is not bool or type(s.executable_prices) is not bool:
        raise ValueError('Boolean metadata required')
    if s.asset_class == AssetClass.FX and (not s.lot_size or not s.pip_size or s.quantity_unit not in ('base_units','lots')):
        raise ValueError("FX requires pip, lot and quantity units")
    if s.asset_class == AssetClass.FUTURE and (not s.expiry or not s.roll_policy or not s.margin_per_unit):
        raise ValueError("Futures requires expiry, roll policy and margin")
    if s.asset_class == AssetClass.STOCK and (not s.eligibility_manifest_path or not s.corporate_action_basis):
        raise ValueError("Equity manifest/action basis required")
    if s.asset_class == AssetClass.STOCK and s.corporate_action_basis not in ('raw_split_consistent','unadjusted synthetic; no actions'):
        raise ValueError('Executable equities require an explicit raw-price corporate-action basis')
    for n in ('spread','slippage_ticks','fee_per_unit_per_side'):
        if n not in s.cost_model or decimal(s.cost_model[n]) < 0:
            raise ValueError(f"Missing/invalid cost estimate {n}")
    if set(s.cost_model)!={'spread','slippage_ticks','fee_per_unit_per_side'}:
        raise ValueError('Unknown cost model fields')

def eligible(s, timestamp, synthetic=False):
    if s.synthetic and not synthetic:
        return False, 'SYNTHETIC_FLAG_REQUIRED'
    if s.asset_class != AssetClass.STOCK:
        if s.expiry and str(timestamp.date()) > s.expiry:
            return False, 'EXPIRED_CONTRACT'
        return True, None
    manifest = yaml.safe_load(Path(s.eligibility_manifest_path).read_text())
    if manifest['large_cap_min_usd']<10_000_000_000 or manifest['mega_cap_min_usd']<200_000_000_000:
        return False,'INVALID_CAP_THRESHOLDS'
    day = str(timestamp.date())
    rows = [r for r in manifest['members'] if r['symbol']==s.symbol and r['from'] <= day <= r['through']]
    if not rows:
        return False, 'NO_POINT_IN_TIME_MEMBERSHIP'
    r = rows[-1]
    if s.synthetic:
        return (synthetic and manifest.get('synthetic') is True), 'SYNTHETIC_ELIGIBILITY'
    return (r['verified'] is True and r['blue_chip'] is True and
            r['market_cap_usd'] >= manifest['large_cap_min_usd'] and
            s.symbol in manifest['manual_allowlist']), 'EQUITY_ELIGIBILITY'

def cash_per_price_unit(instrument, conversion, as_of=None, max_age_seconds=300):
    multiplier = instrument.multiplier * (instrument.lot_size if instrument.quantity_unit=='lots' else 1)
    if instrument.pnl_currency == instrument.account_currency:
        return multiplier
    if conversion is None or as_of is None:
        raise ValueError("Missing timestamped currency conversion")
    aware(conversion.timestamp); aware(as_of)
    age = (as_of-conversion.timestamp).total_seconds()
    if not 0 <= age <= max_age_seconds or conversion.rate <= 0 or not conversion.rate.is_finite():
        raise ValueError("Stale/future/invalid conversion")
    if (conversion.from_currency,conversion.to_currency)!=(instrument.pnl_currency,instrument.account_currency):
        raise ValueError("Wrong conversion currencies")
    return multiplier * conversion.rate
