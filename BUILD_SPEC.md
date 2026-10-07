# BUILD SPEC: StressLab, Codeguard and the HTML report stress layer

Audience: Claude Code. This file is the full specification for a stress
testing and bug detection system for a Python trading stack (instrument
personality models, setup scanners, trade execution, trade journals, HTML
reports), with MT5 and TradingView covered through parity checks.

Work in the phases below, in order. Do not start a phase until the previous
phase meets its acceptance criteria. At the end of each phase, run the
listed commands and show the output.

If a reference copy of the project (`stresslab.zip`) is present in the repo,
treat it as the reference implementation: unzip it, run the acceptance
commands, and match its behaviour. This spec then serves as the checklist
and the explanation of WHY each part exists.

---

## 0. Ground rules

1. Python 3.11 or newer. Dependencies: `numpy pandas scipy pytest ruff beautifulsoup4`.
   Optional: `playwright` plus `playwright install chromium` for browser checks.
2. Every function that uses randomness takes a `seed`. Use
   `np.random.default_rng(seed)`. Never call the global `np.random.*` functions.
3. All timestamps are tz aware UTC.
4. No code runs at import time except definitions. Anything that connects,
   downloads, writes files, launches subprocesses or sends orders goes inside a
   function or under `if __name__ == "__main__":`.
5. Every check reports PASS, WARN or FAIL with a one line reason. FAIL means
   the result cannot be trusted. WARN means a human should look.
6. The test harness must be able to prove it still works. For every class of
   bug it detects, ship a deliberately broken "planted bug" component and a
   test asserting the harness catches it.
7. Create a `CLAUDE.md` at the repo root summarising these ground rules so
   later sessions keep them.

Repo layout to create:

```
stresslab/        core harness (package)
codeguard/        bug detector and entry gate (package, runnable with python -m codeguard)
codeguard/samples planted buggy and clean scripts used by codeguard tests
examples/         demo components and demo report generators (good and buggy)
tests/            pytest suites
run_stress.py     runs the full stress suite, writes reports/
audit_html.py     audits HTML files not generated in Python
ci.py             pipeline: codeguard gate -> pytest -> stress suite
codeguard.toml    repo config for codeguard
.github/workflows/codeguard.yml, .pre-commit-config.yaml
README.md
```

---

## Phase 1. StressLab core harness

### 1.1 `stresslab/adapters.py`
Protocols the user's real scripts get wrapped in. The harness never touches
user internals.

- Bars schema: tz aware UTC DatetimeIndex, strictly increasing; columns
  `open high low close volume`, optional `spread` in price units.
- `PersonalityModel`: attribute `output_bounds: dict[name, (lo, hi)]`, method
  `assess(bars) -> dict[str, float]`. Optional attribute
  `expectations: dict[metric, [regime_low, regime_high]]`. Optional method
  `assess_series(bars) -> DataFrame` (one row per bar).
- `Scanner`: `scan(bars) -> DataFrame` with columns
  `time, side (+1/-1), entry, stop, target`.
- `Executor`: `on_signal(signal_row, broker)`; may expose `risk_frac`.
- `Journal`: `record(trade_dict)`, `summary() -> {"n_trades", "net_pnl", ...}`.
- `validate_bars(bars) -> list[str]`: missing columns, non DatetimeIndex,
  timezone naive index, out of order, duplicates, NaN, high/low envelope
  violations, non positive prices.
- `validate_signals(df) -> list[str]`: side not +1/-1, long with stop at or
  above entry, short with stop at or below entry, NaN prices.

### 1.2 `stresslab/generators.py`
Synthetic markets with controlled properties.

- `gbm_returns`, `garch_returns` (GARCH(1,1) with Student t shocks),
  `jump_returns`, `regime_switching_returns` (Markov switching over
  `Regime(name, drift, sigma, ar)`; default regimes trend_up, trend_down,
  range, panic; returns `(returns, labels)`; must work with a single regime),
  `block_bootstrap_returns(real_returns, n, block)`.
- `to_bars(returns, ...)` builds OHLC. **Pitfall to avoid:** do NOT add
  independent random wicks. Independent wicks bias stop versus target hit
  rates (in the reference build this produced about -0.34R per trade on a pure
  random walk). Build highs and lows from a Brownian bridge between open and
  close with ~12 substeps per bar, using local (rolling 24 bar) volatility.
