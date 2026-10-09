# Causal adaptation version 1.0

Default values are executable in `configs/adaptation.yaml` and strategy YAML;
configuration rejects unknown keys and overlapping/nonfinite thresholds.

Profiles refresh at each supplied session boundary using the previous 20 fully
observed completed sessions. Current-session observations never enter a profile.
Session completeness requires the calendar-derived number of closed setup bars,
with no missing expected components. For each 30-minute session-relative bucket,
first compute each session's median ATR/spread/activity, then the median across
sessions. Counts are sessions, not individual bars. At least 10 valid ATR session
observations per bucket and 20 completed sessions are required. Old partial
sessions are skipped, extending the history span to get 20 complete ones. No
fabricated baseline is used. ATR unavailable after warmup remains NaN. Profiles
include ID/hash, version, cutoff, dates, session IDs, bucket counts and volume
provenance. Measured traits are conditional observations rather than permanent
instrument personality labels.

EMA periods stay 9/15/30/65/200. EMA uses first-close recursive seed with values
hidden until its period. Wilder ATR14 begins with arithmetic mean of 14 true
ranges; first true range is high-low. Wilder RSI14 uses the first 14 price
differences. Wilder DM uses the first 14 differences and ADX14 seeds from the
first 14 available DX values; a zero DX denominator gives zero. Missing Wilder
input resets warmup. Stochastic14 K uses three-period smoothing and D a further
three; a zero range gives 50. VWAP is cumulative typical-price × volume / volume
by session; unknown/zero traded volume disables it, tick activity never implies
centralized-volume VWAP. A missing observation disables VWAP for the rest of
that session.

ER20 = |close[t]-close[t-20]| / sum(|delta close|,20); zero denominator gives 0.
Normalized slope = (EMA65[t]-EMA65[t-5]) / (5 × ATR[t]). ADX is exported as a
diagnostic and is not an added entry predicate. Quote joins and conversion joins
are backward only. Spread/ATR, open gap/ATR, quote age, missing-bar flags, session
phase and same-bucket relative activity are exported. Missing activity is unknown,
not zero liquidity. Quote spread and OHLC-estimated spread are labelled.

Regime updates once per closed setup bar. From transition, directional entry
requires ER >=0.45 and absolute slope >=0.05; range entry ER <=0.25 and slope
<=0.025. Each needs three consecutive bars. Existing directional label first exits
to transition after ER <0.35 OR slope <0.035 for three bars; an opposing slope also
exits. Existing range exits after ER >0.35 OR slope >0.04 for three bars. Exit-first
priority prevents an instantaneous direct range/directional flip; entering the
opposite state needs a new streak. Counters persist. An invalid feature, missing
expected bar, quote age >60 seconds, unknown costs, spread/ATR >0.15 or extreme
volatility immediately pauses entries; this does not wait for hysteresis. Activity
can be unknown while executable cost quality is known.

Volatility ratio is ATR / prior-session same-bucket median ATR. Low <0.75;
normal includes 0.75 and 1.50; elevated >1.50 through 2.50; extreme >2.50 pauses
before scale clipping. Adaptive scale = clip(ratio,0.5,2); effective ATR = baseline
× scale, exactly once. Fixed-ATR uses ATR now. Legacy-static uses the configured
price unit. Distance = ceil(coefficient × effective ATR / tick) ticks, at least one
tick. Stop prices round away from entry and targets conservatively toward entry.
No generic Python rounding supplies executable prices. Base breakout's tiny
positive coefficient intentionally resolves to the one-tick minimum by default.

Trend pauses in range; directional trend/regular/base require slope/EMA-stack
alignment. Regular range interaction still has its approach and oscillator
predicates. Far-MA and double need explicit reversal evidence; opposite-direction
entries are blocked while a directional regime persists. Transition requires two
completed directional confirmations rather than one; it never removes original
predicates. Strategy predicates have immutable names and may not be removed by
configuration.

Costs must be complete. Spread / effective ATR <=0.15 and target-one gross travel
>3 × estimated round-trip price costs are necessary, not an expected-profit claim.
Round-trip price cost = spread + 2 slippage + 2 cash fees / converted point value.
Targets/stops/coefficients/profile/regime, risk fraction, expiry and timeout freeze
when a candidate is armed and emitted. Regime/quality can cancel an unfilled
candidate/order; filled positions retain exits. Elevated volatility selects
10-minute max hold, normal/low 15, bounded by session close and hard maximum15.
Expiry is15 minutes, base formation minimum30, pivot separation30–120. Timing
minima are never compressed. Stops never widen. Market target anchoring at actual
fill happens once using frozen distances, with risk/cost revalidation.

Sizing loss per quantity = structural entry-stop distance × multiplier × causal
FX rate + adverse stop slippage/spread allowance + two cash fees. Quantity floors
to tradable step, bounded by risk budget, max quantity and buying power/margin.
Risk budget is min(configured strategy budget, external cash approval); default
0.5%, hard cap2%, policy multiplier <=1. Minimum partial quantities must be
feasible. A stop gap can violate planned risk and is reported explicitly.

Offline calibration accepts supplied grids and metric, retains all attempts,
uses chronological training-only selection and >=30-minute embargo. No open-trade
outcome triggers refitting. Engineering tests use synthetic rows; real-data
performance and appropriate uncertainty intervals remain pending.
