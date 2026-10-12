# Gold / ES scaling strategy — Python and MT5

Converted from your **Gold / ES Scaling Scanner v2.8.0**. This is the separate
scaling strategy, with its own Python module and **Gold_ES_Scaling.ex5** MT5 reader.
It is independent of the daily confluence scanners, futures scalp methods and S1-S7.

The **Python-to-MT5 bridge** follows the workflow selected earlier: Python computes
and tracks the strategy; MT5 reads its feed, prints/alerts the latest events and
writes the full state JSON. Neither component places, changes or closes broker orders.
The MT5 file is not a native calculation engine or continuously running EA.

## What is preserved

- **Setup S:** Daily close acceptance/reversal or Daily realignment with Weekly;
  4H retest/structure filter and structure exits.
- **Setup A:** 2H EMA14, confirmed structure, impulse/correction, key/mid zone,
  candle/EMA confluence, pullback/retest/breakout priority.
- **Setup B:** 15m EMA50 rubber band, with the same setup rules on its own close.
- S/A/B share one instrument's weekly numbering and tracked P&L, exactly as the
  one-copy-per-instrument indicator does. They maintain separate trade arrays,
  cooldowns, re-entry flags, per-impulse caps and win/loss/BE records.
- Gold zones $100 +/-$15 and $50 +/-$10; ES zones 100 +/-10 and 50 +/-7.
  Gold round step $1, ES 0.25; swing-stop padding 1 and max distance 50.
- TP levels at successive majors, front-run by $10 Gold / 5 ES points and filtered
  by minimum 0.5R, up to five targets by default.
- Breakeven after +2 price units, pending-limit fills/cancellation, BOS exits,
  conditional re-entry and optional-partial TP alerts.
- Source equity/risk tiers: base 1%, then 1.5/2/3% at 25/50/100% growth, with
  starting equity 10,000, optional current-equity override and October 1 tracking
  start. Quantity rounding and ES micro equivalents match the source arithmetic.
- New York Sunday weekly reset, closed-bar timing, structure-violation alerts,
  and non-required stretch/continuation annotations.

S/A/B are components of **one** scaling strategy, not three unrelated strategies.
Splitting their account books would change its numbering and sizing rules. Run one
copy/feed per instrument. Gold and ES both work through their explicit profiles.

## Install Python

**In this repo** there is no `.venv` at the repo root. Use `Development\.venv`
(Python 3.14.7, NumPy 2.5.2, pandas 3.0.6; checked 2026-10-12, so nothing extra to
install). Run every command below from `Development/src/Algo-Scripts-Broker-Test-1`
after setting:

```powershell
$py = "..\..\.venv\Scripts\python.exe"        # Development\.venv
```

**From the standalone ZIP** (contains `Python/gold_es_scaling/` and `Scripts/`),
create an environment in the extracted `Python` folder and run from there:

```powershell
python -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install -r gold_es_scaling/requirements-lock.txt
```

The lock file records runtime dependencies; no broker SDK, credentials, API
subscription or network data provider is required by this strategy.

Entry scripts:

- `python -m gold_es_scaling` runs `cli.py` (via `__main__.py`): the standalone
  strategy CLI and optional read-only MT5 feed (`--mt5-feed`). No broker SDK.
- `python -m gold_es_scaling.demo --output <folder>` writes synthetic fixtures for
  both profiles.
- `python gold_es_scaling/package_delivery.py` checks the MetaEditor compile log
  (0 errors, 0 warnings), that the `.ex5` is newer than the `.mq5`, that the `.mq5`
  has no `OrderSend(` or `#include`, and that both CLI smoke runs in
  `validation/cli_smoke.json` succeeded. It then rewrites `validation/summary.json`
  and rebuilds `Gold_ES_Scaling_Python_MT5.zip`.

## Supply bars and run

Five CSVs are required: **15m chart/B, 2H A, 4H S, Daily and Weekly**. The program
uses those supplied higher-timeframe histories; it does not invent Weekly/Daily
bias from a short intraday file. Each CSV has:

```text
time,open,high,low,close,time_close
2026-10-08T14:00:00+00:00,4000,4005,3998,4002,2026-10-08T14:15:00+00:00
```

The row above is an example schema, not actual market data. `time` is bar open;
`time_close` is its actual closing timestamp. UNIX seconds/milliseconds are also
accepted. Header `timestamp` is accepted instead of `time`. Naive times require
`--timezone` (for example a real IANA zone); otherwise they are rejected. Bars must
be sorted, unique, non-overlapping and valid OHLC. Forming/future bars are excluded
at the chosen decision time.

**Use actual time_close values for session/DST accuracy**, especially Weekly bars.
If omitted, the loader assumes fixed 15m/2H/4H/24h/7d durations. That fallback can
misalign session and DST boundaries relative to TradingView. Do not relabel broker
server clock values as UTC; normalize them with the broker's historical clock rule.

Example command (replace paths with your actual files/data folder):

```powershell
& $py -m gold_es_scaling --chart data/XAUUSD_15m.csv --a data/XAUUSD_2H.csv --s data/XAUUSD_4H.csv --daily data/XAUUSD_D.csv --weekly data/XAUUSD_W.csv --profile Gold --broker-symbol XAUUSD --output output/scaling.json --mt5-feed 'C:\YOUR_MT5_DATA_FOLDER\MQL5\Files\gold_es_scaling.tsv'
```

For ES, use its five histories, `--profile ES` and its exact executable broker
symbol. `--profile Auto` follows the Pine ticker heuristic: XAU/GOLD/GC -> Gold,
otherwise ES. Explicit profiles are preferable for unusual names such as MGC.