- Shocks: `apply_gap(bars, at, pct)`, `apply_flash_crash(bars, at, depth,
  recover_bars)`, `widen_spread(bars, start, end, mult)`.

### 1.3 `stresslab/faults.py`
Data corruption the way real feeds fail. Each fault takes clean bars and
returns corrupted bars.

Faults: missing_bars, duplicate_timestamps, out_of_order, nan_values,
bad_tick_spike, stale_feed, timezone_shift (MT5 server time labelled as UTC),
naive_timestamps, high_low_violation, truncated_history, price_scale_change.

Two sets:
- `MUST_DETECT` (code must raise or warn; silently producing output is a
  FAIL): duplicate_timestamps, out_of_order, nan_values, naive_timestamps,
  high_low_violation, price_scale_change.
- `MUST_SURVIVE` (legitimate ugliness; code must run and produce valid
  output): missing_bars, bad_tick_spike, stale_feed, timezone_shift
  (WARN only, it cannot be detected from data), truncated_history.

### 1.4 `stresslab/invariants.py`
- `check_scanner_no_lookahead(scanner, bars)`: truncation test. Run on full
  history and on history cut at ~12 checkpoints; every signal at or before the
  cut must be identical in both runs (compare time, side, entry, stop rounded).
- `check_personality_no_lookahead(model, bars)`: if `assess_series` exists,
  series value at bar t must equal `assess(bars[:t+1])`. This catches full
  sample z scores and percentile ranks.
- `check_determinism`, `check_personality_bounds`, `check_scanner_schema`
  (also: no signal after last bar, signal times on bar boundaries).
- `check_personality_discrimination(model, bars_by_regime, metric,
  expect_order)`: ground truth test on synthetic regimes.

### 1.5 `stresslab/execution_sim.py`
Pessimistic simulated broker.

- `ExecConditions(spread_mult, slippage, latency_bars=1, reject_prob,
  partial_fill_prob, max_units)`.
- `SimBroker.market_order(side, units, stop, target, signal_time,
  risk_budget)`. Orders without a finite stop log a `naked_order` event.
- Orders fill at the open of bar `submit_bar + latency_bars`, adverse by half
  spread plus slippage. Log `rejected`, `partial`, `filled_beyond_stop`.
- Exits: gap through stop fills at the open (`gap_stop`); intrabar stop is
  checked BEFORE target (pessimistic rule).
- Store `planned_risk` (distance from fill to stop times units) and
  `equity_at_fill` on each position.
- **Pitfall to avoid:** compute `r_multiple = pnl / risk_budget` (the risk the
  executor INTENDED), not pnl divided by risk at fill. Fills beyond the stop
  otherwise produce absurd R values like -90R.
- `run_execution(signals, bars, executor, cond, seed, journal)` walks bars,
  steps the broker, hands each signal to the executor at its bar, returns
  `(trades_df, broker)`, and records every closed trade in the journal.

### 1.6 `stresslab/montecarlo.py`
- `trade_sequence_mc(r_multiples, n_sims, n_trades, risk_frac, ruin_dd,
  method="bootstrap"|"shuffle")`: max drawdown p50/p95/p99, final equity
  p5/p50, p95 losing streak, probability of ruin, probability of loss.
- `parameter_sensitivity(metric_fn, base_params, jitter, n, int_params)`:
  perturb each numeric param by up to +/- jitter; report p5/p50/p95 and
  `cliff_ratio` (share of runs losing more than half the base metric).
- `synthetic_path_mc(evaluate, make_bars, n_paths)`.

### 1.7 `stresslab/overfit.py`
- `pbo_cscv(perf_matrix_T_by_N, n_splits=16)`: Probability of Backtest
  Overfitting via combinatorially symmetric cross validation.
- `deflated_sharpe(returns, n_trials, sr_variance_across_trials=None)`.
- Docstrings must credit Bailey, Borwein, Lopez de Prado and Zhu, and state
  the implementation should be verified against the original papers. Do not
  invent paper titles or URLs.

### 1.8 `stresslab/trial_log.py`
CSV ledger of every backtest trial (`log_trial`, `trial_count`,
`sharpe_variance`) so the Deflated Sharpe uses an honest trial count. Use
`hashlib.sha1(..., usedforsecurity=False)` for param hashes.

### 1.9 `stresslab/regression.py`
- `golden_check(name, output, golden_dir, rtol, update)`: snapshot a
  DataFrame or dict to JSON; later runs compare with tolerance.
