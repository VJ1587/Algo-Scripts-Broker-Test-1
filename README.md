# Algo-Scripts-Broker-Test-1

Four scripts with separate jobs:

| Script | Job | Touches the market? |
| --- | --- | --- |
| `personality.py` | **The game.** Empire Game Market Personality engine: who gains, how each instrument normally behaves, where the fight should happen. Daily cards, monthly and quarterly reviews that validate or invalidate earlier calls. | Reads MT5, TradingView, FRED and CFTC. Places no orders. |
| `broker_v11_algo.py` | **Research.** Backtests the BROKER-v1.1 rules on historical or synthetic quotes. | No. Simulated orders only. |
| `scanner.py` | **Evaluation.** Scores live setups twice a day and says whether each is enough to take a trade (qualified, developing, or not shown). | Reads MT5 and TradingView. Places no orders. |
| `trade_gate.py` | **The hold.** The only path from a scanned setup to an order. Asks the owner before anything happens. | Only after the owner types CONFIRM, and only where algo orders are allowed. |

## Empire Game Market Personality engine (`personality.py`)

Implements the *Empire Game Market Personality Framework* (October 6, 2026). Package `empire/`, one module per
layer of the framework's data model (data, traits, regimes, pairs, levels, push, players, events, stories,
journal, review, report, monitor). Analytical only: it never places orders.

**Cadence.** `python personality.py` runs the daily loop. On the first run of a new month it also writes the
monthly review for the month just ended, and on the first run of a new quarter the quarterly review. Each
report reads the journal the earlier ones wrote, so calls are checked against what happened.

| Run | What it does |
| --- | --- |
| Daily | Regime (dollar liquidity x risk appetite) and scoreboard (dollar index vs equal-weight dollar). Calendar windows. Events: event study, cross-market fingerprint, needle test, event weight, pressure. Character: eight traits, bands from each trait's own 20-year history, one distance-from-normal number, drift, relationship flips. Incentive gaps for pairs. The 20-year level map scored against random levels. One story card per instrument: likelihoods (base rates tilted by the ledger and pressure), reach, hold, EV at entry, four gates, grade, setup type, invalidation, long-term goal vs short-term behavior, alignment against **every configured scanner**. Exposure check. Every forecast is logged before the outcome and resolved after it. |
| Monthly | Forecast scorecard (Brier, skill vs base rates, calibration buckets, hit rates by setup, grade, gates and alignment cell per scanner). Every written behavior in the ledger re-tested: validated, regime-dependent, invalidated or inconclusive, with status changes since last month. Measured profile vs last month. Plan alignment scores and pivot triggers. Event library; pressure half lives refit. |
| Quarterly | Everything monthly plus the validation tests: round and market-made levels vs random levels, trait rankings out of sample (first 14 years vs last 6), pair math identity, incentive gap closure. Recalibrates T and lambda on the forecast log (after 30 resolved), nudges grade thresholds (after 30 per grade), retires setups below a coin flip, fires the pivot triggers (for example dropping the round-number grid if it shows no edge). |
| Annual | The quarterly tests (no second recalibration) and personality year by year over the whole history. |
| Evolution page | Second page of every quarterly and annual review: each instrument's phase per year and per quarter (stable, known phase, new phase, complete change, behavior flips) next to elections, changes of party control, policy regimes and market regimes. |

All recalibration applies automatically (owner decision) and is logged with its reason and evidence in
`data/personality/params_changelog.csv`. Ledger edits are snapshotted and diffed on every run.

**Inputs**

| File | You maintain | Notes |
| --- | --- | --- |
| `personality_config.yaml` | Universe, sources, windows, thresholds, scanners to compare against | Versioned like `scanner_config.yaml`. Source order per instrument: MT5, TradingView, then FRED for gaps. |
| `personality_ledger.yaml` | Players and weights, incentive scores (B, K, U, C), levers, pain zones, plans and goals, hypotheses | Every entry carries an evidence label. `unverified` and `unknown` entries show on cards but are excluded from likelihoods. Seeded from the doc: **review every seed before trusting a grade.** |
| `personality_events.yaml` | Wars, sanctions, policy shifts, confirmed headlines | Red-folder releases come in automatically from the scanner's calendar archive. |
| `ledger/politics.yaml` | Heads of government and state, central bank heads, elections, changes of control, policy regimes | Seeded from memory and `unverified`; the evolution page lists the least certain entries. Does not feed likelihoods. |
| `ledger/cycle_scorecard.yaml` | The 63 cycle inputs (9 players x 7 indicators): definitions, sources, refresh windows, values | Merged into the ledger players on load, so edits are snapshotted and diffed with the ledger. |

