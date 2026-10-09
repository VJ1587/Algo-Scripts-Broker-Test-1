Build an instrument-adaptive scalping system
Task for Codex or Claude Code
Implement the specification below. This document is self-contained: Appendix B embeds the complete, exact contents of all 13 files in the original ZIP. If the project is absent, reconstruct it using Appendix B first. If it exists, inspect it and preserve local modifications; do not overwrite user work. Read the reconstructed README and source before upgrading. Create the actual files, run meaningful tests, fix failures, and report implemented versus deferred requirements. This document is a build instruction, not a claim that the adaptive features already exist. Work in VS Code with Python 3.10+. Preserve a versioned legacy baseline so the original research prototype can be compared with the new implementation. Appendix B is a historical baseline, not the final implementation: its disclosed shortcomings must be corrected under sections 1–11 and Appendix A. This specification takes precedence over embedded legacy comments and defaults for the upgraded implementation.
Goal: adapt to measurable instrument behavior and current market conditions while preserving each strategy's setup logic. Support currencies, large/mega-cap blue-chip stocks, and futures through explicit instrument metadata. Do not assume the same dollar move, pip distance, trading session, spread, or contract value means the same thing across markets.
Use deterministic, explainable rules first. Do not implement a self-modifying trading model, online P&L optimizer, or automatic risk escalation. All initial coefficients below are engineering hypotheses requiring validation, not proven profitable settings.
1. Independent strategies and file ownership
The current five entry scripts are wrappers around one signals() function. Refactor: each method must own its signal state machine, configuration, tests and command-line entry point in its own directory. Strategies may depend on shared, versioned utilities; they must never import another strategy. An edit to one strategy must not change another strategy's output on its reference fixture. Shared code changes require the shared regression suite.
Create this structure, filling every included module with working code rather than placeholders:
scalping_system/
  pyproject.toml
  README.md
  configs/
    instruments/                 # validated examples, one file per symbol/venue
    sessions/                    # calendars, timezone, breaks, overnight rollover
    adaptation.yaml
  src/scalping/
    contracts.py                 # immutable typed records and schemas
    data.py                      # closed bars, timestamps, resampling, freshness
    indicators.py                # EMA, Wilder ATR/RSI/ADX, stochastic, VWAP
    instrument.py                # tick, lot, multiplier, FX conversion, eligibility
    sessions.py
    adaptation/
      features.py
      profile.py                 # slow instrument/session behavior profile
      regime.py                  # fast current regime with hysteresis
      policy.py                  # bounded strategy-specific changes
      calibration.py             # offline training only
    execution/
      simulator.py
      costs.py
      sizing.py
    strategies/
      trend/{__init__.py,strategy.py,config.yaml,cli.py}
      regular_ma/{__init__.py,strategy.py,config.yaml,cli.py}
      far_ma/{__init__.py,strategy.py,config.yaml,cli.py}
      base/{__init__.py,strategy.py,config.yaml,cli.py}
      double/{__init__.py,strategy.py,config.yaml,cli.py}
    reporting.py
    cli.py
  tests/
    test_causality.py
    test_adaptation.py
    test_instrument_math.py
    test_execution.py
    test_strategy_isolation.py
    strategies/                  # positive and negative fixtures per strategy
  docs/
    SOURCE_RULES.md
    ADAPTATION_RULES.md
    DATA_CONTRACT.md
    VALIDATION_REPORT.md
  examples/
    generate_synthetic_data.py
Keep existing launch filenames as backward-compatible wrappers. Add the adaptations to both the exported indicator series and the strategy decision logic. Keep aggregate account allocation in a separate future component. Strategies emit proposed risk; they do not assume they control the entire account.
2. Core strategy identity is immutable within a version
Create SOURCE_RULES.md mapping each photograph rule to implementation, ambiguity, and explicit interpretation. Carry forward disclosed differences; do not label a changed system an exact transcription.
Method	Preserve	Allowed adaptation
Trend	Directional EMA 9/15/30 alignment and pullback entry; structural invalidation	EMA separation tolerance, entry buffer, stop buffer, target distances, eligibility and timeout within limits
Regular MA	Interaction with EMA 65/200 or valid VWAP, with specified directional confirmation	Approach distance and level tolerance, cost filter, session eligibility
Far MA	Extended distance from mean, RSI/stochastic extreme, then confirmed reversal	Extension threshold, additional confirmation in strong trends, eligibility; never remove reversal confirmation
Base	A genuinely narrow consolidation before directional escape	Volatility-scaled width, breakout buffer, liquidity confirmation
Double top/bottom	Two confirmed pivots separated in time with an intervening excursion	Pivot tolerance and excursion distance, bounded confirmation window


Default EMA periods remain 9/15/30/65/200. Default RSI/stochastic extreme levels remain 20/80. Do not change these continuously with regime. Alternatives require named, offline-tested versions.
Implement the missing far-MA one-minute confirmation as an explicit state machine after the completed higher-timeframe setup: arm setup, await directional closed candle, establish prior-bar breakout trigger, expire if unconfirmed, invalidate if structure fails. Record the chosen precise interpretation of the image. Higher-timeframe signals use only fully closed candles.
Never omit a protective stop to reproduce the source's no-stop wording. Pyramiding stays disabled. Do not silently add a swing strategy; daily/weekly rows remain outside this task.
3. Instrument metadata and universe
Require symbol, asset class, venue/data source, timezone, calendar/session, tick size, quantity step/minimum, multiplier, P&L currency, account currency, settlement conventions, short eligibility, cost model and supported order types. Missing essential metadata blocks sizing and actionable signals. Static templates must be labelled examples requiring verification.
- Stocks: input universe is only verified large/mega-cap blue chips. Use a dated eligibility manifest; define large/mega-cap thresholds and a manual blue-chip allowlist rather than guessing what blue chip means. Backtests need point-in-time membership to avoid survivorship bias. No penny, small-cap, or mid-cap additions. Model splits/corporate actions consistently and short availability.
- FX: quantity is explicitly base units or lots with lot size specified. Pip display and minimum tick are distinct. Convert quote-currency P&L into account currency using a causal, timestamped conversion rate. Block if conversion is stale/missing. Distinguish centralized volume from broker tick volume.
- Futures: use actual contract tick and multiplier, expiry, session breaks and roll policy. Back-adjusted continuous data may inform analysis but never supply executable prices or roll P&L without a documented mapping. Check margin metadata separately from stop risk.
- Keep spot gold, gold futures and gold CFDs separate instrument definitions. Likewise separate SPX cash index, equity index futures and broker CFDs. A symbol alias is not a contract specification.
4. Two speeds of adaptation
Slow instrument/session profile
Build a profile using the previous 20 completed sessions only; refresh at session boundaries. Use 30-minute session-relative buckets with at least 10 prior valid observations for each bucket. Estimate typical ATR, spread, trading activity and excursion characteristics. Extend historical lookback if needed rather than inventing values. Insufficient history means NOT_READY for adaptive trading; an explicitly selected static research mode may run and must be labelled static.
Store symbol, venue, profile version, input date range, last refresh, observation counts, volume provenance and profile confidence. Display measured traits such as high session concentration, large typical spread relative to ATR, or persistent directional movement. Avoid permanent labels such as “EUR/USD is always mean reverting.”
Fast current regime
Evaluate once per completed five-minute setup bar. Keep one-minute execution confirmation separate. Features:
- Wilder ATR(14), with an explicit documented seed, on setup bars.
- Volatility ratio v = ATR_now / median(prior_session_ATR_same_bucket).
- Kaufman-style efficiency ER20 = abs(close_t - close_(t-20)) / sum(abs(delta_close), 20); zero denominator returns zero.
- EMA65 slope: (EMA65_t - EMA65_(t-5)) / (5 * ATR_now).
- Wilder ADX(14) as a diagnostic/optional versioned confirmation, not an undocumented extra entry condition.
- Spread/ATR, recent gap/ATR, quote age, missing-bar flags and session phase.
- Relative traded volume or tick activity against prior-session same-bucket medians, with provenance. Missing volume is unknown, never automatically zero liquidity.
Initial candidate regimes: directional, range, transition, plus separate volatility and liquidity flags. Example directional entry: ER >= 0.45 and absolute normalized slope >= 0.05 for three closed bars. Example range entry: ER <= 0.25 and absolute slope <= 0.025 for three bars. Everything else is transition. Retain current state until exit thresholds are crossed for three bars; directional exit uses ER < 0.35 or slope < 0.035, range exit ER > 0.35 or slope > 0.04. Specify evaluation order and test every boundary.
Volatility flags: low below 0.75, normal 0.75–1.5, elevated above 1.5; extreme above 2.5 pauses new entries. These provisional thresholds live in config. Invalid/stale data or abnormal spread causes an immediate pause without waiting for regime hysteresis. Instrument profiles calibrate thresholds offline; never re-fit from the outcome of an open trade.
5. Adaptive price distances: one coherent scale
Use instrument-relative distances, not a common stock-dollar mapping. Keep two explicitly separate modes:
1. legacy_static: original price-distance interpretation, for comparison only.
2. adaptive: ATR coefficients preserving each source rule's relative geometry.
For adaptive mode define:
baseline = prior-session same-bucket median ATR
scale_raw = ATR_now / baseline
scale_bounded = clip(scale_raw, 0.5, 2.0)
effective_atr = baseline * scale_bounded
distance = tick_round_outward(coefficient * effective_atr)
Baseline must be positive and adequately sampled. Do not apply another volatility multiplier on top of effective_atr. Return NOT_READY when ATR/baseline are unavailable. An extreme-volatility pause takes precedence over clipping.
A provisional source-distance mapping is 1 source dollar -> 1 effective ATR; 0.50 -> 0.50 ATR; 1.50 -> 1.50 ATR. This is a dimensionless translation hypothesis, not financial equivalence. Each strategy owns its coefficients and validation. Record the mapping and compare against legacy and fixed-ATR baselines.
Where a tolerance would round to zero, apply an explicit minimum of one tick. For protective stops, round away from entry; for targets, use the configured conservative rounding convention. Check the resulting prices obey direction, minimum distance, and broker order rules. Avoid Python's generic round for trading tick quantization; use decimal/integer tick arithmetic.
Keep structural stop placement intact: a buffer goes beyond the invalidation level, not inside the pattern to manufacture a smaller position risk. If the structurally valid stop is too distant or costs consume the opportunity, skip the trade.
6. Regime-to-strategy policy
State	Trend	Regular MA	Far MA	Base	Double
Directional	Allow aligned setups	Allow aligned pullbacks	Block countertrend entry until reversal state confirms	Allow aligned breakout	Require confirmed reversal; a pivot alone is insufficient
Range	Pause trend method	Allow confirmed level interaction	Allow confirmed extension reversal	Arm consolidation; require breakout confirmation	Allow confirmed reversal
Transition	Require stronger confirmation	Require stronger confirmation	Require stronger confirmation	Arm but require two completed confirmation bars	Require stronger confirmation
Extreme volatility / poor data / unacceptable costs	Pause	Pause	Pause	Pause	Pause