- `parity_check(reference_df, other_df, key="time", cols, price_tol,
  time_tol)`: compares Python versus MT5/TradingView exports; reports
  signals only in one side, side mismatches, price mismatches.

### 1.10 `stresslab/loaders.py`
`load_bars_csv(path, colmap, server_utc_offset_hours, ...)` and
`load_trades_csv(...)`. Take an explicit column map; never assume an export
layout. Docstring warns that MT5 bar times are broker server time and the
offset must be confirmed with the broker.

### 1.11 `stresslab/scenarios.py`
`Scenario(name, description, make_bars(seed), cond, baseline: bool)`.
Ten scenarios of ~3000 hourly bars: random_walk_null, vol_clustering_fat_tails
(baseline), steady_trend, choppy_range (baseline), regime_switching
(baseline), flash_crash, peg_break_gap (8% gap, slippage 0.0001),
liquidity_drought (spread x6, 2 bar latency, 10% rejects, 20% partials),
jumpy_news_market (baseline), slow_execution (3 bar latency plus slippage).

Also `HISTORICAL_REPLAYS`: windows to replay with REAL data (SNB EUR/CHF floor
removal Jan 2015, UK EU referendum June 2016, COVID liquidity shock Feb to Apr
2020, UK gilt/LDI crisis Sep to Oct 2022, yen carry unwind Aug 2024). Mark
dates as "verify against primary sources" and list NO move sizes. Mark all
synthetic magnitudes as illustrative, to be calibrated per instrument.

### 1.12 `stresslab/suite.py`
`StressSuite(personality, scanner, executor_factory, journal_factory,
scanner_factory, scanner_params, seeds=(1,2,3), golden_dir, update_golden,
report_generator=None, report_browser=False)`. `run_all()` runs, in order:

1. invariants (lookahead, schema, bounds, determinism)
2. data faults (MUST_DETECT and MUST_SURVIVE on scanner and personality)
3. personality discrimination (10 synthetic samples per expectation; PASS at
   9 or more correct, WARN at 6 to 8)
4. scenario sweep: per scenario, aggregate seeds. HARD problems (FAIL
   anywhere): naked orders, journal does not reconcile to broker within 0.01,
   journal trade count mismatch. SOFT problems: fills over 1.25x risk budget,
   worst trade below -3R. Soft problems are WARN in stress scenarios and FAIL
   in baseline scenarios.
5. null test: pooled R on random walks with default execution; FAIL if
   t statistic >= 2 (edge on noise means leakage or fill bias). Guard against
   empty trade frames.
6. Monte Carlo: pool R ONLY from baseline scenarios; horizon 200 trades;
   WARN if probability of 25% ruin >= 5%.
7. parameter sensitivity (if factory given): WARN if cliff_ratio > 30%.
8. regression golden masters for scanner signals and personality scores.
9. HTML reports (Phase 3), aggregated to one scorecard row per check.

Return `(scorecard_df, scenario_rows_df, mc_result, sensitivity_result)`; keep
the HTML detail on `suite.html_detail`.

### 1.13 `examples/demo_components.py`
Good components: `DemoPersonality` (efficiency ratio trend_strength,
annualised realized_vol clipped 0..1, mean_reversion from negative lag 1
autocorrelation; validates input and raises on bad data, including any single
bar log move > 50% as a price scale change), `DonchianScanner(lookback=55,
atr_len=14, stop_atr=2, target_atr=3)` with an overridable `_atr` method,
`FixedFractionalExecutor(risk_frac=0.01, max_open=1)`, `SimpleJournal`.

Planted bugs: `LeakyScanner` (ATR uses `rolling(center=True)`, so stops are
sized with future volatility; mark the line with
`# guard: ignore[TG102] planted bug for harness self test`) and
`SloppyPersonality` (no input validation; `assess_series` normalises over the
full sample).

### 1.14 `run_stress.py`
Flags: `--buggy` (planted bug stack), `--update-golden`, `--browser`,
`--out`. Writes `reports/scorecard_<tag>.md/.csv`, `scenarios_<tag>.csv`,
`html_reports_<tag>.csv`. Exit 1 if any FAIL.

### Phase 1 acceptance
- `python run_stress.py` shows 0 FAIL (a few WARNs in stress scenarios are
  expected: risk budget breaches under latency, tail loss in the gap scenario).