**Scanners.** Add one entry per scanner under `scanners:` in the config. `scanner_json` reads `scanner.py`
output; `generic_csv` and `generic_json` read any scanner that writes `symbol, direction, status`. Each card
shows its alignment cell (aligned, watch, tactical, conflict, no trade) per scanner, and the reviews track
results per cell, which is the framework's decisive test of whether the story layer adds value.

**Outputs.** `output/personality/daily_<date>.html|json`, `monthly_<YYYY-MM>.*`, `quarterly_<YYYY-Qn>.*`, `annual_<YYYY>.*`, `evolution_<period>.html`.
The journal lives in `data/personality/` (git-ignored, so back it up: it is the system's memory).

```powershell
python personality.py --demo                  # synthetic data, no network: check the install
python personality.py                         # live: daily, plus monthly/quarterly when due
python personality.py --run quarterly         # force a review now
python personality.py --asof 2026-10-06       # point in time
python personality.py --daemon                # stays running; daily at schedule.daily_utc on weekdays
python -m pytest tests/test_personality.py -q
```

Windows Task Scheduler alternative to `--daemon`: a daily trigger Monday to Friday after the New York close
running `python personality.py` in this folder (with MT5 open and logged in).

**Before relying on it**

- Check the data table at the bottom of the first live daily report: it shows which source each series came
  from and any splice or quality issues. TradingView and FRED codes in the config are best knowledge, not verified.
- Work through the ledger seeds. Weights are judgment; pain zone levels came from memory and start as `unverified`.
- Expect most cards to grade C or none. Without confirmed events touching an instrument, pressure is zero and
  nothing can grade A. That is the discipline working.
- T starts high (2.0), keeping likelihoods close to the base rates until 30 forecasts resolve.

## Trade gate (the hold)

Run it after a scan. For each qualified setup in the latest live scan it asks, in order:

1. **Take this trade?** A no is logged as skipped.
2. **Which account(s)?** From `accounts.yaml`: MT5 accounts and manual-only accounts (for example a
   futures broker).
3. **Modifications?** The ticket starts from BROKER-v1.1 rules (ladder 38.2 / 50 / 61.8 % of the
   impulse, FX stop at 89.3 %, TP1 / TP2 at the 27 % / 61.8 % extensions, lots from 2 % idea risk
   inside the 5 % portfolio cap, rounded down). Change any value with `field=value`, for example
   `stop=1.1180 tp1=1.1650 lots=0.10,0.15,0.25 route=M`. A setup with no impulse starts as a market
   entry at the live price, and you set the stop and targets: v1.0 defines no levels without one.
4. **Who places it?** `m` manual (you place it on the platform) or `a` algo (the gate sends it to MT5).
5. **Final ticket.** Type `CONFIRM` exactly; anything else cancels.

```powershell
copy accounts.example.yaml accounts.yaml   # then list your accounts (git-ignored, stays local)
python trade_gate.py                        # latest live scan, qualified setups
python trade_gate.py --include-developing   # also show developing setups (2+ checks), with a warning
```

Safety rules:

- **Demo first.** Algo orders need `allow_algo: true` on the account. A real-money account also
  needs `allow_live_algo: true` at the top of `accounts.yaml`. Algo Trading must be on in the MT5
  terminal, and the terminal's login must match the configured one. Manual tickets work everywhere.
- **Risk ceilings.** Algo orders above 2 % idea risk or the 5 % portfolio cap are refused. A manual
  ticket above them needs the word `OVERRIDE`, and the override is logged.
- **All or nothing.** Every order passes MT5 `order_check` before any is sent. If a later order is
  rejected, the gate offers to remove the ladder legs already placed.
- **Each leg goes as two orders,** half to TP1 and half to TP2, because MT5 holds one target per
  order. Unfilled ladder legs expire after 24 h (v1.0: six 4H bars). The gate does not trail the stop.
- **Everything is logged:** skipped, cancelled, approved (manual), sent and failed decisions go to
  `logs/trade_decisions.csv` and `tickets/<id>.json`, and show on `output/journal.html`. Orders sent
  by the gate carry magic number 5110001 and the ticket id in the order comment.

## Red-folder news (Forex Factory)

`news.py` reads the Forex Factory calendar (https://www.forexfactory.com/calendar; High impact = red
folder) through its weekly data feed, because the HTML page blocks scripts. The feed has date, time,
currency, impact, forecast and previous, for the current week only and without actual values.

- **Scanner dashboard:** upcoming red events with the instruments they affect, and how recent red events
  moved each currency (15m / 1h / 4h, from MT5 5-minute bars, logged to `logs/news_reactions.csv`).
- **Trade gate:** lists red events for the trade's currencies and blocks algo orders within 30 minutes
  of one (v1.0 Section 9). Manual tickets are allowed with a warning.
- **Backtest:** `python news.py --export-backtest data/news/ff_high_impact.csv` writes the file for
  `broker_v11_algo.py --news`. History starts with the first saved calendar snapshot.

```powershell
python news.py --upcoming     # red events for the rest of this week
```

## Broker backtest script (research)

`broker_v11_algo.py` packages the BROKER-v1.1 research and backtest script (release v1.0.0). It is a
research specification, not a validated profitable trading system. Instrument specs in
`placeholder_catalog()` are illustrative only: replace them with verified broker contract data before
relying on any result.

```powershell
python broker_v11_algo.py --selftest             # built-in worked example and checks
python broker_v11_algo.py --demo --route both    # synthetic data, plumbing check only
python broker_v11_algo.py --data-dir ./quotes --route L --news news.csv --out results
```

See the script header for the CSV format used by `--data-dir` and `--news`.

# Daily Instrument Scanner (Addendum v0.1)

Python implementation of **Daily Instrument Scanner Addendum v0.1** (October 2, 2026) on top of
**Trading Algorithm Specification v1.0** (September 30, 2026). Code version 0.7.0, config `cfg-0.7.0`.

Twice a day it scores 34 instruments in both directions against v1.0 confluences C1 to C6, adds a
COT overlay and a headline sentiment overlay (each capped at ±2), and writes an HTML dashboard of the
top 5 setups (qualified first, then developing) plus every developing setup. It places no orders and
sizes no positions.

Qualification: qualified = C1 + C4 (a reversal closed in a key level zone, 2H or higher) + at least 3
of C1-C6 (cfg-0.4.0). Developing = any 2 or more of C1-C6 that do not qualify (cfg-0.7.0). COT and
sentiment affect ranking only. The full rule set is in the `scanner.py` docstring and the config
changelog.

### Confluences C1 to C6

The names and definitions live in `CONFLUENCES` in `scanner.py`. The dashboard, the journal page,
`logs/trade_journal.csv` and the trade gate all use them, and each scan JSON stores the definitions it ran with.

| Check | Name | Passes when |
|---|---|---|
| C1 | Trend alignment | Daily bias and 4H structure (latest two highs and lows both rising for a long, both falling for a short) point in the trade direction. |
| C2 | Key level zone | Latest completed 2H close is inside the zone around a major or mid round-number level (FX ±15 pips, JPY pairs ±0.15, gold and S&P ±20, oil ±1.00). |
| C3 | Fibonacci retracement | Latest 2H close is within the ATR zone width of the 38.2%, 50% or 61.8% retracement of the latest qualifying 4H impulse. |
| C4 | Reversal at the zone | A reversal that started in the key level zone and closed on the Daily, 4H or 2H chart: hammer, inverted hammer, shooting star, hanging man, engulfing, tweezer, morning or evening star, reversal + marubozu, or a double top/bottom or head and shoulders closed beyond the neckline. |
| C5 | 2H EMA momentum | On the 2H chart the 8 EMA is above the 14 EMA for a long, below it for a short. |
| C6 | Trend line | Latest 2H candle touches the unbroken 4H trend line (last two rising lows for a long, last two falling highs for a short) and closes on the trend side. |

The journal page labels each trade with its setup (the checks passed in the last scan before entry,
for example `C1+C4+C5`) and shows win rate and net P/L by section, by setup and for each confluence
with versus without. Under about 20 closed trades a group is too small to judge.

## Files

| File | Purpose |
| --- | --- |
| `scanner.py` | The scanner (single module) |
| `scanner_config.yaml` | Versioned parameters: universe, grids, COT codes, theme weights, keywords |
| `tests/test_scanner.py` | Unit tests (pivots, bias, impulse, zones, candles, COT, sentiment, ranking, MT5 time and routing, journal) |
| `requirements.txt` | Dependencies |

## Install

```
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt  # includes MetaTrader5 on Windows
pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git
pip install transformers torch    # only if you want FinBERT sentiment now
```

MetaTrader 5 needs Windows and the MT5 desktop terminal installed, open and logged in to the broker
account. Without it (or with `mt5.enabled: false`) every instrument uses TradingView and the
dashboard and journal note that MT5 is unavailable.

Optional TradingView login (more history, fewer limits): set `TV_USERNAME` and `TV_PASSWORD`
environment variables. Without them the library runs in anonymous mode.

## Run

```
python scanner.py --demo                         # offline, synthetic data: check the install works
python scanner.py --run evening                  # live: TradingView + CFTC API + Forex Factory
python scanner.py --run preny
python scanner.py --daemon                       # stays running; fires at 00:05 and 12:05 UTC
python scanner.py --journal                      # rebuild only the journal and balance page from MT5
python scanner.py --source csv --asof 2026-10-02T12:05Z   # point in time from your own CSV bars
python -m pytest -q                              # tests
```

`--run auto` (the default) picks evening before 06:00 or after 18:00 UTC, pre NY otherwise.

### Scheduling

Run times are fixed in UTC so they never drift with daylight saving (evening 00:05 UTC is 7:05 PM CDT
or 6:05 PM CST; pre NY 12:05 UTC is 7:05 AM CDT or 6:05 AM CST). The simplest option is
`python scanner.py --daemon` started at login. Windows Task Scheduler and plain cron use local time,
so if you use them, either update the trigger at each DST change or set two triggers per run and let
`--run auto` decide. On Linux with cronie: `CRON_TZ=UTC` then `5 0 * * *` and `5 12 * * *`.

## MetaTrader 5 (cfg-0.5.0, read only)

Open and log in to the MT5 terminal before a run; the scanner attaches to it (`mt5.enabled` in the
config). Nothing in the scanner sends, changes or closes orders.

- **Currency pairs** use Forex.com bars from MT5 (`bars.fx_source: mt5`). **CFDs and futures** always
  use TradingView.
- Before a pair's first MT5 fetch the scanner checks the broker's history depth (250+ Daily bars and
  enough 1H bars for 250 4H bars). A pair that fails the check or the fetch comes entirely from
  TradingView, so one pair never mixes feeds. The dashboard's Bars column and the MT5 line in the
  header show which pairs fell back and why. As of 2026-10-06 Forex.com keeps too little Daily history
  for AUDJPY, CADJPY, CADCHF, AUDNZD and AUDCHF.
- MT5 stamps time in server time. `mt5.server_time: ny_close` converts it (server = New York + 7h);
  each run checks a live tick against that rule and warns in the header if they disagree.
- The first request for a pair makes the terminal download its history, which can take minutes.
  Later runs read it from the terminal's cache.
- Broker symbol names that differ from the scanner's go in `mt5.symbol_map`.

## Outputs

- `output/scan_<UTC stamp>_<run>.html`: self contained dashboard (header with versions and data
  freshness, top 5 table, all candidates, every instrument with its rejection reason)
- `output/scan_<UTC stamp>_<run>.json` and `.csv`: every row, for later backtest comparison
- The dashboard also shows MT5 open positions and broker bid, ask and spread for the 28 pairs
- `output/journal.html`: account balance, equity and margin; a balance snapshot per run; every MT5
  position (from deal history, `journal.history_days`) with open and close time, prices, net P/L, and
  the confluences the last live scan before the entry logged for that instrument and direction
- `logs/mt5_account.csv`: balance snapshot log; `logs/trade_journal.csv`: the journal as a table
- `logs/c2_rejections.csv`: C2 pass/fail and distance to the nearest level per instrument per run
- `logs/scanner.log`: run log, including the CFTC market name returned for each COT code
- `data/calendar_snapshots/`: Forex Factory snapshots (the feed keeps no history)
- `data/headlines/`: headline archive (sentiment backtests start from the first capture date)
- `data/cot/cot_latest_pull.csv`: last COT pull

## How the code maps to the documents

| Spec item | Where |
| --- | --- |
| Completed bars only, 250 bar checks, OHLC sanity (v1.0 §2) | `completed`, `check_bars` |
| EMA seeded with SMA, Wilder ATR14 (v1.0 §2) | `ema`, `atr` |
| Strict 2 bar pivots, confirm at k+2, alternation (v1.0 §2) | `find_pivots`, `add_alternating` |
| Daily bias D01, 4H agreement D02 | `daily_bias`, `structure` |
| Impulse I01 (6 bar recency, 3 to 30 bars, 2×ATR, efficiency 0.60, break of prior swing) | `select_impulse` |
| Zones Z01, C1 to C6 | `in_psych_zone`, `candle_signal`, `trendline_signal`, `score_instrument` |
| COT index, pair mapping, ±2 table (Add 6.2) | `cot_for_instrument`, `cot_points` |
| Sentiment S, themes, direction rules (Add 6.3, 7) | `SentimentEngine` |
| Calendar countdown and event risk (Add 6.4) | `next_event` |
| Sections and ranking (Add 8) | `score_instrument`, `rank` |
| Dashboard and machine readable copy (Add 9) | `render_html`, `write_outputs` |

## Implementation choices the documents leave open (test defaults, all flagged `[IMPL]` in code)

1. **Bar anchoring.** TradingView's native 2H and 4H bars follow venue sessions, not 00:00 UTC. The
   scanner rebuilds 2H and 4H from 1H bars on a UTC grid (`intraday_mode: resample_1h`). Daily bars
   stay native because 5,000 1H bars cannot make 250 Daily bars, so Daily follows TradingView's
   session (for FX I believe this is 17:00 New York; please verify). This is a documented deviation
   from v1.0 §2.
2. **Pre NY Daily freeze.** The pre NY run uses only Daily bars complete at that day's 00:05 UTC.
3. **Bias re-arm guard.** After invalidation a bias cannot reinitialize while the close is already
   beyond the new invalidation level (otherwise the broken bias re-arms on the same bar).
4. **Impulse void.** An impulse is dropped once a 4H close passes its origin A (config switch).
5. **No impulse.** If no impulse qualifies, C3 is false and zone width uses the latest 4H ATR.
5a. **Key levels are zones (cfg-0.2.0).** Every major and mid grid level is a zone of fixed
   half-width `grids.<grid>.zone_half_width`: FX 0.0015 (15 pips), JPY pairs 0.15, gold $20 (XAUUSD
   and GC), S&P 20 points, oil $1.00 (gold's 20% of major spacing; $20 would overlap the $2.50 oil
   grid). C2 and the fib ladder test against this zone. A grid with `zone_half_width: null` falls back
   to the ATR width and is flagged "zone width not set". C3 and C6 tolerance still use the ATR width.
5b. **Wick principle (flag only).** The scanner counts completed 2H candles in the last 6 whose wick
   reaches into the zone and is rejected in the trade direction (wick at least the body and the
   opposite wick, close not through the zone). Two or more adds a "zone tested: N wicks" flag. It never
   changes C1 to C6, the score, or qualification. Wait for the reversal candle to close (C4).
5c. **C4 candlestick patterns, zone only (cfg-0.3.0).** C4 passes when the just-closed 2H candle
   completes a reversal pattern from the candlestick guide and the pattern's candles traded inside the
   key level zone. Outside a zone C4 is always false. Long: hammer, inverted hammer, bullish engulfing,
   tweezer bottom, morning star (an engulfing candle must itself wick into the zone; its close may
   finish beyond it). Short: shooting star, hanging man, bearish engulfing, tweezer top,
   evening star. Pins, tweezers and stars need the matching trend before them (2H close vs 6 bars
   earlier). A directional marubozu right after a zone pattern also counts ("hammer + marubozu").
   Doji alone is indecision and does not count. Thresholds live under `features.candle`.
5d. **C4 timeframes and chart patterns (cfg-0.4.0).** C4 checks the latest completed Daily, then 4H,
   then 2H bar and keeps the highest timeframe found (shown as e.g. "4H hammer"). On Daily and 4H it
   also checks double bottom/top and (inverse) head and shoulders: bottoms/tops or shoulders within
   100 pips (Daily) / 40 pips (4H) on FX, at least 5 bars apart, head at least 50 / 20 pips beyond both
   shoulders, the bottom or head in a key level zone, and a candle CLOSE beyond the neckline within the
   last 3 bars (a wick does not count). Distances scale with each instrument's zone width
   (`features.chart_patterns`).
5e. **Qualification (cfg-0.4.0).** Qualified = C1 + C4 + at least 3 checks: no setup qualifies without
   a reversal closed in a key level zone. Developing (cfg-0.7.0) = any 2 or more of C1-C6 that do not
   qualify. On equal totals a Daily confirmation ranks above 4H, and 4H above 2H.
   `qualification.require_close_in_zone: true` would also demand the latest close sit inside the zone.
6. **COT publication time.** Assumed position date + 3 days at 15:30 Eastern; holiday delays not
   modelled. Live, the CFTC API only returns published data, so this matters mainly for CSV
   backtests. Cross pair "weekly change" is the change in the rescaled cross index.
7. **Sentiment mapping.** Gold is treated as XAU against USD (a hawkish Fed headline weighs on gold
   through the USD leg). Gold auto includes conflict headlines; oil auto includes energy supply
   headlines and conflict headlines naming a producing region. "Thin news" uses the sum of theme
   weights without recency. JPY and CHF get the higher conflict weight only; no safe haven sign flip
   was specified, so none is applied.
8. **Hawk/dove and supply direction** come from keyword lists in config, versioned with the weights.

## Open items before treating output as validated (Addendum §10)

- [ ] Choose headline feeds and confirm each allows scripted access (`sentiment.feeds` is empty, so
      sentiment scores 0 with "thin news" until you add feeds)
- [ ] Confirm TradingView provider symbols for XAUUSD, SPX500 (`SPX500USD`), WTI (`WTICOUSD`)
- [ ] Decide COT access: CFTC API (default) or the Codex scraper (`cot.source: csv`, columns
      `market,report_date,publication_ts,long,short,open_interest,report_family`)
- [ ] Verify the COT contract codes against the market names in `logs/scanner.log`
- [ ] Confirm S&P theme weights and the "overlays rank, never qualify" rule
- [ ] Verify the CFTC publication schedule and holiday delays
- [ ] Verify the approximate futures roll windows in `roll_rules`
- [ ] Backtest the CL $5 grid; review `logs/c2_rejections.csv` for the 500 pip FX grid

## Known limitations

TradingView (tvDatafeed) and Forex Factory access are unofficial and may stop without notice or
conflict with their terms; the owner accepts this risk per the addendum. FinBERT's tone reading of FX
and commodity headlines is untested. The demo mode uses synthetic data and a crude word list for
tone; its output says nothing about markets.

## Verified during build (October 2, 2026)

- CFTC Socrata datasets `gpe5-46if` (TFF futures only) and `72hh-3qpy` (Disaggregated futures only)
  respond, and the fields `lev_money_positions_long/short`, `m_money_positions_long_all/short_all`,
  `open_interest_all` exist. Codes 099741 (Euro FX) and 067651 (WTI) returned the expected markets;
  the other codes are believed correct but were not individually confirmed.
- Forex Factory JSON fields are `title, country, date, impact, forecast, previous`.
- tvDatafeed returns naive local timestamps via `datetime.fromtimestamp`; the scanner converts back to
  UTC. Its `n_bars` maximum is 5,000.

Governance: change any threshold, weight, spacing or cap by bumping `config_version` and adding a
`changelog` line in the config (v1.0 §12).
