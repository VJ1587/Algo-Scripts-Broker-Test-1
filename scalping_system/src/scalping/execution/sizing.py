"""Stop risk + conservative exit friction, buying power and partial feasibility."""
from decimal import Decimal
from ..contracts import SizingDecision
from ..instrument import round_quantity, cash_per_price_unit

def size_order(proposal, account, instrument, costs, conversion=None, research=False):
    reasons=[]; zero=Decimal(0)
    if account.account_currency!=instrument.account_currency: reasons.append('ACCOUNT_CURRENCY')
    if account.equity<=0 or not account.equity.is_finite(): reasons.append('INSUFFICIENT_EQUITY')
    if account.available_buying_power is None and not research: reasons.append('MISSING_BUYING_POWER')
    if proposal.side==-1 and not instrument.short_allowed: reasons.append('SHORT_UNAVAILABLE')
    if proposal.order_type not in instrument.supported_orders: reasons.append('UNSUPPORTED_ORDER')
    try: point=cash_per_price_unit(instrument,conversion,account.timestamp)
    except ValueError: reasons.append('FX_CONVERSION'); point=zero
    budget=account.equity*Decimal(str(proposal.risk_fraction))*Decimal(str(proposal.policy.risk_multiplier))
    if account.approved_cash_risk is not None: budget=min(budget,account.approved_cash_risk)
    if budget<=0: reasons.append('NO_RISK_BUDGET')
    distance=proposal.side*(proposal.entry-proposal.stop)
    if distance<instrument.tick_size: reasons.append('INVALID_STOP')
    if any(proposal.side*(t-proposal.entry)<instrument.tick_size for t in proposal.targets): reasons.append('INVALID_TARGETS')
    loss=distance*point+(costs.slippage+costs.spread/2)*point+2*costs.fee_per_side
    if loss<=0: reasons.append('INVALID_UNIT_RISK')
    if reasons: return SizingDecision(False,zero,zero,zero,costs,tuple(reasons))
    quantity=round_quantity(min(budget/loss,instrument.max_quantity),instrument.quantity_step)
    margin_per_unit=instrument.margin_per_unit if instrument.asset_class=='future' else abs(proposal.entry)*point
    if account.available_buying_power is not None:
        quantity=min(quantity,round_quantity(max(zero,account.available_buying_power)/margin_per_unit,instrument.quantity_step))
    first=round_quantity(quantity*Decimal(str(proposal.fractions[0])),instrument.quantity_step)
    if quantity<instrument.min_quantity or first<instrument.min_quantity or quantity-first<instrument.min_quantity:
        reasons.append('INSUFFICIENT_PARTIAL_SIZE')
    return SizingDecision(not reasons,quantity,quantity*loss,quantity*margin_per_unit,costs,tuple(reasons))
