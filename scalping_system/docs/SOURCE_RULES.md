# Source rules and declared version 1.0 interpretations

Reference: user-supplied `BUILD_SPECIFICATION.md`, sections 2 and Appendix A3;
the photograph is described by that document and its embedded original README.
No image was separately supplied or verified. Original source distances are
stock-dollar numbers; their adaptive translation is a dimensionless hypothesis,
not financial equivalence or proof of profitability.

| Method | Preserved rule and implementation | Ambiguity / exact interpretation and deviations |
|---|---|---|
| Trend | `trend/strategy.py`: EMA9 crosses EMA15 on the completed setup; ordered 9/15/30 stack; separation <=0.50 scale; pullback limit at EMA9 | No-stop/add-every-0.50 wording is replaced by protective stop 0.50 beyond EMA30 and no pyramiding. Nonmarketable pullback only. Targets 1/2 scale. Hold-for-day and EMA15 trailing exit absent; frozen 10/15-minute research timeout. Transition waits two later directional one-minute candles while retaining the original crossing evidence. |
| Regular MA | `regular_ma/strategy.py`: price is 1–1.50 scale ahead of an eligible EMA65/EMA200/traded-volume VWAP in pullback direction; stochastic <50/>50; execution direction confirms | Legacy approach side could create a marketable buy limit; corrected to level behind price. Nearest level wins, ties 65 then 200 then VWAP. EMA15/30 alignment required outside range. Range retains correct approach side and candle confirmation. RSI wording ambiguous and not imposed. Five-minute default; named 15/30 versions are independent completed-chart runs. |
| Far MA | `far_ma/strategy.py`: extension >=1.50 scale from nearest of all five means, RSI <=20/>=80 and stochastic <20/>80 | Legacy used EMA65 only and entered signal close. Corrected explicit arm → later directional closed one-minute candle → stop one tick beyond that candle high/low. This candle is the prior bar for the upcoming breakout. Transition requires two consecutive directional execution bars. Stop 0.50 beyond original setup low/high. Expire at 15 minutes; invalidate structure breach or mean touch. Countertrend directional-regime entry blocked until regime changes; target-one must fit before mean after costs. |
| Base | `base/strategy.py`: prior six completed five-minute bars span 30 minutes, total range <=0.50 scale; exclude breakout bar; directional close beyond boundary+one tick | Subjective “at base” resolved to confirmed escape. Transition needs two setup closes beyond frozen boundary. Market order at next eligible event; stop 0.50 beyond opposite boundary. No extra vote from unfinished 15/30-minute charts. |
| Double | `double/strategy.py`: strict three-bar confirmed pivots; second known only after right close; separation 30–120 minutes, tolerance <=0.50 scale, intervening excursion >=1 scale | Most recent qualifying first pivot selected. Neckline is maximum intervening high for bottoms / minimum low for tops. Directional closed neckline confirmation replaces blind second-pivot retest. Transition requires two confirmations. Market entry; stop 0.50 beyond both pivots. |

All short rules are symmetric, subject to short availability and regime approval.
Targets are 0.50/1.50 scale with 50/50 quantities except trend 1/2 scale.
Risk remains 0.5% by default and never increases through adaptation. Target and
stop buffers resolve through a single effective ATR. Protective stops are never
inside structural invalidation. Entry expiry and hold limit are separate; the
hold interpretation is engineering policy, not a claim about the source sheet.
Daily/weekly blank rows, multi-day far-MA reversals and slow-day swing momentum
are outside this request. No swing method has been added.

Shared corrections relative to the baseline: arithmetic Wilder seeding; VWAP by
explicit session; correct approach direction; all five means for far-MA;
closed one-minute confirmation; limit/stop/market distinctions; no extra minute
latency; tick rounding by decimal; causal conversion and margin sizing; target
limit prices never worsened; initial equity in drawdown; missing final close
flagged; explicit overnight/break calendars. Frozen baseline semantics remain
available for comparison and are not silently relabelled as corrected.