`config/gold.json` and `config/es.json` select the default profiles. `--config`
accepts JSON overrides named after the `Config` fields in `strategy.py` and the
`Profile` fields. Example MES sizing override (ES price rules remain the same):

```json
{"profile_name":"ES","profile":{"name":"MES","value_per_point":5,"quantity_step":1,"micro_value":0}}
```

The quantities are the indicator's **research assumptions**, not broker-derived
sizes or centrally approved allocations. SPX500 CFDs and gold CFDs can have
broker-specific point values/lot steps; change the profile facts accordingly. The
program does not read your broker equity. It uses source tracked realized P&L or
an explicitly provided `current_equity` override.

## Install/run MT5

1. MT5 **File > Open Data Folder**.
2. Copy **Gold_ES_Scaling.ex5** from `Scripts/` into `MQL5/Scripts/StrategyExercise/`.
3. Refresh Navigator > Scripts; attach it to the matching instrument's **15m chart**.
4. Set `InpFeedFile` to the TSV filename inside `MQL5/Files` (default
   `gold_es_scaling.tsv`). The reader rejects another broker symbol, wrong version,
   stale/expired output, invalid field values and synthetic/replay feeds by default.
5. Read Toolbox > Experts and `MQL5/Files/gold_es_scaling_<symbol>_latest.json`.

The headline shows the first new entry if several S/A/B entries coincide. **Every
entry and management event**, all open trades/TPs/risk arithmetic, and all three
setup books are preserved in the JSON payload. `FLAT` means no new entry on that
bar; it does not mean all tracked trades are closed. Management-only events still
alert. Alerts are once per instrument/config/chart-close by default; terminal
Global Variables store only the last alert timestamp. `InpRepeatAlerts` allows an
explicit repeat. No quantity is sent to MT5 and no broker position is managed.

Refresh CSVs, run Python and rerun the reader after each closed 15m bar. Both are
one-shot commands; there is no installed scheduler. The feed expires after 300
seconds by default (`--ttl` / `InpMaxFeedAgeSeconds`). Bar data older than two chart
intervals is rejected in current mode. Full history retains missed earlier events,
but the MT5 reader alerts only the latest bar; it does not deliver an alert backlog.
Use distinct feed filenames and symbol-matching readers when running Gold and ES.

## Replay, simulation semantics and limits

`--asof TIMESTAMP_WITH_OFFSET` enables explicit **REPLAY**. `--synthetic` flags
fixture data, as do filenames containing `synthetic`/`fixture`. MT5 requires
`InpAllowDiagnosticData=true` to display those feeds; diagnostic alerts are suppressed.
The standalone source is preserved at `source/gold_es_scaling_v2_8_0.pine`.

Important source behaviors are intentionally retained:

- An existing stop is checked before same-bar BE/TP triggers. A newly moved BE
  stop is not retested until the next chart bar.
- A pending limit fill is not checked for a stop/TP/BE on that same fill bar.
- TP hits **only notify**; quantity is not reduced, no partial P&L is credited,
  and hitting the last TP does not close the whole tracker position. Full P&L is
  realized at the eventual stop/structure exit. A TP1 hit can count as a win even
  if the later exit loses money, exactly like the indicator's record.
- Zero-rounded quantity still produces/tracks the source signal and weekly number;
  it is marked below-minimum, with optional ES micro equivalent. It is not an order.
- Daily breakout acceptance in `f_daily` crosses the **level center**; A/B breakout
  acceptance crosses the **zone edge**. The code's thresholds take precedence over
  its shorthand comments. S impulse counters use closing-date IDs, as in the source.
- Weekly numbering resets without closing old trades; weekly trade numbers are
  display labels, not globally unique order IDs.
- The `[1]` Daily/Weekly bias offset is retained: a Daily close's new structure is
  used for bias on the next chart bar, not earlier on that Daily-close bar itself.

The engine replays the supplied histories from scratch on each run. Keep a fixed,
append-only input history when tracking P&L/numbering; changing the history start
changes indicator seeds, structure and book state. There is no broker reconciliation.
The supported chart is the source's recommended 15m/Setup B chart; custom lower
chart timeframes are rejected rather than treated as the same 15m signals. A/S
periods and EMA/pivot settings can be configured with compatible bar histories.

Closed HTF states are used between their closes. Realtime Pine `request.security`
unoffset values can be fluid before the HTF closes, so interim re-entry validity
can differ from a realtime Pine run. Pivot ties use a rightmost-equal convention.
**TradingView bar-by-bar parity and native MT5 runtime remain unverified.** Plot
boxes, colored stop lines, table layout, emoji and prose formatting are represented
by structured reports/events rather than reproduced graphically in MT5.

## Tests and demo

22 new scaling checks passed; with 11 existing futures-profile checks the focused
run passed **33 tests**. The new reader compiled with **0 errors and 0 warnings**.
Gold and ES CLI smoke runs each produced one synthetic breakout limit. These are
engineering checks, not trading-performance evidence.

With `$py` set as in Install Python:

```powershell
& $py -m gold_es_scaling.demo --output demo
```

This writes five clearly synthetic CSVs and a manual-bias demo config for each
profile. Run the main CLI with those five files, that config,
`--asof '2026-10-08T00:00:00+00:00' --synthetic`; inspect in MT5 only with explicit
diagnostic mode. The shipped `validation/cli_smoke.json` records the executed runs.

The separate key-level/Fibonacci indicator conversion is still awaiting its source.

Pine HTF timing reference: [TradingView repainting documentation](https://www.tradingview.com/pine-script-docs/concepts/repainting/).