- Random walk null test mean near 0R (spread cost only); a fixed 2 ATR stop /
  3 ATR target bracket on a random walk with zero spread hits target about
  40% of the time (within 5 points).
- `python run_stress.py --buggy` FAILs scanner lookahead and personality
  lookahead plus all personality MUST_DETECT faults.
- `tests/test_stress.py` covers the above plus PBO (noise > 0.3, one real edge
  < 0.1), deflated Sharpe penalising many trials, shuffle MC preserving
  expectancy, and parity detecting a price disagreement. All pass.

---

## Phase 2. Codeguard: bug detector and entry gate

### 2.1 `codeguard/rules.py`
AST rules for bugs generic linters miss. `RULESET_VERSION` string included in
cache keys; bump it whenever rules change.

| ID | Sev | Detects |
|---|---|---|
| TG000 | BLOCK | file does not parse |
| TG101 | BLOCK | `.shift(-n)` or `shift(periods=-n)` |
| TG102 | BLOCK | `rolling(..., center=True)` |
| TG103 | BLOCK | `.bfill()`, `.backfill()`, `fillna(method="bfill")` |
| TG104 | WARN | `(x - x.mean()) / x.std()` style full sample normalisation. Not flagged when the chain contains rolling/expanding/ewm/groupby/resample, OR the base variable was assigned from such a chain (track `r = s.rolling(50)`) |
| TG105 | WARN | `.rank(pct=True)` not on a windowed chain |
| TG201 | WARN | global `np.random.<fn>`, `default_rng()` with no seed, `random.<fn>` |
| TG202 | WARN | `datetime.now()`/`today()` without tz, any `utcnow()`, `pd.Timestamp.now()` without tz |
| TG203 | WARN | `==`/`!=` against a non zero float literal |
| TG301 | BLOCK | broad or bare `except` whose body is only pass/continue/docstring |
| TG302 | WARN | mutable default argument |
| TG303 | WARN | chained assignment `df[a][b] = v` |
| TG304 | BLOCK | MetaTrader5 `initialize/login/order_send/symbol_select/order_check` called as a bare statement (result discarded); track `import MetaTrader5 as X` aliases |
| TG305 | BLOCK | dict literal with keys action, symbol, volume, type but no `sl` |
| TG306 | BLOCK | name or keyword matching password/passwd/api_key/secret/token/private_key assigned a string literal of length >= 4 |
| TG307 | WARN | side effect at import time (module level, outside main guard): mt5 initialize/login/order_send, requests.get/post, yf.download, subprocess run/call/Popen/check_call/check_output, os.system, `.to_csv/.to_parquet/.to_excel`. Check Expr statements AND any Call anywhere inside an Assign value (e.g. `rc = subprocess.run(cmd).returncode`) |
| TG308 | WARN | `assert` outside test files |
| TG401 | WARN | `x = pd.concat([x, ...])` inside a loop |
| TG402 | BLOCK | `df = df.append(...)` (same name assigned back; removed in pandas 2.0). **Pitfall:** do NOT flag `self.trades.append(x)`; list.append returns None, so only the assign back pattern is a reliable signal |
| TG403 | INFO | `.iterrows()`, `.apply(axis=1)` |
| TG404 | WARN | eval, exec, pickle.load(s) |
| TG405 | WARN | f-string containing an HTML tag that inserts a Subscript or Attribute value (e.g. `r['notes']`, `meta['title']`) with no format spec and no Call inside it, i.e. not escaped |

Suppressions: `# guard: ignore[TG101] reason`. A suppression only counts if
the reason has at least 5 characters. Applies to ruff findings too.

Each finding: rule, severity, path, line, col, message, fix hint, code
snippet, source (codeguard or ruff).

### 2.2 `codeguard/engine.py`
- Discovery with default excludes (.git, venvs, caches, build, dist,
  node_modules, site-packages) plus `codeguard.toml` excludes.
- `codeguard.toml` keys: `exclude`, `disable`, `[codeguard.severity]`
  overrides, `[codeguard.per_path_disable]` glob to rule list.
- Incremental cache `.codeguard_cache.json` keyed by sha256 of
  `RULESET_VERSION + file content`; drop deleted files from cache.