Define “stronger confirmation” explicitly per strategy in its config/state machine; initial implementation uses two completed directional confirmation bars instead of one. Never allow this shorthand to bypass the strategy's original mandatory predicates. Aligned means side agrees with the directional EMA slope and stack. Track all rejection reasons.
Timing defaults: entry expiry 15 minutes; pattern separation minimum 30 minutes; base formation minimum 30 minutes. Do not compress these source minima automatically. Candidate hold-time mapping may use 10 minutes in elevated volatility and 15 in normal/low volatility, bounded by session close and the configured 15-minute hard maximum. This is a research interpretation, not an assertion that the sheet's entry timing implies a hold limit. Freeze the selected expiry and timeout on signal creation.
Liquidity gate: require complete cost estimates; reject when spread/effective ATR exceeds configured ceiling (initial test hypothesis 0.15). Require target-one gross expected price travel to exceed three estimated round-trip price-equivalent costs. This is a cost viability filter, not an expected-profit forecast. Quote-based and OHLC-estimated costs must be separately labelled.
7. Risk stays controlled as distances change
Default risk budget stays at the existing 0.5% of equity. Preserve any explicitly configured lower account limit. The adaptation layer may reduce risk or disable entry; it cannot increase it beyond the configured base budget. Any separate account allocator may approve less risk or reject a signal.
budget = min(strategy_budget, externally_approved_budget_if_supplied)
cash_loss_per_unit = abs(entry - stop) * multiplier * fx_conversion
                     + conservative_stop_slippage_cash + round_trip_fees_cash
