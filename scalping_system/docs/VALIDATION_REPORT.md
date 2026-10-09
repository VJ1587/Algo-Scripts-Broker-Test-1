# Executed validation report

## GitHub repository delivery verification

The implementation is included under `scalping_system/` in
`https://github.com/VJ1587/Algo-Scripts-Broker-Test-1`, on the existing `Scanner`
branch. After copying the clean source delivery into that repository, the
synthetic generator was run again and the tests were rerun from this package
folder: **107 modern tests passed in 13.15 seconds**, and **4 legacy tests passed**.
The repository README links the package and its instructions. `.gitattributes`
preserves frozen baseline bytes across Windows Git checkouts; generated inputs,
results, environments and test scratch directories remain ignored.

Validation date: 2026-10-08 (user timezone America/Chicago).
Environment: CPython 3.14.7, Windows AMD64; numpy 2.5.3, pandas 3.0.6, PyYAML 6.0.3, pytest 9.1.1.

Engineering implementation is validated on deterministic synthetic data. Real-data performance validation is pending. No adaptive improvement or profitability is claimed.

## Executed commands and results

Commands ran from `scalping_system`; `.venv\Scripts\python.exe` was used for Python commands.

```powershell
python reconstruct_scalping.py  # workspace root; all 13 embedded lengths/SHA-256 verified
python -m venv .venv
python -m pip install -e '.[dev]'
python examples/generate_synthetic_data.py --output examples/generated --seed 42
python -m scalping.cli validate-config --instrument configs/instruments/DEMO_STOCK.yaml --strategy trend
python -m scalping.cli run --strategy trend --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --mode adaptive --synthetic --output results
python tools/smoke_matrix.py
python -m scalping.cli compare --strategy trend --data examples/generated/stock_bars.csv --instrument configs/instruments/DEMO_STOCK.yaml --synthetic --output results/comparison_verified
python -m pytest -q -p no:cacheprovider --basetemp .test-tmp-acceptance
python -m unittest test_engine.py
python -m pip check
```

Final modern suite: **107 passed in 13.93 seconds**. Legacy suite: **4 passed**. `pip check`: no broken requirements. Editable installation and config validation succeeded.

The default `python -m pytest -q` was also executed: 90 tests passed and four temporary-directory fixtures were blocked by the sandbox. The approved rerun passed all 94 tests present at that stage. Later tests used a workspace-local temporary directory and disabled the cache provider, avoiding that environment issue.

## Failures found and corrected

- Generator initially omitted its instrument-config parent directory; creation was added.
- Two tests imported predicate helpers from narrow package exports; imports were corrected to their own strategy module.
- Empty CSV numeric types caused a nonfinite-check type error; numeric parsing and empty-frame handling were corrected.
- Fixed-ATR warmup tried to quantize unavailable ATR; it now emits NOT_READY without decimal NaN arithmetic.
- Direction sign could change before the three-bar regime exit; sign now remains frozen through the exit streak.
- A session-close setup could propose an already-expired order; session-close proposals are explicitly rejected.
- Missing resample components could lose their quality flag; the next valid setup now carries the missing-data flag.
- Pending-order/position ownership now keeps ORDER_PROPOSED state live until execution releases it; regime/quality cancels pending entries while filled exits stay frozen.

## Cross-asset synthetic smoke matrix

All 15 commands exited zero and their six output artifacts, feature columns, profile readiness, accepted signal IDs, sizing and JSON manifests were inspected by the runner. No-trade results are supported by independent positive bullish/bearish strategy fixtures.

The matrix was exercised during implementation; the final regression suite was rerun after the last core corrections. Counts below are captured engineering observations, not locked performance expectations.

| Strategy | Synthetic asset | Accepted proposals | Completed trades |
|---|---|---:|---:|
| trend | stock | 5 | 2 |
| regular_ma | stock | 27 | 3 |
| far_ma | stock | 1 | 1 |
| base | stock | 0 | 0 |
| double | stock | 2 | 2 |
| trend | fx | 0 | 0 |
| regular_ma | fx | 18 | 6 |
| far_ma | fx | 0 | 0 |
| base | fx | 0 | 0 |
| double | fx | 3 | 3 |
| trend | future | 0 | 0 |
| regular_ma | future | 0 | 0 |
| far_ma | future | 0 | 0 |
| base | future | 0 | 0 |
| double | future | 0 | 0 |

Every far-MA smoke command used its standalone module entry point; FX used the specified synthetic FX bars and causal USD/EUR conversions. Futures with insufficient risk/margin/partial size correctly skip entries.

## Engineering variant comparison

Common first-profile-ready start: `2026-02-02 14:30:00+00:00`. Identical modern execution engine and source data; all six attempts retained. The historical original engine remains separately frozen and is not confused with repaired legacy-static price distances.

| Mode | Cost multiplier | Completed synthetic trades |
|---|---:|---:|
| legacy_static | 1.0 | 3 |
| fixed_atr | 1.0 | 2 |
| adaptive | 1.0 | 2 |
| legacy_static | 2.0 | 3 |
| fixed_atr | 2.0 | 2 |
| adaptive | 2.0 | 2 |

These arbitrary generated-price outcomes validate wiring and reproducibility only. They are not financial evidence, a parameter selection result or a held-out improvement claim. Full metrics/manifests remain in local `results`; bulky generated results are excluded from the source ZIP.

## Validation coverage

Prefix/future perturbation invariance; profiles excluding current/future sessions; independent arithmetic indicator references; right-edge and delayed availability; missing/incomplete bars; zero-range/zero/unknown volume; decimal tick/lot rounding; pip, lot, multiplier and timestamped FX conversion; strict metadata/config failure; dated stock eligibility; risk/margin/minimum partial feasibility; bounded single ATR scale; every hysteresis equality and immediate quality pause; direction sign persistence; frozen policies/targets/stops/budgets; streaming state restart and duplicate delivery; stop/limit distinction, legal target prices, stop gaps, stop-first ambiguity, fees, partial exits, initial-equity drawdown; overnight, DST and scheduled breaks; session/final liquidation uncertainty; each strategy’s positive/negative fixtures; positive reference-fixture isolation and independent imports; offline training-only selection with embargo and every candidate retained.

## Sources and remaining limitations

The only strategy source is the user-supplied Markdown and its hash-verified embedded baseline. The photograph itself was not separately available. Built-in market data, quotes, calendars, membership and rates are synthetic; no real exchange/broker data source was independently verified or downloaded.

- Real-data chronological walk-forward validation, uncertainty intervals, out-of-sample degradation, cost reconciliation and nearby-parameter sensitivity remain pending suitable verified data. The offline runner is implemented and its selection/embargo logic is tested.
- OHLC cannot establish intrabar ordering or queue priority. Full limit-touch fills are a disclosed research assumption; event quotes are used when available, while intrabar extrema remain estimates. Stop-first ambiguities are counted.
- Split/corporate-action feeds, historical borrow availability and futures roll mapping must be supplied consistently upstream; there is no automatic feed or roll-P&L reconstruction. Continuous analysis prices cannot be executable contracts.
- Missing end-of-session/final prices and stale conversion can make liquidation unpriceable. These are flagged and excluded from completed-trade metrics. Stop gaps can exceed planned risk.
- Streaming APIs persist/restore state; the CLI writes terminal strategy state but does not orchestrate a resumable feed service.
- No live broker adapter, aggregate account allocator, online optimizer, risk escalation, pyramiding or swing method is included.

Exact source deviations and modern order interpretations: [SOURCE_RULES.md](SOURCE_RULES.md). Installation, tree and every strategy/mode command: [README.md](../README.md).