- Parallel AST analysis with ProcessPoolExecutor when more than 20 files.
- One batched ruff call per 400 changed files: select
  `F,E9,B,PD,NPY,S,PERF,PLE,ASYNC`; ignore codes that duplicate codeguard or
  are noise: `B006,S101,S105,S106,S110,S112,S603,S607,S301,S307,S311,NPY002,BLE001,DTZ,PD901,PD011`.
  Skip ruff findings with no code or `invalid-syntax` (TG000 covers parse
  errors). BLOCK prefixes: F821,F822,F823,E9,PLE,F632,B015,B018; everything
  else WARN. If ruff is not installed, continue without it and say so.
- `--changed [REF]` scans only `git diff --name-only REF` plus untracked files.
- Baseline ratchet: fingerprint = sha1 of `rule|path|whitespace normalised
  snippet` (NOT line number). `split_new` uses counts, so a second copy of a
  known bug is new. Report how many baseline items were fixed.
- Outputs: markdown (summary by rule plus findings with code and fix),
  JSON, SARIF 2.1.0.

### 2.3 `codeguard/smoke.py`
Import every module in a fresh interpreter, in parallel threads, with a
timeout. Import by DOTTED MODULE NAME from the repo root (so relative imports
and dataclasses work); fall back to loading by path only for filenames that
are not valid identifiers, registering the module in `sys.modules`. **Skip any
file with a TG307 finding**, so a smoke test can never connect to a broker or
place an order.

### 2.4 `codeguard/debugrun.py`
`run(script, argv, report_path)`: runs the script with runpy as `__main__`,
captures warnings with counts, wall time and peak memory. On exception, walks
traceback frames that belong to the user's code (exclude site-packages and
the codeguard package directory itself, compared by exact directory, not
substring, so `codeguard/samples` still counts as user code) and summarises
locals: DataFrame shape, columns, NaN counts by column, index tz, sorted,
unique, first and last timestamp; Series; ndarray with NaN/inf counts;
scalars; container lengths. Adds pattern hints: tz naive/aware mix, KeyError
column mismatch, NoneType from MT5 (check `mt5.last_error()`), index out of
bounds, divide by zero, empty/unsorted/duplicated DataFrames, NaNs present.
Restore `warnings.showwarning`, filters, `sys.argv` and `sys.path` in a
`finally` block.

### 2.5 `codeguard/__main__.py`
Subcommands: `check`, `gate` (check plus smoke; prints GATE OPEN/CLOSED),
`smoke`, `run SCRIPT [-- ARGS]`, `rules`. Flags: `--baseline`,
`--update-baseline`, `--changed [REF]`, `--fail-on BLOCK|WARN|never`,
`--jobs`, `--no-cache`, `--no-ruff`, `--format md|json|sarif`, `--out`.
Gate on new findings when a baseline exists, otherwise all findings.

### 2.6 `codeguard/samples/`
- `bad_mt5_scanner.py`: triggers TG101 to TG105, TG201 to TG203, TG301 to
  TG308, TG401, TG403 in realistic MT5 scanner code (hardcoded password,
  initialize/login at import, order dict without sl, swallowed exception...).
- `bad_journal.py`: `journal = journal.append(...)` in a loop (TG402),
  `pickle.load` (TG404), an f-string row with `r['symbol']` (TG405), and a
  main block that crashes with IndexError on an empty filtered DataFrame.
- `bad_syntax.py`: does not parse (TG000).
- `clean_scanner.py`: equivalent clean code with ZERO findings, including a
  correctly suppressed research label line.
Exclude `codeguard/samples` from repo scans via `codeguard.toml`.

### Phase 2 acceptance
- `tests/test_codeguard.py`: every rule in RULES is triggered by some
  sample; clean sample has no findings; parametrised lookahead patterns are
  caught; parametrised clean patterns (shift(1), rolling mean, ffill, rolling
  variable alias, expanding normalisation, rolling rank, list append) are NOT
  flagged; suppression without reason does not suppress; order dict with and
  without sl; side effect inside main guard is fine; module level
  `rc = subprocess.run([...]).returncode` is flagged; baseline ratchet (moved
  line is not new, duplicated bug is new); cache reuses and invalidates
  (30 files: 30 analyzed, then 30 cached, then 1 analyzed after an edit);
  SARIF shape; debug runner reports "EMPTY DataFrame" for bad_journal.py.
- `python -m codeguard check codeguard/samples --no-cache` flags the planted
  bugs; `python -m codeguard run codeguard/samples/bad_journal.py` prints the
  frame summary showing `df` with shape [0, 3] and the empty DataFrame hint.
- `python -m codeguard gate .` on the repo: 0 BLOCK, all modules import,
  GATE OPEN.