quantity = floor_to_step(budget / cash_loss_per_unit)
Validate available buying power/margin, min quantity, max quantity and partial-exit feasibility. Missing buying-power information permits labelled research simulation only, not actionable execution. Larger stop distance reduces quantity. Skip if minimum tradable size breaches budget. For two partial exits requiring two lots/contracts, default is to skip insufficient size; single-exit behavior requires a separately named configuration.
At signal creation freeze the profile, regime, coefficients, levels, risk budget and intended schedule. If it expires or is cancelled, a replacement is a new signal ID. Never widen a live stop when volatility rises. Any implemented trailing stop may only tighten; target and timeout changes after fill are forbidden in the initial version. A risk budget is not a guaranteed maximum realized loss: gap/slippage scenarios must be reported.
8. Repair execution/data issues before judging adaptive results
Audit the existing prototype, not just its four tests. Specifically:
- Match order type to intended entry. A buy limit above current ask is marketable; a breakout above price requires buy stop logic. Distinguish market, limit and stop entries explicitly. Do not use generic level-touch code for every setup.
- Establish one event order: bar closes, features and signal become available, broker latency applies, then future executable prices may fill. No same-bar retrospective fill. Remove accidental extra one-minute latency or make it an explicit setting. Test right-edge resampling and closed-bar availability.
- Distinguish scheduled market closure/breaks from missing data. Support overnight futures/FX sessions and DST; do not reject an expected intraday break simply because the gap occurs on the same UTC date.
- Cancel pending orders at session boundaries. Ensure unexpectedly missing end-of-session bars cannot leave positions silently carried overnight; flag unpriceable liquidation rather than inventing a close.
- Correct one-minute spread/slippage and target fills so limit orders never violate their limit. Model adverse selection separately from illegally worse limit prices. Stop gaps can fill worse.
- Use bid/ask when available. With OHLC ambiguity retain conservative stop-first handling and report its frequency. Never infer queue priority from a touched candle.
- Seed indicators consistently and test against independently calculated known values. Exclude incomplete candles and any resample with missing expected components.
- Handle empty datasets, insufficient history, zero-range bars, zero volume, nonfinite/negative configuration, negative futures prices where valid, currency conversion failures and insufficient equity.
9. Outputs and reproducibility
Every run writes under a unique directory including strategy ID, symbol, config hash, data hash and run ID. Save:
- indicators.parquet or CSV: raw features, ATR, profile baseline, regime, liquidity/volatility flags, adaptive distances.
- signals.jsonl: accepted and rejected decisions with reason codes; actionable signals include side, order type, entry/stop/targets, expiry, quantity, risk, profile/config versions and source bar availability time.
- trades.csv, equity.csv, metrics.json and run_manifest.json.
- Diagnostics split by strategy, instrument, session and regime, including a no-trade result.
Expose a stable evaluate(closed_data, instrument, config, state) -> decision, state interface. Persist strategy state where needed; deterministic replay must reproduce the same decisions after restart. Ensure the indicator and strategy share the same feature calculation rather than computing inconsistent copies.
Provide CLI examples to run any method independently in static, fixed-ATR and adaptive modes, plus a deterministic synthetic demonstration that works without paid data or credentials. Synthetic demonstrations must be clearly marked and must not be presented as performance evidence.
10. Tests and validation gates
Required tests:
1. Causality: prefix invariance; perturb future prices/volume and verify earlier signals/profiles/regimes do not change; profiles exclude the current session.
2. Correctness: independent indicator reference values; pip/tick/multiplier conversion; account-currency conversion; directional price rounding; lot rounding; costs and stop gaps.
3. Adaptation: same geometric setup at different volatility scales produces proportional distances within bounds; increased stop distance cannot increase quantity at constant budget; hysteresis prevents single-bar state flips; invalid data pauses immediately.
4. Trade immutability: live stop never widens; targets/budget/profile remain frozen after signal creation; state persists through restart.
5. Structure: setup cannot pass merely because adaptation changed a distance; mandatory predicates still hold. Test bullish/bearish and near-miss examples for all five methods.
6. Execution: stop/limit distinction, partial fills policy, stop/target ambiguity, session close, overnight session and DST, scheduled breaks, no data and final candle behavior.
7. Isolation: modifying one strategy's parameters changes no other strategy's fixture results.
Performance research: chronological walk-forward training/validation/test splits, with an embargo at least as long as maximum position/pending-order horizon at split boundaries. Indicator warmup may use older data; future outcomes may not. Fit profile parameters and selection thresholds only on permitted past data. Compare legacy static, fixed-ATR and adaptive variants using identical execution assumptions and valid time ranges. Include costs at base and stressed levels; report multiple tested variants to expose selection bias.
KPIs: net expectancy per completed trade and in R, net profit factor, marked-to-market drawdown, trade count, rejected-signal rate by reason, exposure, holding time, spread/slippage burden, turnover, out-of-sample degradation, per-regime concentration, and sensitivity to nearby parameters. Report uncertainty/sample sizes; a higher win rate alone is not acceptance. Do not claim an adaptive improvement without sufficient held-out data. If only synthetic data is available, complete engineering validation and report performance validation as pending.
11. Delivery sequence
1. Refactor independent strategy modules, formalize data/order contracts and fix execution defects; verify parity with documented legacy behavior except explicitly recorded bug fixes.
2. Implement instrument/session profiles, causal feature calculations, hysteretic regimes and bounded policy; connect the indicator exports and each strategy.
3. Complete causal, execution, cross-asset and isolation tests; deliver reproducible CLI and synthetic example.
4. Produce validation report and user instructions; run real-data walk-forward research only if suitable data is available. Keep broker live-order execution out of this research delivery.
Finish with the file tree, commands to run, test results, exact deviations from the photograph, verified data sources and remaining limitations. Do the implementation; do not return only an architecture proposal.
Appendix A — Exact file construction contracts for the adaptive upgrade
A1. Build order and complete inventory
First reconstruct Appendix B into an empty directory or inspect the existing ZIP project. Commit/checkpoint that baseline. Then build the files below in dependency order: packaging and contracts; instrument/data/session primitives; indicators; adaptation; strategies; execution; reporting/CLI; tests/docs. Do not leave pass, TODO, NotImplementedError, fabricated validation results, or nonfunctional example commands in delivered required files. Unknown real instrument metadata belongs in an explicitly invalid example configuration that fails closed, not a guessed production default.
Every directory under src/scalping is a Python package and gets __init__.py. These initializer files contain only a short module docstring and public exports; no data download, file writes, or CLI execution at import. strategies/*/__init__.py exports its own strategy class and config schema only. No namespace package ambiguity.
File	Required implementation and contract
pyproject.toml	Setuptools build with src discovery; Python >=3.10; numpy >=1.26,<3, pandas >=2.2,<4, PyYAML >=6,<7; optional dev pytest >=8,<10. Console command scalping = scalping.cli:main. Include strategy YAML configs as package data. No need for parquet dependencies when default output is CSV.
requirements.txt	Compatibility installer containing -e .[dev]; document that commands run from project root. Resolve/pin a tested lock or constraints file for the delivered environment and record versions.
src/scalping/contracts.py	Frozen dataclasses/enums below; configuration validation; stable serialization. No strategy logic.
src/scalping/instrument.py	Load validated instrument schema, decimal tick rounding, quantity rounding, P&L conversion, eligibility and margin checks. Export load_instrument(path), round_price(price, tick, direction), round_quantity(quantity, step), cash_per_price_unit(instrument, conversion).
src/scalping/data.py	load_bars(path, instrument) and closed_resample(bars, timeframe, as_of, session_calendar); require aware timestamps or explicit timezone; explicit input price basis; OHLCV checks; missing expected bar detection; completed-bar mask. Preserve bar open and available-at timestamps separately.
src/scalping/sessions.py	SessionCalendar loads explicit UTC session intervals with session IDs, breaks and named display timezone; session_at(ts), expected_bar_opens(start,end), next_close(ts); fixture calendars test DST and overnight sessions. Real calendars must be supplied/verified; do not infer holidays from weekdays alone.
src/scalping/indicators.py	Pure causal functions for EMA, ATR, RSI, ADX, stochastic K/D, session VWAP and compute_features_input(bars, config). Document Wilder seeds and NaN policy; insufficient history stays unavailable. VWAP resets on session ID, not UTC date.
src/scalping/adaptation/profile.py	build_profile(completed_sessions, instrument, cutoff, config) -> InstrumentProfile; exclude current session and anything available after cutoff; compute same-session-bucket medians and counts. Serialize immutable profile plus hash.
src/scalping/adaptation/features.py	compute_behavior_features(closed_bars, profile, quotes, as_of); calculations in section 4; return known/unknown quality flags; no future interpolation for quotes/conversion/activity.
src/scalping/adaptation/regime.py	update_regime(features, prior_state, config) -> RegimeState; implement thresholds, counters, hysteresis and immediate quality pause. Direction sign is separate from volatility/liquidity flags. Persist counters.
src/scalping/adaptation/policy.py	resolve_policy(strategy_id, features, profile, regime, config) -> PolicySnapshot; bounded ATR distances, allowed directions, confirmation count, frozen timeouts and rejection codes. No indicator recomputation or P&L optimization.
src/scalping/adaptation/calibration.py	Offline chronological split runner, grid supplied by config, training-only selection, embargo, immutable calibrated config export and comparison manifest. Require explicit target metric; preserve every attempted candidate. With no real data, run engineering fixture only and label performance pending.
src/scalping/execution/costs.py	Estimate spread, slippage and fees in price and account currency with timestamps and provenance. Enforce limit-price bounds. Distinguish price basis mid/bid/ask/trade; prevent double-counting spread. Missing essential estimates fail actionable mode.
src/scalping/execution/sizing.py	size_order(proposal, account, instrument, costs, conversion) -> SizingDecision; section 7 formula, risk/margin/quantity constraints and reason codes; no trades sent.
src/scalping/execution/simulator.py	Explicit event-driven single-symbol backtest: pending orders, fills, position, partial exits, fees, stops, session close, liquidation uncertainty and equity. Process available signals before the next eligible execution event, respecting configured latency. Stateful streaming and batch replay agree.
src/scalping/reporting.py	Write all section 9 outputs, JSON-safe nulls for undefined metrics; metrics computed from completed net trades; include initial equity in drawdown high-water mark, including an initial losing trade. No generated output overwrites another run.
src/scalping/cli.py	Subcommands run, compare, validate-config; static strategy registry imports classes without side effects; argument/config precedence documented. main(argv=None) returns exit code; invalid configs/non-ready actionable run fail nonzero.
engine.py	Compatibility adapter for the old imports; preserve frozen legacy engine under legacy/engine.py rather than silently changing its semantics. Upgraded CLI uses new package. Label legacy mode explicitly in outputs.
run.py	Compatibility main(default=None) translates original --strategy, --data, --config, --output, --equity into modern CLI arguments; require an explicit modern instrument config for adaptive mode, never guess from generic asset-class JSON.
trend.py, regular_ma.py, far_ma.py, base.py, double.py	Thin standalone wrappers selecting only their named strategy through run.main; independently executable after package installation.
stocks.json, forex.json, futures.json	Preserve exact legacy examples for legacy mode; header-free valid JSON. Document limitations. Modern metadata lives in configs/instruments and these examples cannot bypass validation.
test_engine.py	Preserve legacy regression suite and label its limited scope. Full acceptance resides in tests.
README.md	Installation, reconstruction, each independent command, data examples, assumptions, source deviations, calibration and output interpretation; state adaptive implementation status accurately.
docs/SOURCE_RULES.md	The source matrix from section 2 plus all explicit decisions in A3 below; cite photograph as supplied reference, distinguish observed rules from engineering interpretation.
docs/ADAPTATION_RULES.md	Feature formulas, seeds, units, bounds, state transition priority, missing data, freezing and calibration; list all configuration defaults.
docs/DATA_CONTRACT.md	Example CSV rows, timestamp semantics, price basis, calendar, corporate actions, futures rolls, quotes/FX conversion and failure modes.
docs/VALIDATION_REPORT.md	Actual executed commands, environment, tests and failures/fixes; synthetic vs real data; comparison results only if run, otherwise explicit pending status.
examples/generate_synthetic_data.py	Deterministic seed, >=25 synthetic full sessions, multiple volatility/regime segments, valid OHLC, quotes and session manifest; write a synthetic instrument config. Produce output compatible with documented CLI. Never label artificial series as downloaded market data.


Create legacy/README.md explaining that Appendix B is frozen and unsuitable for performance assertions without its documented corrections. The legacy directory can contain the 13 embedded baseline files verbatim. Do not ship caches or secrets.
A2. Typed interfaces and configuration schema
Use frozen dataclasses for immutable records, validated enums for asset class/order type/regime, timezone-aware UTC datetimes and Decimal or integer ticks for order arithmetic. Float arrays are acceptable for indicators. Required records:
- InstrumentSpec: symbol, venue, asset_class, tick_size, quantity_step, min_quantity, max_quantity, multiplier, pnl_currency, account_currency, timezone, session_calendar_path, price_basis, volume_kind, short_allowed, supported_orders, metadata_as_of, synthetic flag; optional lot_size, expiry, margin_per_unit and dated equity eligibility manifest path.
- Bar: open_time, close_time, available_at, open/high/low/close, volume or unknown, session_id, completeness flag.
- InstrumentProfile: profile_id/hash, instrument_id, version, cutoff, completed session IDs, per-bucket baseline ATR/spread/activity medians/counts and readiness flags.
- RegimeState: label, directional_sign, candidate_label, consecutive_count, volatility_flag, liquidity_flag, changed_at, quality_reasons.
- PolicySnapshot: policy_id, mode, profile_id, regime, effective_atr, each resolved distance in ticks, allowed_sides, confirmation_bars, expires_after, hold_timeout, risk_multiplier <=1, ready, reasons.
- StrategyState: strategy_id/version, last_processed_bar_id, phase, candidate setup data, confirmation count, candidate first/second pivot IDs as applicable, frozen policy snapshot, last emitted signal ID. JSON serialize/restore deterministically.
- SignalProposal: signal_id, strategy_id/version, instrument_id, decision_time, source_available_at, side, order_type, entry/trigger/limit as applicable, structural_invalidation, stop, target prices and fractions, expiry, max_hold, policy_id, source evidence and reasons. No fabricated quantity before sizing.
- AccountSnapshot: timestamp, equity, account_currency, available_buying_power or unknown, approved_cash_risk or unknown, source.
- SizingDecision: accepted, quantity, planned_cash_risk, required_margin, costs, reasons.
- Decision: accepted/rejected/not_ready, proposal or null, reasons and diagnostic features.
Strategy interface: evaluate(context, state) -> (Decision, StrategyState), where context contains closed setup/execution bars, current instrument, feature snapshot, resolved policy and immutable strategy config. It never accepts future bars without as-of filtering. Same input + state yields same output. Use stable signal IDs from strategy/config/instrument/source timestamps; repeated delivery must not duplicate orders.
Create configs/adaptation.yaml with every value from sections 4–6 explicitly named: enabled mode; 20 profile sessions; 10 bucket observations; 30-minute buckets; ATR14; ER20; slope lag5; directional enter/exit and range enter/exit thresholds; persistence3; volatility thresholds0.75/1.5/2.5; scale bounds0.5/2; max spread ratio0.15; min target/cost ratio3; session-quality requirements. Configuration validation rejects overlapping/invalid thresholds and nonfinite inputs.
Create these fully populated synthetic example instrument configs: configs/instruments/DEMO_STOCK.yaml, DEMO_FX.yaml, DEMO_FUTURE.yaml, with explicitly fake venue SYNTHETIC, known synthetic calendar and account-currency conversions. Use stock tick0.01/multiplier1/quantity_step1; FX tick0.00001/multiplier1/quantity_step1000 in base units; futures tick0.25/multiplier50/quantity_step1. Synthetic stock eligibility is allowed only in synthetic mode. Do not present these as validated specifications of actual instruments. Include a strict real-instrument template with required-field explanations in docs; no invented live data.
Every strategies/<id>/config.yaml includes schema_version, strategy_id/version, setup_timeframe, execution_timeframe, mode, EMA/oscillator periods, coefficients, required predicates, confirmation count, expiry, max_hold, targets/fractions, risk_fraction0.005, risk_cap0.02, pyramiding=false, partial_size_policy=skip, and adaptation override bounds. YAML must include the specific default coefficients below. Reject unknown keys and invalid quantities, fractions or periods. Strategy-specific overrides cannot override instrument tick, contract value or hard risk cap.
A3. Deterministic per-strategy implementation contract
For every strategy directory create:
1. strategy.py: its own validated Config dataclass, Strategy class, evaluate state machine and pure predicate helpers. It owns its setup logic, imports shared primitives only, and implements all below.
2. config.yaml: defaults below plus common schema fields above.
3. cli.py: main(argv=None) delegates to shared CLI with a fixed strategy ID; callable through python -m scalping.strategies.<id>.cli.
4. __init__.py: exports config and strategy.
Defaults below govern the new adaptive implementation and supersede conflicting interpretations in the embedded legacy source. Distances are coefficient * effective_atr. Half targets use 50%/50%, rounded quantities; stop and session/time exits handle all remaining size. One candidate per strategy/symbol, no pyramids. Evaluate short rules symmetrically unless short eligibility blocks them. Require regime and cost policy approval after mandatory setup predicates pass.
Strategy	Explicit default logic to implement
trend	Setup 5min, execution1min. Long EMA9>15>30, with EMA9 crossing above15 on current completed setup bar; short inverse. Spread of those EMAs <=0.50 ATR. Freeze a pullback limit at EMA9, protective stop beyond EMA30 by0.50 ATR; long limit must be below current ask (short above bid) or reject NO_PULLBACK_LIMIT. Targets +1/+2 ATR from planned entry. Existing open trade does not re-enter on repeated signals. This is a versioned interpretation, not automatic reconstruction of source ladders.
regular_ma	Setup5min; configurable15/30 as separate versioned runs, not a vote across unfinished charts. Define trend-aligned pullback by EMA15>30 and close>selected EMA65/200/VWAP for long, inverse short. Candidate level must be behind price in pullback direction, with distance between1 and1.50 ATR (bounded approach zone). Choose nearest eligible level deterministically; ties EMA65, EMA200, VWAP. Long stochastic<50, short>50, then one closed execution candle in trade direction; freeze limit at level, stop0.50 ATR beyond. Targets +0.50/+1.50 ATR. In range regime permit level interaction without15/30 alignment, but still require correct side of level and candle confirmation. This explicitly resolves the ambiguous old code that could put a buy limit above market.
far_ma	Setup5min. Long close below nearest of EMA9/15/30/65/200 by >=1.50 ATR, RSI<=20 and stochastic<20; short inverse with RSI>=80/stochastic>80. Arm candidate, freeze structural stop beyond setup low/high by0.50 ATR. On later closed1min candle with directional body (close>open for long, <open for short), set buy stop one tick above that candle's high (sell stop below low). Transition policy requires two consecutive directional closed bars. This chooses that completed candle as the prior bar for the upcoming breakout. Setup invalidates if structural stop breached, expiry reached, or price touches nearest MA before entry; no retroactive fill on confirmation bar. Targets +0.50/+1.50 ATR from trigger; reject if setup mean is too close to justify target1 after costs. Strong-directional countertrend trades stay blocked until regime changes and reversal confirms.
base	Setup5min. Prior six completed bars (exclude candidate breakout bar) span >=30min and total high-low<=0.50 ATR, using current closed-bar effective_atr for that decision. Long breakout close above base high+one tick, short below low-one tick; directional candle required. Transition needs two consecutive closes beyond the frozen base boundary. Emit market entry for next eligible execution event, structural stop0.50 ATR beyond opposite base boundary. Targets +0.50/+1.50 ATR from actual fill using frozen distances; reject/re-size if slippage makes cost/risk conditions invalid. No hindsight base selection from future bars.
double	Setup5min. Strict three-bar pivot: central low lower than both neighbors (top higher); pivot only known after right-hand close. Two same-type pivots >=30min apart and <=120min apart; price difference<=0.50 ATR; intervening excursion>=1 ATR. Choose most recent valid first pivot deterministically. After second pivot confirmation require close beyond intervening neckline (maximum intervening high for bottom, minimum low for top); one closed confirmation or two in transition. Enter market at next eligible event. Structural stop beyond lower of bottoms/higher of tops by0.50 ATR; targets +0.50/+1.50 ATR from fill. Neckline confirmation replaces legacy blind pivot retest as a declared versioned decision.


For market orders only, target prices are anchored once at actual fill using the already frozen distances, then immutable. This is not post-entry adaptive re-optimization. Limit/stop entry targets remain as proposed; revalidate fill-to-target geometry and budget. For stop slippage beyond the allowed risk allocation, simulation records actual fill/risk breach, while any broker adapter must use supported guardrails; do not silently pretend the fill did not happen. No broker adapter is required here.
For all state machines explicitly define IDLE -> ARMED -> CONFIRMING -> ORDER_PROPOSED -> EXPIRED/INVALIDATED, with return to IDLE after terminal outcome and de-duplication of the same source setup. Execution owns pending/filled/closed position lifecycle. Regime disallowance cancels unfilled entry candidates; filled positions retain frozen exit/risk rules. Persist all state required to recover without duplicate trades.
A4. Minimum tests, file by file, and executable acceptance
File	Minimum independent assertions
tests/test_causality.py	Prefix equivalence, future perturbation invariance, no current session in profile, no unfinished HTF candles, confirmation timestamp before entry eligibility.
tests/test_adaptation.py	Scale bounds; not-ready baseline; correct threshold equality; three-bar persistence; immediate quality pause; no ATR double-scaling; frozen trade policy.
tests/test_instrument_math.py	FX quote/account conversion, contract multiplier, integer tick arithmetic, lot rounding, insufficient minimum size, wrong/stale currency metadata rejected.
tests/test_execution.py	Stop vs limit behavior, no limit-price violation, stop gap, both-exit touched, partial exits/fees, margin, initial-loss drawdown, session breaks/DST, empty data, idempotence/restart and ambiguous liquidation flag.
tests/test_strategy_isolation.py	Each method's config mutation affects only that method; import of one method requires no other strategy; no global mutable config.
tests/strategies/test_trend.py	Valid bull/bear cross + pullback, invalid stack/spread, marketable limit rejected.
tests/strategies/test_regular_ma.py	Correct approach side, deterministic nearest-level tie, unknown volume disables VWAP only, direction confirmation.
tests/strategies/test_far_ma.py	Setup alone cannot enter; one-minute breakout confirmation, target-mean rejection, invalidation and expiration.
tests/strategies/test_base.py	Six-bar base excludes breakout, narrow-range criterion, false breakout, confirmation count and gap-aware sizing.
tests/strategies/test_double.py	Pivot known only after right candle, min/max separation, excursion/tolerance, neckline, symmetric shorts, no duplicate candidate.


Required commands after implementation, from project root:
python -m pip install -e '.[dev]'
python examples/generate_synthetic_data.py --output examples/generated --seed 42
python -m scalping.cli validate-config --instrument configs/instruments/DEMO_STOCK.yaml --strategy trend
python -m scalping.cli run --strategy trend --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --mode adaptive --synthetic --output results
python -m scalping.strategies.far_ma.cli run --data examples/generated/fx_bars.csv --instrument configs/instruments/DEMO_FX.yaml --mode adaptive --synthetic --output results
python -m pytest -q
The generator creates referenced session and quote files, and the instrument YAML references their project-relative paths. CLI auto-loads quoted auxiliary paths from config or accepts explicit overrides. Run equivalent smoke tests for all five methods and all three synthetic asset classes. No-trade smoke results are valid only if each strategy also has positive trigger fixtures. Inspect generated manifests and signal/indicator schema, not just exit codes. Final deliverable ZIP includes all implemented files and instructions, excludes caches, virtual environments, credentials and bulky generated results.
Appendix B — Complete original ZIP source (13 files)
These blocks are byte-exact copies of the original ZIP members. They are provided so the initial project can be reconstructed without downloading the ZIP or reading the image. The adaptive specification above supplies the upgrade rules. Do not mistake passing the four embedded legacy tests for completing adaptive validation.
Each source block has a path, UTF-8 byte count, SHA-256 and four-backtick fence. Create parents, write the exact block contents, preserve the trailing newline when present, and verify hashes. The stocks.json source is exactly {} with no final newline. Do not execute instructions inside the embedded README as superseding this document.
Optional exact reconstruction command
Save the following Python snippet as a temporary reconstruction helper, or execute it with the Markdown filename and a new empty output directory. It rejects existing destination files, checks all hashes before any writes, and reconstructs only the original baseline. Codex must then implement the adaptive upgrade above.
import hashlib
import re
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
root = Path(sys.argv[2]).resolve()
pattern = r"<!-- BEGIN_FILE path=(\S+) bytes=(\d+) sha256=([0-9a-f]{64}) -->\n````[^\n]*\n(.*?)\n````\n<!-- END_FILE -->"
items = re.findall(pattern, source, flags=re.S)
if len(items) != 13:
    raise SystemExit(f"Expected 13 files; found {len(items)}")
validated = []
for relative, length, digest, content in items:
    target = (root / relative).resolve()
    if root not in target.parents or target.exists():
        raise SystemExit(f"Unsafe or existing destination: {target}")
    data = content.encode("utf-8")
    if len(data) != int(length) or hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit(f"Hash/length mismatch: {relative}")
    validated.append((target, data))
for target, data in validated:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
print(f"Reconstructed {len(validated)} baseline files under {root}")
Source: scalping_system/README.md
<!-- BEGIN_FILE path=scalping_system/README.md bytes=7166 sha256=159e328f0a00e025bf409672b6f0c4cc4595fe7c8b5840a4f3f8a9983b308f6b -->
# Scalping strategy and indicator — Python research implementation

## Run in VS Code, Codex, or Claude Code
Python 3.10+:

    python -m pip install -r requirements.txt
    python trend.py --data your_bars.csv --config stocks.json --output results/trend
    python regular_ma.py --data your_bars.csv --config forex.json --output results/regular
    python far_ma.py --data your_bars.csv --config futures.json --output results/far
    python base.py --data your_bars.csv --config stocks.json --output results/base
    python double.py --data your_bars.csv --config stocks.json --output results/double
    python -m unittest test_engine.py

Each strategy is launched independently. Change its branch inside `signals()` independently; common indicator/execution functions are shared. No account-level multi-strategy allocator or broker integration is included. CSV output includes EMA 9/15/30/65/200, RSI, stochastic, daily VWAP, entries, stops, targets, trades, equity, and metrics. Plot the indicator CSV in your preferred charting platform; this is not a TradingView/MetaTrader plugin.

## Data contract
CSV header: timestamp,open,high,low,close,volume
One-minute bars; timestamp means candle OPEN, UTC or an explicit UTC offset. Complete sorted unique bars; overnight gaps allowed but intraday missing bars rejected. Supply at least 1,005 minutes of history for 200 completed five-minute EMA bars and a signal. Five-minute candles are used for setup indicators. Orders are scheduled after the setup candle closes and may fill starting one minute later. Never feed an unfinished last candle. Feed executable instrument prices, not SPX index prices for an ES contract. Handle stock splits, futures rolls, exchange calendars, holidays, and broker-specific FX sessions upstream.

VWAP requires meaningful volume. FX tick volume is a proxy, not consolidated traded volume; set volume to zero to disable VWAP candidates if unavailable. Other indicators remain usable.

## What the photograph says and what this code implements
The source is a historical handwritten/typed stock strategy sheet, not proof of an edge. Dollar thresholds do not transfer economically to all instruments. This implementation makes each dollar distance `distance_unit` units of instrument price. These defaults are examples, not calibrated trading parameters.

| Method | Implemented interpretation | Differences / unresolved source rules |
|---|---|---|
| Trend | 9 crosses 15 with ordered 9/15/30 stack, total separation <= 0.50 units; limit at 9; stop beyond 30; two fixed targets | Sheet says no stop and add every 0.50; replaced with a fixed stop and no adds. Hold-for-day and 15 EMA trailing exit are not implemented. |
| Regular MA | Price >= 1 unit from 65/200 EMA or VWAP; stochastic below/above 50; limit at nearest qualifying level; stop 0.50 beyond | Photo also mentions 15/30 charts and “65 or 200”; only five-minute chart implemented. RSI threshold wording is unclear and not imposed. |
| Far MA | >= 1.50 from 65 EMA; RSI <=20/>=80 and stochastic <20/>80; signal-close entry; stop beyond signal candle | Source switches from 5-minute to 1-minute green/red confirmation and previous-bar breakout; that refinement is NOT implemented. |
| Base | Prior 30 minutes range <=0.50; close breaks range; signal-close entry; stop beyond opposite range edge | “At base” is interpreted as confirmed breakout. No additional 15/30-minute confirmation. |
| Double top/bottom | Confirmed local pivots >=30 minutes apart, within 0.50, with >=1 unit intervening excursion; limit retest of second pivot | These are explicit algorithmic definitions of a subjective pattern, and pivot confirmation delays entry. |

All methods: half quantity exits at +0.50 and +1.50 units, except trend at +1 and +2. Sheet offers alternative target distances; these choices are configurable by editing the corresponding method. Sheet's “always ... within 15 minutes” is interpreted as pending order expiry and maximum position duration. Fixed stops do not ratchet or pyramid. The daily slow-day momentum, far-MA multi-day reversal, and blank weekly/daily-level rows are excluded from this scalping request.

## Cross-asset configuration
- Stocks: price units, tick 0.01, point_value 1 per share; shorting availability and borrow costs must be modeled externally. No stock universe scan is included; provide mega/large-cap blue-chip input data.
- FX example: distance unit 0.0001 (one pip), tick 0.00001, quantities in base currency units. `point_value=1` assumes quote currency matches account currency (e.g. EUR/USD in USD account). USD/JPY and crosses require current currency conversion; do not use this preset unchanged.
- Futures example: tick 0.25 and point_value 50 represent an ES-like contract. Verify your actual contract specification, margin, and session. Scaling out requires at least two contracts; smaller sizes are skipped.

Set distance_unit deliberately: 1 pip is not equivalent to a stock dollar in volatility or fees. Tune only on training data, then freeze settings before out-of-sample evaluation. Quantities are capped by risk and max_quantity, but there is no margin/buying-power or liquidation model. Account risk_fraction defaults to 0.5%, capped at 2%; this is a configurable ceiling, not a prop-firm rules engine. Sessions use configurable timezone and same-day start/end; overnight sessions spanning midnight are not supported. Session_end 23:59 deliberately leaves one closing minute in the FX example.

## Backtest assumptions and limitations
One symbol and one position at a time. Limit fills use bar touch, not queue priority. Spread and slippage apply adversely on both entry/exit; commission applies per unit per side. Stop gaps fill at worse opening price. When a bar touches stop and target, stop is evaluated first (conservative unknown intrabar ordering). Half quantities round down to lot step. Insufficient size for two exits skips entry. The model cannot establish a limit fill or stop ordering from OHLC data; validate against bid/ask and tick replay. Last bar closes any position; end-of-session and time exits use bar close. No overnight position holding. Equity uses unrealized price P&L; drawdown is bar-close, not intrabar. Profit factor is null when no losing trades exist, rather than claiming infinity.

## Execution roadmap and acceptance gates
1. Confirm the interpretations in the table, especially far-MA 1-minute refinement and trend exits.
2. Import real one-minute data for each chosen symbol; verify timestamps, units, tick sizes, conversion, volume and session.
3. Backtest each strategy separately, then walk-forward test with frozen parameters and stressed costs. Track net expectancy, profit factor, drawdown, fill rate, trade count and out-of-sample stability. No profitability has been established by this package.
4. Paper trade with broker fills and compare signals/fills/slippage; add a separate account allocator and broker adapter only after reconciliation. Enforce buying power, correlated exposure and account loss limits there. Codex/Claude can implement these next steps using this README; never infer unspecified broker rules.

<!-- END_FILE -->

Source: scalping_system/base.py
<!-- BEGIN_FILE path=scalping_system/base.py bytes=65 sha256=ac74facac8b71bb2b1c35de34689f813b1fafed37fc9c8af509d888a9f72ab60 -->
from run import main
if __name__ == "__main__":
    main("base")

<!-- END_FILE -->

Source: scalping_system/double.py
<!-- BEGIN_FILE path=scalping_system/double.py bytes=67 sha256=735f26ca6bc6f98e3f1f2a1a16eb795b19f072c06cd8526cdf009c9cf470a083 -->
from run import main
if __name__ == "__main__":
    main("double")

<!-- END_FILE -->

Source: scalping_system/engine.py
<!-- BEGIN_FILE path=scalping_system/engine.py bytes=11466 sha256=a2215ee5c35372786e8b7412fad5cc12b9556b786557cb0af3da0210ed603bc9 -->
"""Closed-bar indicators and independent scalping signals. No broker orders."""
from dataclasses import dataclass
import math
import numpy as np
import pandas as pd

@dataclass
class Config:
    tick_size: float = .01
    distance_unit: float = 1.0  # price units per sheet dollar; FX can use a pip
    point_value: float = 1.0  # account currency per 1.0 price move per quantity
    quantity_step: float = 1.0
    max_quantity: float = 1000
    risk_fraction: float = .005
    spread: float = 0.0
    slippage_ticks: float = 1.0
    commission: float = 0.0  # per quantity, per side
    timezone: str = 'America/New_York'
    session_start: str = '09:30'
    session_end: str = '16:00'
    base_minutes: int = 30
    base_width: float = .50
    pattern_minutes: int = 30
    pattern_tolerance: float = .50
    max_hold_minutes: int = 15
    rsi_period: int = 14
    stoch_period: int = 14
    stoch_smooth: int = 3
    def __post_init__(self):
        for name in ('tick_size','distance_unit','point_value','quantity_step','max_quantity'):
            if not math.isfinite(getattr(self,name)) or getattr(self,name) <= 0:
                raise ValueError(name + ' must be positive and finite')
        if not 0 < self.risk_fraction <= .02:
            raise ValueError('risk_fraction must be in (0, .02]')
        if min(self.spread,self.slippage_ticks,self.commission) < 0:
            raise ValueError('Costs cannot be negative')


def load_csv(path):
    d = pd.read_csv(path)
    d.columns = d.columns.str.lower()
    d['timestamp'] = pd.to_datetime(d['timestamp'], utc=True)
    d = d.set_index('timestamp')
    validate(d)
    return d


def validate(d):
    if not isinstance(d.index,pd.DatetimeIndex) or d.index.tz is None:
        raise ValueError('Timezone-aware DatetimeIndex required')
    if not d.index.is_monotonic_increasing or d.index.has_duplicates:
        raise ValueError('Bars must be sorted and unique')
    if not {'open','high','low','close','volume'} <= set(d.columns):
        raise ValueError('OHLCV required')
    a=d[['open','high','low','close','volume']]
    if not np.isfinite(a.to_numpy()).all() or (a.volume<0).any():
        raise ValueError('Invalid OHLCV values')
    if ((d.high < d[['open','close','low']].max(axis=1)) | (d.low > d[['open','close','high']].min(axis=1))).any():
        raise ValueError('Invalid candle bounds')
    if len(d)>1 and (d.index.to_series().diff().dropna()!=pd.Timedelta(minutes=1)).any():
        # Overnight/session gaps are allowed; missing in-session bars are not.
        same=d.index.to_series().dt.date.eq(d.index.to_series().shift().dt.date)
        gaps=d.index.to_series().diff()>pd.Timedelta(minutes=1)
        if (same & gaps).any():
            raise ValueError('Missing intraday bars: supply complete one-minute sessions')


def indicators(d,c):
    x=d.copy()
    for n in (9,15,30,65,200):
        x[f'ema{n}']=x.close.ewm(span=n,adjust=False,min_periods=n).mean()
    delta=x.close.diff(); up=delta.clip(lower=0); down=-delta.clip(upper=0)
    gain=up.ewm(alpha=1/c.rsi_period,adjust=False,min_periods=c.rsi_period).mean()
    loss=down.ewm(alpha=1/c.rsi_period,adjust=False,min_periods=c.rsi_period).mean()
    x['rsi']=100-100/(1+gain/loss.replace(0,np.nan))
    x.loc[(loss==0)&(gain>0),'rsi']=100
    x.loc[(loss==0)&(gain==0),'rsi']=50
    lo=x.low.rolling(c.stoch_period).min(); hi=x.high.rolling(c.stoch_period).max()
    x['stoch']=(100*(x.close-lo)/(hi-lo).replace(0,np.nan)).rolling(c.stoch_smooth).mean()
    dates=x.index.tz_convert(c.timezone).date
    pv=((x.high+x.low+x.close)/3*x.volume).groupby(dates).cumsum()
    vv=x.volume.groupby(dates).cumsum()
    x['vwap']=pv/vv.replace(0,np.nan)
    return x


def signals(d,c,method):
    validate(d)
    if method not in ('trend','regular_ma','far_ma','base','double'):
        raise ValueError('Unknown method')
    # Input timestamps denote bar OPEN. Resampled labels denote bar CLOSE.
    bars=d.resample('5min',label='right',closed='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
    counts=d.close.resample('5min',label='right',closed='left').count()
    bars=bars.loc[counts==5]
    x=indicators(bars,c); u=c.distance_unit
    x['side']=0; x['limit']=np.nan; x['stop']=np.nan
    x['target1']=np.nan; x['target2']=np.nan
    for i in range(200,len(x)):
        r=x.iloc[i]; prev=x.iloc[i-1]; side=0; entry=stop=t1=t2=np.nan
        if method=='trend':
            long=r.ema9>r.ema15>r.ema30 and prev.ema9<=prev.ema15
            short=r.ema9<r.ema15<r.ema30 and prev.ema9>=prev.ema15
            # Crossing the 9/15 with a full ordered stack defines a new trend.
            side=1 if long else -1 if short else 0
            if side and max(r.ema9,r.ema15,r.ema30)-min(r.ema9,r.ema15,r.ema30)<=.5*u:
                entry=r.ema9; stop=r.ema30-side*.5*u; t1=entry+side*u; t2=entry+side*2*u
            else: side=0
        elif method=='regular_ma':
            for s in (1,-1):
                levels=[r.ema65,r.ema200,r.vwap]
                valid=[v for v in levels if np.isfinite(v) and s*(v-r.close)>=u]
                if valid and (r.stoch<50 if s==1 else r.stoch>50):
                    side=s; entry=min(valid,key=lambda v:abs(v-r.close)); stop=entry-s*.5*u
                    t1=entry+s*.5*u; t2=entry+s*1.5*u; break
        elif method=='far_ma':
            for s in (1,-1):
                extreme=(r.rsi<=20 and r.stoch<20) if s==1 else (r.rsi>=80 and r.stoch>80)
                if s*(r.ema65-r.close)>=1.5*u and extreme:
                    side=s; entry=r.close; stop=(r.low-.5*u if s==1 else r.high+.5*u)
                    t1=entry+s*.5*u; t2=entry+s*1.5*u; break
        elif method=='base':
            n=max(1,math.ceil(c.base_minutes/5)); hist=x.iloc[i-n:i]
            if len(hist)==n and hist.high.max()-hist.low.min()<=c.base_width*u:
                if r.close>hist.high.max(): side=1; entry=r.close; stop=hist.low.min()-.5*u
                elif r.close<hist.low.min(): side=-1; entry=r.close; stop=hist.high.max()+.5*u
                if side: t1=entry+side*.5*u; t2=entry+side*1.5*u
        else:
            # Confirm second pivot only once its right-hand candle has closed.
            j=i-1
            for s in (1,-1):
                col='low' if s==1 else 'high'
                pivot=x.iloc[j][col]
                if not (pivot<x.iloc[j-1][col] and pivot<r[col] if s==1 else pivot>x.iloc[j-1][col] and pivot>r[col]): continue
                for k in range(j-math.ceil(c.pattern_minutes/5),1,-1):
                    if k<1: break
                    first=x.iloc[k][col]
                    is_pivot=(first<x.iloc[k-1][col] and first<x.iloc[k+1][col]) if s==1 else (first>x.iloc[k-1][col] and first>x.iloc[k+1][col])
                    middle=x.iloc[k+1:j]
                    if is_pivot and abs(first-pivot)<=c.pattern_tolerance*u and len(middle) and ((middle.high.max()-first>=u) if s==1 else (first-middle.low.min()>=u)):
                        side=s; entry=pivot; stop=pivot-s*.5*u; t1=entry+s*.5*u; t2=entry+s*1.5*u; break
                if side: break
        if side and side*(entry-stop)>0:
            # Quantize order prices to valid ticks.
            vals=[round(v/c.tick_size)*c.tick_size for v in (entry,stop,t1,t2)]
            if side*(vals[0]-vals[1])>0:
                x.loc[x.index[i],['side','limit','stop','target1','target2']]=[side,*vals]
    return x


def backtest(d,sig,c,equity=10000):
    """One position; next-bar fills; stop-first ambiguity; fixed stops; no pyramids."""
    cash=equity; position=None; pending=None; trades=[]; curve=[]
    slip=c.slippage_ticks*c.tick_size+c.spread/2
    for ts,r in d.iterrows():
        local=ts.tz_convert(c.timezone); clock=local.strftime('%H:%M')
        in_session=c.session_start<=clock<c.session_end
        if not in_session: pending=None
        if pending and ts>pending['expires']: pending=None
        if not position and pending and in_session:
            q=pending; side=q['side']; level=q['limit']
            touched=r.low<=level if side==1 else r.high>=level
            if touched:
                raw=min(r.open,level) if side==1 else max(r.open,level)
                entry=raw+side*slip
                risk=side*(entry-q['stop'])
                if risk>0 and side*(q['target1']-entry)>0:
                    unit_risk=(risk+slip)*c.point_value+2*c.commission
                    qty=math.floor(min(cash*c.risk_fraction/unit_risk,c.max_quantity)/c.quantity_step)*c.quantity_step
                    if qty>=2*c.quantity_step:
                        cash-=qty*c.commission
                        position={**q,'entry':entry,'qty':qty,'initial_qty':qty,'entered':ts,'pnl':-qty*c.commission,'partial':False}
                pending=None
        if position:
            q=position; side=q['side']; exit_price=None; reason=None
            stopped=r.low<=q['stop'] if side==1 else r.high>=q['stop']
            timeout=ts-q['entered']>=pd.Timedelta(minutes=c.max_hold_minutes)
            last_session=(local+pd.Timedelta(minutes=1)).strftime('%H:%M')>=c.session_end
            if stopped:
                exit_price=(min(r.open,q['stop']) if side==1 else max(r.open,q['stop']))-side*slip; reason='stop'
            elif timeout or last_session or ts==d.index[-1]:
                exit_price=r.close-side*slip; reason='time_or_session'
            else:
                for key in ('target1','target2'):
                    if key=='target1' and q['partial']: continue
                    hit=r.high>=q[key] if side==1 else r.low<=q[key]
                    if hit:
                        qty=math.floor(q['initial_qty']/2/c.quantity_step)*c.quantity_step if key=='target1' else q['qty']
                        price=q[key]-side*slip
                        pnl=side*(price-q['entry'])*qty*c.point_value-qty*c.commission
                        cash+=pnl; q['pnl']+=pnl; q['qty']-=qty; q['partial']=True
                        if q['qty']==0: reason='targets'; break
            if exit_price is not None:
                pnl=side*(exit_price-q['entry'])*q['qty']*c.point_value-q['qty']*c.commission
                cash+=pnl; q['pnl']+=pnl; q['qty']=0
            if reason:
                trades.append({'entry_time':q['entered'],'exit_time':ts,'side':side,'entry':q['entry'],'quantity':q['initial_qty'],'pnl':q['pnl'],'reason':reason})
                position=None
        mtm=cash if not position else cash+position['side']*(r.close-position['entry'])*position['qty']*c.point_value
        curve.append({'timestamp':ts,'equity':mtm})
        # Signal at boundary is available for this minute only AFTER preceding
        # five-minute bar closed. Schedule now; earliest fill is next minute.
        if ts in sig.index and not position and not pending and in_session and ts!=d.index[-1]:
            q=sig.loc[ts]
            if q.side:
                pending=q[['side','limit','stop','target1','target2']].to_dict()
                pending['expires']=ts+pd.Timedelta(minutes=15)
    t=pd.DataFrame(trades,columns=['entry_time','exit_time','side','entry','quantity','pnl','reason'])
    e=pd.DataFrame(curve)
    dd=(e.equity/e.equity.cummax()-1).min() if len(e) else 0
    wins=t.loc[t.pnl>0,'pnl'].sum(); losses=-t.loc[t.pnl<0,'pnl'].sum()
    return t,e,{'trades':len(t),'net_pnl':cash-equity,'win_rate':float((t.pnl>0).mean()) if len(t) else 0,'profit_factor':float(wins/losses) if losses else None,'max_drawdown':float(dd)}

<!-- END_FILE -->

Source: scalping_system/far_ma.py
<!-- BEGIN_FILE path=scalping_system/far_ma.py bytes=67 sha256=58a94ff0b75deb70879eeaa2a0b175fe2576117eb4fc9488131d7c36dda8a0ca -->
from run import main
if __name__ == "__main__":
    main("far_ma")

<!-- END_FILE -->

Source: scalping_system/forex.json
<!-- BEGIN_FILE path=scalping_system/forex.json bytes=196 sha256=aa2c8acaf76a307f2806808a35611f7108fdc78da25f9591c7d17529694a2404 -->
{
  "tick_size": 1e-05,
  "distance_unit": 0.0001,
  "point_value": 1,
  "quantity_step": 1000,
  "max_quantity": 100000,
  "spread": 0.0001,
  "session_start": "00:00",
  "session_end": "23:59"
}
<!-- END_FILE -->

Source: scalping_system/futures.json
<!-- BEGIN_FILE path=scalping_system/futures.json bytes=164 sha256=e3ae0ef1ef28b0661602cf47f4841c04d7c6374d49d994151543dea5a702ac15 -->
{
  "tick_size": 0.25,
  "distance_unit": 1,
  "point_value": 50,
  "quantity_step": 1,
  "max_quantity": 10,
  "session_start": "09:30",
  "session_end": "16:00"
}
<!-- END_FILE -->

Source: scalping_system/regular_ma.py
<!-- BEGIN_FILE path=scalping_system/regular_ma.py bytes=71 sha256=386f7ee47108a92a06cbf7ca2742899108b2646126d1cf7f3e605236f8891ae2 -->
from run import main
if __name__ == "__main__":
    main("regular_ma")

<!-- END_FILE -->

Source: scalping_system/requirements.txt
<!-- BEGIN_FILE path=scalping_system/requirements.txt bytes=30 sha256=76fd9dd57e1080a3e32fd47743bc1f13d6c393f03a2b1c33560f63cfff9166e0 -->
numpy>=1.26,<3
pandas>=2.2,<4

<!-- END_FILE -->

Source: scalping_system/run.py
<!-- BEGIN_FILE path=scalping_system/run.py bytes=956 sha256=cbfe84f4dbeb478430921576c709d337dec025e4e00cce0c8f29872e8db3c3e4 -->
import argparse,json
from pathlib import Path
from engine import Config,load_csv,signals,backtest

def main(default=None):
    a=argparse.ArgumentParser()
    a.add_argument('--data',required=True); a.add_argument('--config',required=True)
    a.add_argument('--strategy',default=default,choices=['trend','regular_ma','far_ma','base','double'],required=default is None)
    a.add_argument('--output',default='results'); a.add_argument('--equity',type=float,default=10000)
    args=a.parse_args(); c=Config(**json.loads(Path(args.config).read_text()))
    d=load_csv(args.data); s=signals(d,c,args.strategy)
    t,e,m=backtest(d,s,c,args.equity); out=Path(args.output); out.mkdir(parents=True,exist_ok=True)
    s.to_csv(out/'indicators_signals.csv'); t.to_csv(out/'trades.csv',index=False)
    e.to_csv(out/'equity.csv',index=False); (out/'metrics.json').write_text(json.dumps(m,indent=2))
    print(json.dumps(m,indent=2))
if __name__=='__main__': main()

<!-- END_FILE -->

Source: scalping_system/stocks.json
<!-- BEGIN_FILE path=scalping_system/stocks.json bytes=2 sha256=44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a -->
{}
<!-- END_FILE -->

Source: scalping_system/test_engine.py
<!-- BEGIN_FILE path=scalping_system/test_engine.py bytes=1761 sha256=b8cd1c6607999745e74fc83fb6e197e8e93fba39e8108016e8c81921472fc297 -->
import unittest
import pandas as pd
import numpy as np
from engine import Config, indicators, signals, backtest

class Tests(unittest.TestCase):
    def data(self,n=1500):
        idx=pd.date_range('2026-01-05',periods=n,freq='min',tz='UTC')
        a=100+np.sin(np.arange(n)/20)
        return pd.DataFrame({'open':a,'high':a+.2,'low':a-.2,'close':a,'volume':100},index=idx)
    def test_no_lookahead(self):
        d=self.data(); c=Config()
        for method in ('trend','regular_ma','far_ma','base','double'):
            full=signals(d,c,method); prefix=signals(d.iloc[:1300],c,method)
            pd.testing.assert_frame_equal(prefix,full.loc[prefix.index])
    def test_stop_before_target(self):
        d=self.data(4); ts=d.index[0]
        d.loc[d.index[1],['open','high','low','close']]=[100,103,98,100]
        s=pd.DataFrame([{'side':1,'limit':100,'stop':99,'target1':101,'target2':102}],index=[ts])
        c=Config(session_start='00:00',session_end='23:59',timezone='UTC',slippage_ticks=0)
        t,e,m=backtest(d,s,c)
        self.assertEqual(t.iloc[0].reason,'stop'); self.assertLess(t.iloc[0].pnl,0)
    def test_risk_sizing_and_costs(self):
        d=self.data(4); ts=d.index[0]
        d.loc[d.index[1],['open','high','low','close']]=[100,100.1,98,100]
        s=pd.DataFrame([{'side':1,'limit':100,'stop':99,'target1':101,'target2':102}],index=[ts])
        c=Config(session_start='00:00',session_end='23:59',timezone='UTC',commission=.01,slippage_ticks=1)
        t,e,m=backtest(d,s,c)
        self.assertLessEqual(-t.iloc[0].pnl,10000*c.risk_fraction+1e-8)
    def test_zero_volume_vwap(self):
        d=self.data(300); d.volume=0
        self.assertTrue(indicators(d,Config()).vwap.isna().all())
if __name__=='__main__': unittest.main()

<!-- END_FILE -->

Source: scalping_system/trend.py
<!-- BEGIN_FILE path=scalping_system/trend.py bytes=66 sha256=e5d3ab1d446acc37f048388f8f8d68640732d58a9c13fd670dd5e16e83a092ef -->
from run import main
if __name__ == "__main__":
    main("trend")

<!-- END_FILE -->