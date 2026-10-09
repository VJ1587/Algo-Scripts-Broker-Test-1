# Instrument-adaptive scalping research system

Implemented deterministic upgrade, version 1.0. Python 3.10+; tested environment
and actual results are in [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md).
No profitability or adaptive improvement has been established. The supplied
specification is retained in `BUILD_SPECIFICATION.md`. Existing workspace trading
projects were not modified.

## Delivery and validation status

The implementation files are present in this `scalping_system` folder. The sibling
`scalping_system_delivery.zip` contains the source, documentation, tests and
hash-verified historical baseline. It excludes virtual environments, caches,
temporary test directories and generated market data/results.

Latest executed validation: **107 modern tests passed**, **4 legacy tests passed**,
**15 cross-asset smoke runs completed**, and **6 engineering comparison variants
completed**. See `docs/VALIDATION_REPORT.md` for commands, captured results and
remaining limitations. Only synthetic inputs were used; real-data performance
validation and verification of live instrument metadata remain pending.

To rebuild the delivery after editing source or documentation, run
`python tools/package_delivery.py` from this directory. Repository changes should
include source/configuration/docs/tests and the frozen baseline, while following
the project `.gitignore`; generated results and environments stay local.

## Install and demonstrate

Run from this project directory. On Windows, activate the local environment with
`.venv\Scripts\Activate.ps1`, or use `.venv\Scripts\python.exe` instead of `python`.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python examples/generate_synthetic_data.py --output examples/generated --seed 42
python -m scalping.cli validate-config --instrument configs/instruments/DEMO_STOCK.yaml --strategy trend
python -m scalping.cli run --strategy trend --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --mode adaptive --synthetic --output results
python -m scalping.strategies.far_ma.cli run --data examples/generated/fx_bars.csv --instrument configs/instruments/DEMO_FX.yaml --mode adaptive --synthetic --output results
python -m pytest -q
python tools/smoke_matrix.py
```

The generator makes 26 explicitly artificial sessions, OHLC, quotes, timestamped
USD/EUR conversions, a calendar and eligibility manifest. Generated data is
excluded from the delivery ZIP; regenerate it using the command above. All three
DEMO instrument definitions are fake and require `--synthetic`. No credentials,
paid data or downloads are needed after installation. Tests involving generated
fixtures require the generator first. In restricted Windows environments use
`python -m pytest -q -p no:cacheprovider --basetemp .test-tmp` if the default
temporary directory is inaccessible. Constraints for the tested environment are
in `constraints.txt`; use `pip install -c constraints.txt -e '.[dev]'` to reproduce.

## Independent methods and modes

Each directory owns its config, predicates, state machine and entry point. Run
any of `trend`, `regular_ma`, `far_ma`, `base`, `double` independently:

```powershell
python -m scalping.strategies.regular_ma.cli run --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --mode legacy_static --synthetic --output results
python -m scalping.strategies.base.cli run --data examples/generated/future_bars.csv --instrument configs/instruments/DEMO_FUTURE.yaml --mode fixed_atr --synthetic --output results
python -m scalping.strategies.double.cli run --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --mode adaptive --synthetic --output results
python -m scalping.cli compare --strategy trend --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --synthetic --output results/comparison
```

`legacy_static` uses the new declared strategy definitions and repaired execution
with `legacy_distance_unit` price distances. `fixed_atr` uses current ATR without
profile scaling. `adaptive` needs a positive same-bucket baseline from 20 prior
complete sessions, each bucket with at least 10 session observations. These are
research variants. The exact historical API/semantics are preserved separately:

`compare` runs all three modes on the same first-profile-ready date range under
the modern simulator, at base costs and doubled spread/slippage/fees. It records
all six attempts and their outputs in a unique comparison JSON. This engineering
comparison does not select parameters or establish out-of-sample improvement.

```powershell
python trend.py --legacy --data one_minute_bars.csv --config stocks.json --output results/legacy
python -m unittest test_engine.py
```

`engine.py` is the compatibility adapter. `legacy/engine.py` and the original
13-member `legacy/baseline-v0.zip` retain the original engine. The modern wrapper
requires `--instrument`; old generic `stocks.json`, `forex.json`, `futures.json`
cannot establish contract metadata for adaptive execution.

To reconstruct the original prototype elsewhere, use
`python tools/reconstruct_baseline.py BUILD_SPECIFICATION.md new_baseline` or
`python -m zipfile -e legacy/baseline-v0.zip new_baseline`. The reconstruction
helper verifies all 13 hashes before writing and refuses existing destinations.

## Configuration and risk

Defaults live in `configs/adaptation.yaml` and each strategy's packaged
`config.yaml`. CLI `--mode` overrides YAML mode; `--config` selects a full strategy
configuration; strategy adaptation overrides merge over global adaptation
defaults and must stay within hard scale bounds. Unknown keys, nonfinite values,
changed mandatory predicates, pyramiding and risk above 2% fail validation.
Default proposed risk is 0.5%; lower configured limits are preserved. External
`--approved-risk` is a cash ceiling, and `--buying-power` constrains notional/margin.
Neither adaptation nor sizing increases the configured risk budget.

All commands simulate research by default. `--actionable --buying-power 100000`
requires complete metadata, readiness, eligibility, costs and conversion; it
still sends no orders. A non-ready actionable run exits with code 2. Research
with unknown buying power is explicitly labelled in decisions. An externally
approved risk of zero suppresses entries. Gaps can exceed planned loss; the
simulator records risk breaches. Two partial exits require tradable size at
both exits; no automatic single-exit fallback.

Regular MA 15/30-minute setup versions may be selected only through a config
with a named version different from `1.0`; they do not vote across unfinished
charts. The other methods retain five-minute setup and one-minute execution.

## Source interpretations and data requirements

[SOURCE_RULES.md](docs/SOURCE_RULES.md) records each versioned interpretation.
The photograph itself was not supplied here; the source rules come from the
user's complete Markdown and embedded legacy README. This implementation is
not an exact transcription of an independently inspected image.

[DATA_CONTRACT.md](docs/DATA_CONTRACT.md) specifies timestamps, price and volume
basis, calendars, quotes, FX conversion, equity membership and contract rolls.
Real calendars and live instrument metadata must be supplied and verified. The
invalid `docs/REAL_INSTRUMENT_TEMPLATE.yaml` is intentionally rejected until
completed. Stock point-in-time membership uses a manual blue-chip allowlist,
large-cap minimum USD 10 billion and mega-cap minimum USD 200 billion. Borrow
availability and corporate-action-consistent prices remain input obligations.
Do not use an index alias, continuous futures series or CFD as an actual contract.

## Outputs and replay

Every run creates a new directory with strategy, symbol, config/data hashes and
a unique run ID. It contains `indicators.csv`, `signals.jsonl`, `trades.csv`,
`equity.csv`, `metrics.json`, `run_manifest.json`. Indicator rows and strategy
decisions share the same feature/policy calculation. Accepted and rejected
decisions carry timestamps and reason codes; proposed orders have frozen policy,
levels, expiry, quantity sizing, risk and source evidence. No-trade runs still
produce all files. Undefined metrics are JSON null. Drawdown includes initial
equity. Reports separate completed net trades from unpriceable liquidations.

`Strategy.evaluate(Context, StrategyState)` returns `(Decision, StrategyState)`;
contexts reject future bars. Strategy state has deterministic JSON restore.
`Simulator.process` is the streaming event API; its JSON state restores pending
orders and positions, and duplicate event/signal delivery does not add orders.
The CLI records terminal strategy state; application-level resume orchestration
is left to a caller of these APIs. No strategy imports another strategy.

The event order is closed bars → causal features/decision → configured latency →
next eligible future execution event. Zero latency permits the open at the
decision timestamp after the preceding candle closes, with no extra minute
delay. No candle that supplied confirmation can retrospectively fill its order.
Market targets are anchored once at actual fill; limit/stop targets stay frozen.
Stops never widen; exits never adapt after fill.

## Offline research and limits

`adaptation.calibration.calibrate` takes an explicit candidate grid, evaluator,
target metric, chronological boundaries and at least 30 minutes of embargo.
It selects on training data only, exports every attempted candidate and evaluates
the frozen choice on validation/test. Synthetic calibration tests validate
engineering only. Real walk-forward studies, stressed costs, parameter
sensitivity and out-of-sample degradation remain pending without suitable
verified historical inputs. Costs, thresholds and source-dollar-to-ATR mapping
are provisional hypotheses.

OHLC fills assume full limit-touch fill without queue knowledge; one-minute
quotes supply event-side prices, while intrabar extrema remain OHLC estimates.
Stop-first resolves ambiguous stop/target candles conservatively and counts
them. Missing final/session-close prices are flagged as unpriceable rather than
inventing a liquidation. There is no live broker adapter, account allocator,
automatic risk escalation, online optimizer or self-modifying strategy.

## File tree

```text
scalping_system/
  pyproject.toml, requirements.txt, constraints.txt, README.md
  BUILD_SPECIFICATION.md
  legacy/                     # hash-verified baseline and v0 ZIP
  configs/{instruments,sessions}/, configs/adaptation.yaml
  src/scalping/
    contracts.py, instrument.py, sessions.py, data.py, indicators.py
    strategy_support.py, cli.py, reporting.py
    adaptation/{features,profile,regime,policy,calibration}.py
    execution/{costs,sizing,simulator}.py
    strategies/{trend,regular_ma,far_ma,base,double}/
      __init__.py, strategy.py, config.yaml, cli.py
  tests/                      # shared suite plus five strategy fixtures
  docs/                       # source, adaptation, data and validation rules
  examples/generate_synthetic_data.py
  tools/{smoke_matrix,package_delivery}.py
  engine.py, run.py, trend.py, regular_ma.py, far_ma.py, base.py, double.py
  stocks.json, forex.json, futures.json, test_engine.py
```
