# Daily Instrument Scanner (Addendum v0.1)

Python implementation of **Daily Instrument Scanner Addendum v0.1** (October 2, 2026) on top of
**Trading Algorithm Specification v1.0** (September 30, 2026). Code version 0.1.0, config `cfg-0.1.0`.

Twice a day it scores 34 instruments in both directions against v1.0 confluences C1 to C6, adds a
COT overlay and a headline sentiment overlay (each capped at ±2), and writes an HTML dashboard of the
top 5 setups (qualified first, then developing). It places no orders and sizes no positions.

## Files

| File | Purpose |
| --- | --- |
| `scanner.py` | The scanner (single module) |
| `scanner_config.yaml` | Versioned parameters: universe, grids, COT codes, theme weights, keywords |
| `tests/test_scanner.py` | 39 unit tests (pivots, bias, impulse, zones, candles, COT, sentiment, ranking) |
| `requirements.txt` | Dependencies |

## Install

```
python -m venv .venv
.venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
pip install --upgrade git+https://github.com/rongardF/tvdatafeed.git
pip install transformers torch    # only if you want FinBERT sentiment now
```

Optional TradingView login (more history, fewer limits): set `TV_USERNAME` and `TV_PASSWORD`
environment variables. Without them the library runs in anonymous mode.

## Run

```
python scanner.py --demo                         # offline, synthetic data: check the install works
python scanner.py --run evening                  # live: TradingView + CFTC API + Forex Factory
python scanner.py --run preny
python scanner.py --daemon                       # stays running; fires at 00:05 and 12:05 UTC
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

## Outputs

- `output/scan_<UTC stamp>_<run>.html`: self contained dashboard (header with versions and data
  freshness, top 5 table, all candidates, every instrument with its rejection reason)
- `output/scan_<UTC stamp>_<run>.json` and `.csv`: every row, for later backtest comparison
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
