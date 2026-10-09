"""Separate price spread/slippage from cash fees; never worsen limit fills."""
from ..contracts import Costs
from ..instrument import decimal, cash_per_price_unit

def estimate_costs(instrument, timestamp, quote=None):
    model=instrument.cost_model
    spread=decimal(quote.ask-quote.bid) if quote is not None else decimal(model['spread'])
    if spread<0: raise ValueError('Crossed quote')
    return Costs(spread,instrument.tick_size*decimal(model['slippage_ticks']),decimal(model['fee_per_unit_per_side']),
        timestamp,'quote' if quote is not None else 'OHLC_estimated',instrument.price_basis)

def round_trip_price(costs, instrument, conversion=None):
    point=cash_per_price_unit(instrument,conversion,costs.timestamp)
    return costs.spread+2*costs.slippage+2*costs.fee_per_side/point

def executable_prices(bar, side, entering, costs, quote=None):
    # Quote-side OHLC is not available: quotes provide event-open only; extrema are estimated.
    if quote is not None:
        opening=decimal(quote.ask if (side==1)==entering else quote.bid)
    else:
        opening=decimal(bar.open)
        if costs.price_basis in ('mid','trade'):
            opening+=(side if entering else -side)*costs.spread/2
        elif costs.price_basis=='bid' and (side==1)==entering:
            opening+=costs.spread
        elif costs.price_basis=='ask' and (side==1)!=entering:
            opening-=costs.spread
    offset=(side if entering else -side)*costs.spread/2 if costs.price_basis in ('mid','trade') else (costs.spread if costs.price_basis=='bid' and (side==1)==entering else -costs.spread if costs.price_basis=='ask' and (side==1)!=entering else 0)
    return opening,decimal(bar.high)+offset,decimal(bar.low)+offset
