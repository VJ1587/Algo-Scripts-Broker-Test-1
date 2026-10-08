# Algo-Scripts-Broker-Test-1

Three scripts with separate jobs:

| Script | Job | Touches the market? |
| --- | --- | --- |
| `broker_v11_algo.py` | **Research.** Backtests the BROKER-v1.1 rules on historical or synthetic quotes. | No. Simulated orders only. |
| `scanner.py` | **Evaluation.** Scores live setups twice a day and says whether each is enough to take a trade (qualified, developing, or not shown). | Reads MT5 and TradingView. Places no orders. |
| `trade_gate.py` | **The hold.** The only path from a scanned setup to an order. Asks the owner before anything happens. | Only after the owner types CONFIRM, and only where algo orders are allowed. |

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

## TradingView indicator (`tradingview/kl_fib_confluence_setup.pine`)

A Pine Script v6 overlay for **The Complete Setup System**. Paste it into the Pine Editor, *Add to chart*, and use it on the **2H** chart (the table warns if you're on another timeframe). It draws and checks the nine checklist items:

| Step | What the indicator does |
| --- | --- |
| 1 Key levels | Boxes at every major (500 pips / $100 gold) and mid (250 pips / $50 gold) level. Box half-width is ±10 pips for FX; ±$10 for gold ($1 = 10 pips, so ±100 pips). On FX a fib level up to 10 pips outside a box is labelled NEAR BOX (developing), but does not count as the key level confluence. |
| 2 Range | Finds a consolidation whose top and bottom each have ≥2 separate touches (by default both must sit at key levels). Draws the range box; on a **close** beyond the box ± half-width it clones the box in the breakout direction as the target. Signals are suppressed while price is inside an unbroken range. |
| 3–4 Bias | Daily and 4H structure from swing pivots: HH+HL = bullish, LH+LL = bearish, otherwise ranging. Fib setups are only taken when both agree. Uses the last closed HTF bar (no repainting). |
| 5 Impulse | Daily sideways or 4H disagreeing = no Fib. When both agree, the Fib goes on the most recent clean impulse in the Daily direction, found on the 2H whatever chart you are on. Clean = swing low → swing high (or high → low), at least 2.5 × ATR, 3 to 40 candles, 60%+ closing in the move direction, average body 50%+ of range, slope 50%+ clean. A move that escapes a tight range (under 4 ATR over the 20 bars before A) or blows through a major key level box is a breakout: no Fib until price retests the broken level, then the Fib runs from the retest low (high) to the breakout high (low). A setup is dropped on a close beyond 78.6 or B, when the bias stops agreeing, or 60 2H bars after the impulse with no entry. |
| 5b Daily fib | The same impulse rules on the Daily, drawn in purple for the big picture. Context only: signals come from the 2H fib. The checklist shows whether the 2H entry zone sits inside the Daily 38.2–61.8. |
| 6 Fib ladder | 38.2 / 50 / 61.8 buy or sell limits with small / medium / large lot weights (1:2:3) and sizes from account and risk %. SL a few pips beyond 61.8. A close beyond 78.6 marks the setup invalid. |
| 7 Confluence | Fib zone reached, fib level inside a key box, higher low / lower high forming in the zone, confirmation candle (engulfing, hammer / shooting star, morning / evening star) at the zone. A signal needs fib + key box + candle (3); with structure it is graded A (4/4). |
| 8 Manage | TP1 at -0.27 (move SL to breakeven), TP2 at -0.618. |

The checklist table sits bottom left by default. Settings › Display can hide it, move it, change its text size, or drop the detail column for a narrow version.

Alerts: long setup, short setup, price entered fib zone, range breakout, TP1 hit, setup invalid / stopped (plus one `alert()` message with entry, SL and targets).

Zones match the scanner: FX ±10 pips (cfg-0.12.0), gold ±$10 and S&P ±10 points (cfg-0.10.0 / 0.11.0); on S&P "pips" in the table are index points. Lot sizes assume the quote currency is the account currency (true for EURUSD, GBPUSD, XAUUSD with a USD account).

# Daily Instrument Scanner (Addendum v0.1)

Python implementation of **Daily Instrument Scanner Addendum v0.1** (October 2, 2026) on top of
**Trading Algorithm Specification v1.0** (September 30, 2026). Code version 0.9.0, config `cfg-0.9.0`.

Twice a day it scores 34 instruments in both directions against v1.0 confluences C1 to C6, adds a
COT overlay and a headline sentiment overlay (each capped at ±2), and writes an HTML dashboard of the
top 5 setups (A, then B, then developing) plus every developing setup. It places no orders and
sizes no positions.

Setups are graded with the owner's **Position Trading Confluence System** (cfg-0.8.0); see
Setup rules below. COT and sentiment affect ranking only. The full rule set is in the `scanner.py`
docstring and the config changelog.

### Confluences C1 to C6

The names and definitions live in `CONFLUENCES` in `scanner.py`. The dashboard, the journal page,
`logs/trade_journal.csv` and the trade gate all use them, and each scan JSON stores the definitions it ran with.

| Check | Name | Passes when |
|---|---|---|
| C1 | Trend alignment | Daily bias and 4H structure (HH/HL bullish, LH/LL bearish) point in the trade direction; if they conflict, no trade. Gold uses Weekly and Daily instead (no 4H). |
| C2 | Key level zone | Price is at or around a major or mid level: the latest completed 2H close is inside that level's zone. FX majors every 500 pips (1.3000), mids halfway (1.3250), ±10 pips; JPY pairs every 5.00 and 2.50, ±0.10; gold and S&P every 100 and 50, ±10; oil every 5.00 and 2.50, ±1.00. |
| C3 | Fibonacci retracement | Latest 2H close is within the ATR zone width of the 50% or 61.8% (golden, primary) or 38.2% (valid, lower conviction) retracement of the most recent clean 4H impulse. Stacked: a 50% or 61.8% level inside the C2 zone. |
| C4 | Reversal at the zone | A reversal that started in the key level zone and closed on the Daily, 4H or 2H chart: hammer, inverted hammer, shooting star, hanging man, engulfing, tweezer, morning or evening star, reversal + marubozu, or a double top/bottom or head and shoulders closed beyond the neckline. |
| C5 | 2H EMA momentum | On the 2H chart the 8 EMA is above the 14 EMA for a long, below it for a short. |
| C6 | Market structure | 4H structure holds at entry: the latest 4H swing low is a higher low (a lower high for a short) and no 2H close has broken it since. A close through it means structure is compromised: skip. |

### Setup rules

These live in `SETUP_RULES` and `grade_setup` in `scanner.py`. A and B setups carry
`section: qualified` plus `grade: A` or `B` in the scan files.

| Rule | Definition |
|---|---|
| A setup | C1 + C2 + C3 at 50% or 61.8% stacked inside the C2 zone + one or more of C4, C5, C6 (4+). Full size; limit order inside the zone at the Fib level; no need to wait for C4 when C1 + C2 + C3 stack. More confluences beyond 4 allow larger size. |
| B setup | C1 + (C2 or C3 at 50%/61.8%) + one of C4, C5, C6 (3). Smaller size; a limit order is still valid if C1 + C3 are clean. |
| Minor pairs | FX crosses: only a strong A setup with a closed C4 reversal candle counts. Do not rely on limit orders alone; size smaller than on majors. |
| S&P and oil | SPX500, ES, WTI and CL: only an A setup counts; a B setup is listed as developing. |
| Gold | B minimum is C1 + C2 + C3 (size very small); A adds a 50%/61.8% Fib stacked in the zone and a C4 close (morning star or bullish engulfing preferred). C1 is Weekly and Daily, no 4H. DXY falling is extra conviction for longs; check the Fed and safe-haven news. |
| Stop and targets | Stop at the 78.6% or 89% retracement; a close beyond it means the retracement went too deep. TP1 at the -27% extension (take 50-75% off), TP2 at -61.8%; after TP1 move the stop to breakeven. |
| Developing | Any 2 or more of C1-C6 that do not make a B setup, or an FX close up to 10 pips outside a key level zone plus one other check ("near zone", cfg-0.12.0). Watch only. |

Owner decisions (October 7, 2026): majors are the 7 USD majors in the config (EURUSD, GBPUSD, USDJPY,
USDCHF, USDCAD, AUDUSD, NZDUSD) and every other pair is a minor; gold C1 is Weekly + Daily without the
4H; S&P and oil trade A setups only. C4 keeps the full owner pattern list (cfg-0.3.0, cfg-0.4.0), and
every candle and chart pattern counts only at a key level zone; chart patterns name the level they
formed at. The trade gate already uses the system's targets (TP1 -27 %, TP2 -61.8 %) and the 89.3 %
stop for FX; it does not yet move the stop to breakeven after TP1.

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
python scanner.py --run intraday                 # one run between the two main runs
python scanner.py --daemon                       # stays running; every 2 hours at :05 UTC
python scanner.py --journal                      # rebuild only the journal and balance page from MT5
python scanner.py --source csv --asof 2026-10-02T12:05Z   # point in time from your own CSV bars
python -m pytest -q                              # tests
```

`--run auto` (the default) picks evening before 06:00 or after 18:00 UTC, pre NY otherwise.

### Scheduling (cfg-0.9.0)

`python scanner.py --daemon` runs every 2 hours at 5 past the hour, UTC (`runs.every_hours`, `runs.minute`),
just after each 2H bar closes: an hourly run between two closes would see the same bars. 00:05 is the
evening run, 12:05 pre NY, and the rest are intraday runs, which keep the Daily bias from the last evening
run as pre NY does. Times are fixed in UTC so they never drift with daylight saving (00:05 UTC is 7:05 PM
CDT or 6:05 PM CST). The PC must be on and the MT5 terminal open and logged in.

- **Alerts.** When a setup is newly graded A or B, or a B becomes an A, Windows shows a notification;
  clicking it opens that dashboard (`alerts.windows_toast`). The scanner log records each one.
- **Calendar.** The Forex Factory feed refuses rapid repeat requests, so a snapshot is reused for 4 hours
  (`calendar.refresh_hours`) and used whenever a fetch fails.
- **Disk.** A run writes about 240 KB. Intraday HTML and CSV files are deleted after 14 days
  (`output.keep_intraday_html_days`); evening and pre NY dashboards and every JSON file are kept, because
  the journal matches trades to them.

To start the daemon at login, create a Windows Task Scheduler task "At log on" that runs
the virtual environment's `pythonw.exe` (here `Development\.venv\Scripts\pythonw.exe`) with the argument
`scanner.py --daemon` and this folder as the start-in folder.

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
| Zones Z01, C1 to C6 | `in_psych_zone`, `candle_signal`, `structure_holding`, `grade_setup`, `score_instrument` |
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
   half-width `grids.<grid>.zone_half_width`: FX 0.0010 (10 pips; was 15 before cfg-0.12.0), JPY pairs 0.10, gold $10 (XAUUSD
   and GC; $1 = 10 pips, so +/-100 pips; was $20 before cfg-0.10.0), S&P 10 points (was 20 before cfg-0.11.0), oil $1.00
   (placeholder; a $10-20 box would overlap the $2.50 oil grid). C2 and the fib ladder test against this zone. A grid with `zone_half_width: null` falls back
   to the ATR width and is flagged "zone width not set". Chart-pattern (C4) tolerances scale from
   `pattern_zone_half_width` when set: FX keeps 15 pips (JPY 0.15), gold and S&P keep 20, so narrowing
   the zones (cfg-0.10.0 to 0.12.0) did not change pattern detection.
5c. **Near zone (cfg-0.12.0, FX only).** A 2H close outside the zone but within `near_zone_width`
   (10 pips) of its edge does not score C2. With at least one other check the setup is listed as
   developing, with a note such as "Near key level: 4.0 pips outside the 1.3 major zone; becomes B on a
   2H close inside the zone". It is never graded A or B. C3 tolerance still uses the ATR width.
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
5e. **Grades (cfg-0.8.0).** A, B and developing as in Setup rules above. cfg-0.4.0 required C4 for every
   qualified setup; an A setup no longer does. On equal totals a Daily confirmation ranks above 4H, and
   4H above 2H.
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