- Scale benchmark (record actual numbers, do not invent them): generate
  3,000 files (~300k lines) in a temp folder; measure cold scan, warm scan,
  and scan after editing 25 files. Reference single core numbers were about
  9s cold, 0.5s warm, 0.5s after 25 edits.

---

## Phase 3. HTML report stress layer

### 3.1 `stresslab/html_stress.py`
Report generator contract: `render(trades_df, meta) -> html_str`. Trades
columns: time (UTC), symbol, side, entry, exit, units, pnl, r_multiple,
strategy, notes. Meta: title, start_equity.

Page tagging contract:
- `<x data-metric="NAME">` for n_trades, net_pnl, win_rate (percent), avg_r,
  max_drawdown (percent), profit_factor, excluded_rows.
- `<table data-table="trades" data-total="N">` (data-total when paginated).
- `<script type="application/json" data-chart="equity">[{"t","equity"}]</script>`.

`datasets(seed)` returns 12 named `(trades, meta)` pairs: normal_200, empty,
single_trade, all_losers, all_winners, missing_values (NaN pnl on 3 rows, inf
r on 1), hostile_text (injection strings in notes, symbol, strategy, each
containing the marker `XSSMARK`, including `</script>` breakout and
`onerror` attributes), unicode_long_text (emoji, Arabic, CJK, zero width,
combining marks, one 5,000 char note), extreme_values (1.5e12, -9.9e11,
1e-9, -0.0), unsorted_duplicates, scale_5k, scale_50k.

`expected_metrics(trades, start_equity)`: an INDEPENDENT oracle (must not
import or share code with any report generator). Drop non finite pnl rows and
count them as excluded_rows; sort by time; win_rate counts pnl > 0 only;
max_drawdown peak INCLUDES starting equity; profit_factor is inf when no
losses but some gains, None when nothing to divide.

Checks:
- `parse_number`: handles `$`, `£`, `€`, commas, accounting parentheses,
  unicode minus, `%`, scientific notation, `∞`, `n/a`; returns
  (value, displayed decimals).
- `check_structure`: tag balance with html.parser (void elements ignored,
  optional end tags tolerated), duplicate ids, title, charset.
- `check_bad_tokens`: visible text (scripts/styles removed) must not contain
  nan, NaN, inf, -inf, Infinity, None, NaT, undefined, null, [object Object].
- `check_injection`: marker inside executable script, `</script` inside JSON
  data blocks, marker in on* / href / src / action / style attributes, live
  img/script/iframe carrying the marker, marker rendered as a real `<b>`.
- `check_offline`: external script/stylesheet/img/iframe/media/css url()
  sources; leaked local file paths.
- `check_reconcile(page, expected) -> (problems, n_checked, untagged)`:
  tolerance = half a unit of the displayed last decimal (times k/M/bn
  suffix) plus 1e-9 relative. Untagged metrics are NOT failures; they become
  a separate WARN "metric_coverage: not tagged, so not verified". Undisclosed
  excluded rows are a failure.
- `check_table`: row count equals valid trade count, or data-total equals it.
  **Pitfall:** if the table has no `<th>` and its first row contains no
  digits, treat that first row as a header (a11y flags it separately).
- `check_chart`: parse with `json.loads(..., parse_constant=raise)` because
  Python accepts NaN/Infinity but browser JSON.parse does not; length equals
  trade count, timestamps monotonic, last equity equals start plus net P&L.
- `check_timezone` (WARN), `check_a11y` (WARN: html lang, th, img alt,
  viewport), determinism (render twice, strip data-generated content).
- Scale: `ReportBudget(max_seconds_5k=2, max_seconds_50k=15, max_mb_50k=40,
  max_growth_exponent=1.3)`; growth exponent from 5k to 50k render times.
- `browser_checks(page)` (optional, Playwright): 390x844 viewport; collect
  console errors and page errors; route and abort every http(s) request and
  record it; evaluate `!!window.XSSMARK` to detect executed injection; detect
  horizontal overflow. Skip with WARN if Playwright is missing.
- `HtmlReportStress(generator, budget, browser, allow_external).run()`
  returns a detail DataFrame (dataset, check, status, detail).
- `audit_html_file(path, expected=None)`: integrity checks for files not
  generated in Python; detect UTF-16 by BOM; list tables via pandas.read_html.
