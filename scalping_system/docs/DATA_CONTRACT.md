# Data, calendar and metadata contract

CSV timestamp denotes one-minute candle OPEN; UTC or an explicit offset is
required. `open_time`, `close_time=open+1 minute`, and `available_at` remain
separate. Optional input `available_at` may delay data but cannot precede close;
optional `complete=false` marks an unfinished bar. Caller-provided `as_of` filters
future/unavailable bars. Unknown volume is blank/NaN, not automatic zero activity.

```csv
timestamp,open,high,low,close,volume
2026-01-05T14:30:00Z,100.00,100.20,99.90,100.10,1000
2026-01-05T14:31:00Z,100.10,100.25,100.00,100.20,1100
```

OHLC must be finite and internally bounded, timestamps sorted and unique,
volume nonnegative or unknown. Negative futures prices are allowed; nonpositive
stock/FX prices fail. Input `price_basis` is explicitly mid/bid/ask/trade. Trade
OHLC with quote spread is an estimated executable envelope, not exact tick replay.
Bid/ask sidecar schema:

```csv
timestamp,bid,ask
2026-01-05T14:30:00Z,99.995,100.005
```

Quote timestamps denote observation time. Joins never look ahead, stale quotes
pause entries. If supplied, quote prices drive event-open bid/ask; OHLC extrema
remain estimated. No queue priority is inferred. `--quotes` and `--conversions`
override instrument auxiliary paths. Config paths in `configs/instruments` are
project-relative; other configs resolve paths beside themselves.

Calendar YAML explicitly supplies timezone and all UTC intervals:

```yaml
timezone: America/Chicago
sessions:
  - id: overnight-example
    start: '2026-03-08T23:00:00Z'
    end: '2026-03-09T22:00:00Z'
    breaks:
      - ['2026-03-09T12:00:00Z', '2026-03-09T12:30:00Z']
```

Named timezone is display context, not inferred holiday logic. Overnight rollover,
holidays, early closes and DST belong in verified supplied intervals. Scheduled
breaks are excluded from expected opens; same UTC-date gaps are not inherently
missing data. Resampling aligns to session open and uses right-edge close labels.
All expected components must be complete and available, and no bucket crosses a
break. Incomplete buckets are excluded; the following setup carries missing-data
quality. Missing closing prices flag unpriceable liquidation, never a fabricated
close or silent overnight carry.

Modern metadata requires symbol, actual venue/source, asset class, timezone,
calendar, tick, quantity step/min/max, multiplier, P&L/account currencies,
settlement, supported order types, price/volume basis, short eligibility, dated
metadata and complete cost estimates. The real-instrument template has nulls and
must fail until verified. No actual contract specifications were invented.

Stocks require dated point-in-time membership, verified capitalization and a
manual blue-chip allowlist. Large cap is >=USD10 billion; mega cap >=USD200 billion.
Eligibility applies on the evaluation date, including historical constituents;
today's membership must not substitute for a past universe. Synthetic stock
eligibility is permitted only with synthetic metadata and explicit CLI flag.
Corporate-action basis must be supplied consistently: prices used for execution
must correspond to actual executable prices, and split-adjusted history must not
be mixed with unadjusted orders/positions. This package does not download/process
split or borrow event feeds; supply consistent segments and current short
availability. This remains a real-data preparation obligation, recorded as a
limitation rather than guessed metadata.

FX explicitly uses base units or lots with lot size. Pip size is display; tick
is minimum executable increment. Tick volume is broker activity, not consolidated
trade volume. For differing P&L and account currencies supply:

```csv
timestamp,from_currency,to_currency,rate
2026-01-05T14:30:00Z,USD,EUR,0.9
```

Rate means account currency per one P&L-currency unit. It must be positive,
finite, causal, correctly labelled and <=300 seconds old. No forward interpolation
or inferred inverse currency rate. Missing conversion blocks sizing and priceable
simulation events. Fees are per quantity per side in account currency; multiplier
is P&L currency per price unit per quantity (lot size multiplies it for lots).

Futures require actual contract expiry, tick/multiplier, separate margin per unit,
break calendar, explicit roll policy and executable-price flag. Unmapped
back-adjusted continuous series must have `executable_prices=false` and fail
execution validation. Supply actual contract segments and documented mapping
externally; no roll P&L is inferred. Spot gold, gold futures/CFDs, SPX cash, index
futures and broker CFDs require different definitions. Asset classes supported
here are stock, FX and actual futures; no CFD alias is accepted as a contract.

`margin_per_unit`, available buying power and approved cash risk are denominated
in account currency. Convert venue margin requirements before supplying metadata;
they are distinct from the converted stop-loss calculation. With no specified
futures margin, execution validation fails. Stock/FX buying power uses conservative
full notional; no unverified leverage is inferred.

All built-in data comes from the deterministic synthetic generator. Venue is
`SYNTHETIC`; no real quotes, exchange calendars, equity eligibility or broker
specifications have been verified by this implementation exercise.