- `escape_json_for_script(obj)`: `json.dumps(allow_nan=False)` then escape
  `<`, `>`, `&` as `<`, `>`, `&`.

### 3.2 `examples/demo_reports.py`
- `SafeJournalReport`: escapes every text field with `html.escape`
  (including labels), excludes non finite pnl rows and discloses the count,
  sorts by time, n/a for undefined metrics, `∞` for infinite profit factor,
  enough decimals to reconcile, "All times UTC", inline CSS with dark mode,
  responsive KPI grid, table inside an overflow-x wrapper, long notes wrap,
  shows the latest 2,000 rows with data-total, inline SVG equity sparkline
  downsampled to 1,000 points for drawing only, chart JSON via
  `escape_json_for_script`, a tiny inline script that must not error, lang,
  charset, viewport. No external resources.
- `NaiveJournalReport` (planted bugs): f-strings with no escaping, `np.sum`
  poisoned by NaN, breakeven counted as win with NaN rows in the denominator,
  drawdown peak ignoring start equity, profit factor dividing by zero, table
  silently truncated to 1,000 rows with a `<td>` header row, `json.dumps`
  writing NaN, unsorted equity series, CDN Chart.js script, an inline script
  that throws, no lang/charset/viewport.

### 3.3 `audit_html.py`
CLI over files or folders (recursive `*.htm*`): prints PASS/WARN/FAIL per
file with issues; FAIL on structure, bad tokens or external resources.

### Phase 3 acceptance
- `SafeJournalReport` passes every check on every dataset, with and without
  `--browser`. 50k trades should render well inside budget (reference: about
  0.4s and 3.3 MB).
- `tests/test_html_stress.py`: safe generator has no FAIL; the naive
  generator FAILs at least these (dataset/check): hostile_text/injection,
  missing_values/bad_tokens, missing_values/reconcile, missing_values/chart,
  extreme_values/reconcile, single_trade/reconcile, unsorted_duplicates/chart,
  scale_50k/table, normal_200/offline; reconcile respects rounding;
  parse_number cases; injection detector catches a JSON block breakout;
  escaped JSON is inert; browser test (skip if Playwright or Chromium is
  missing) shows no executed injection, no page errors, no external requests.
- With `--browser`, the naive report on hostile_text shows "INJECTED SCRIPT
  EXECUTED", a `Chart is not defined` JS error and a blocked CDN request.
- Codeguard TG405 flags the naive report's unescaped fields and nothing in
  the safe report.

---

## Phase 4. Pipeline, CI and docs

- `ci.py`: `main()` runs codeguard gate, then pytest, then `run_stress.py`;
  stops at the first failure and says which stage. `--fast` skips the stress
  suite. **Pitfall from the reference build:** the first version ran the
  pipeline at module level; the import smoke test imported it, which
  relaunched the pipeline recursively and timed out other modules. Keep all
  work inside `main()` under the main guard.
- `.github/workflows/codeguard.yml`: checkout, setup Python 3.12, pip install
  dependencies, `python ci.py`, upload `reports/codeguard.sarif` with
  `github/codeql-action/upload-sarif` on `if: always()`.
- `.pre-commit-config.yaml`: local hook running
  `python -m codeguard check . --changed HEAD --fail-on BLOCK --out reports`.
- After everything passes: `python -m codeguard check . --update-baseline`.
- `README.md`: layers table (1 to 9 plus overfitting stats and parity), quick
  start, wrapping user scripts (example adapter), MT5 and TradingView parity
  workflow (Python reference implementation; export trade lists; timezone and
  bar timestamp convention pitfalls; state that the MetaTrader5 Python package
  is believed to be Windows only and should be verified; Pine Script cannot
  run locally), scenario calibration note, rules that keep the harness honest
  (tests change only with a written reason in CHANGELOG.md, self checks must
  pass, trial ledger, add scenarios after live surprises, never remove the
  null test), Codeguard section, HTML layer section with the tagging
  contract. No em dashes in README prose.

### Definition of done
```
python -m codeguard gate .          -> GATE OPEN, 0 import failures
python -m pytest -q                 -> all pass (reference: 62 tests)
python run_stress.py --browser      -> 0 FAIL
python run_stress.py --buggy        -> FAILs in invariants, data_faults, html_reports
python ci.py                        -> PIPELINE PASSED
```
Report the actual counts and timings observed. Where a number here is
labelled "reference", treat it as a sanity range, not a target to force.
