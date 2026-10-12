# Separate MT5 strategy scripts

23 individual MT5 scripts: 15 technical scanners, seven institutional Python signal readers and the independent Gold / ES scaling strategy converted from this project's `scanner.py`
(cfg-0.13.0) and the ES/NQ/CL/GC rules in `futures_scalp_scanner.py` (scalp-cfg-0.1.0).
Every file contains one method. The daily/futures scanners are standalone MQL5 calculations.
The seven ITS methods use their original Python engines and individual read-only MT5 consumers.
No file combines strategies or submits orders.

## Gold / ES scaling addition (owner Pine v2.8.0)

**Gold_ES_Scaling.mq5 / .ex5** is the actual Gold / ES scaling strategy, independent
of the futures scalp methods and S1-S7. It uses a dedicated Python engine and
read-only MT5 feed. Setup S (Daily + 4H), Setup A (2H EMA14) and Setup B (15m EMA50)
share the source strategy's instrument book, weekly numbering and tracked equity.
Scaling caps, pending limits, risk tiers, BE, re-entry and TP/structure events are
preserved. It submits no orders. **Run its Python command before its MT5 reader.**

The combined ZIP includes `GoldESScaling/Gold_ES_Scaling_Python_MT5.zip`, with its
complete independent Python source, `.mq5`/`.ex5`, original Pine, dependencies,
examples and guide. See `GoldESScaling/README.md` in the ZIP or
`../gold_es_scaling/README.md` in this workspace. TradingView/MT5 runtime parity is
pending. The key-level/Fibonacci indicator conversion still awaits its source.

## Institutional S1-S7 additions

The following seven **Python-to-MT5 bridge readers** are included as `.mq5` and `.ex5`:

| ID | File | Strategy |
| --- | --- | --- |
| S1 | ITS_S1_Macro_Trend_Continuation | Macro-aligned trend continuation |
| S2 | ITS_S2_Liquidity_Sweep_Reversal | Liquidity sweep reversal |
| S3 | ITS_S3_Equity_Value_Repricing | Equity value repricing |
| S4 | ITS_S4_Volatility_Compression_Breakout | Volatility compression breakout |
| S5 | ITS_S5_Positioning_Exhaustion | Positioning exhaustion |
| S6 | ITS_S6_Statistical_Mean_Reversion | Statistical mean reversion |
| S7 | ITS_S7_Options_Volatility_Screen | Options volatility research screen |

**Python must export a feed before these seven scripts run in MT5.** See
`InstitutionalBridge/README.md` in the combined ZIP and the source-project
`Development/src/institutional_trading_system/mt5/README.md`. The nested
`InstitutionalBridge/ITS_S1_S7_Bridge.zip` contains their pinned Python source snapshot,
separate exporters, config files, dependencies and installation guide.
S3 retains equity eligibility, S5 requires COT, and S7 needs an option chain and
remains a research screen. These are not chart-only strategy rewrites or automatic
trading EAs. No broker position management or risk approval is added.

## Original daily and futures scanner files

| File in Scripts/ | Individual strategy |
| --- | --- |
| Daily_FX_Major_A.mq5 | USD major: C1 + C2 + golden C3 stacked + C4/C5/C6 |
| Daily_FX_Major_B.mq5 | USD major: C1 + (C2 or golden C3) + C4/C5/C6; excludes A |
| Daily_FX_Cross_A.mq5 | Non-USD FX cross: A rules, with closed C4 required |
| Daily_Gold_A.mq5 | Gold: Weekly/Daily C1 + C2 + golden C3 stacked + closed C4 |
| Daily_Gold_B.mq5 | Gold: Weekly/Daily C1 + C2 + C3; excludes A |
| Daily_SP500_A.mq5 | S&P: Weekly/Daily C1, A rules only |
| Daily_Oil_A.mq5 | Oil: Weekly/Daily C1, A rules only |
| Futures_Scalp_MOMO_Base.mq5 | M5 base, breakout the selected product's entry offset beyond it |
| Futures_Scalp_Trend.mq5 | M5 rising/falling 9/15 EMA; entries at each EMA |
| Futures_Scalp_RMA.mq5 | M5 extension and return to EMA30/65/200, pivot or VWAP clusters |
| Futures_Scalp_FFMA.mq5 | M5 RSI extreme, first fresh M1 turn toward the 9 EMA |
| Futures_Scalp_Double_Bottom_Top.mq5 | M5 first pivot, qualifying move/bounce, second test |
| Futures_Scalp_ORB_MOMO.mq5 | First break of the New York RTH opening range |
| Futures_Scalp_ORB_Trend.mq5 | First return to the ORB edge after a qualifying extension |
| Futures_Scalp_One_Minute_Range_Break.mq5 | M5/M1 trend, flat M1 coil, stop-limit reference |

A/B are mutually exclusive qualified outputs, matching the source's grade priority.
Long and short directions belong to the same strategy. Double Bottom/Top is one
symmetric method. ORB MOMO and ORB Trend are separate files even though the Python
scanner uses one function for both. The five methods in `scalping_system/` are a
separate adaptive research package and are outside this daily/futures-scanner conversion.

## Install and run

1. In MT5 select **File > Open Data Folder**.
2. Copy the desired `.mq5` and/or compiled `.ex5` files from `Scripts/` into
   `MQL5/Scripts/StrategyExercise/` inside that data folder.
3. If compiling from source, open each `.mq5` in MetaEditor and press **F7**.
4. Refresh **Navigator > Scripts**, open the instrument's chart, and drag its
   individual script onto that chart.
5. Set `InpSymbol` to the broker's exact symbol, or leave it blank for the chart.
   Daily gold files must run on a gold instrument; S&P and oil files must run on their
   respective instruments. FX files validate their canonical pair and major/cross
   class; `InpFXPair` handles unusual broker prefixes/suffixes.
6. For each `Futures_Scalp_*` file, select **InpProduct**: ES/MES, NQ/MNQ,
   CL/MCL or GC/MGC. The script refuses to scan until this is selected. Use
   **ES/MES for S&P futures**, regardless of a broker's contract-specific symbol name.
7. Read signals in **Toolbox > Experts** and the per-strategy CSV in
   `MQL5/Files/`. The CSV filename includes both strategy and symbol. Each run
   overwrites that file, including a header-only report when no setup qualifies.

These are one-shot **Scripts** using `OnStart`. Run again to rescan. They are not
Expert Advisors, continuous monitors or Strategy Tester trading robots. They scan
one broker symbol per run; the daily universe can be covered by running the same
individual method on each appropriate chart. Nothing sends, modifies or closes
orders. Turning on Algo Trading is unnecessary for these signal scans.

## Inputs and calculation details

- Futures scalp defaults come from the selected **product profile**, including
  rule tick, entry offset, targets, extension, EMA gap, double-test move, coil depth
  and ORB stop. Zero-valued distance/target inputs use that profile; a positive input
  overrides it, and a negative second-target input disables that optional target.

  | Profile | Source price tick | MOMO entry ticks | Trend max gap ticks | RMA/FFMA extension ticks | DB/DT move ticks |
  | --- | ---: | ---: | ---: | ---: | ---: |
  | ES / MES | 0.25 | 2 | 10 | 40 | 40 |
  | NQ / MNQ | 0.25 | 4 | 40 | 160 | 120 |
  | CL / MCL | 0.01 | 2 | 10 | 25 | 20 |
  | GC / MGC | 0.10 | 2 | 10 | 20 | 30 |

  These are the existing scanner's settings, not newly verified exchange specs or
  performance recommendations. Micro products use their parent product's **price
  rules**; entries/targets round to the selected broker symbol's executable tick
  and risk uses **one broker lot in account currency**. No full-size contract dollar
  value is reused for a micro or CFD. ES ORB Trend retains its special two-ATR
  extension requirement. `futures_profiles.json` records the complete source profile.
  `OrderCalcProfit` estimates profit/loss and never places an order. Commission,
  slippage and swap are absent. CFD feeds can produce different signals from futures.
- The default clock model is the source's Forex.com **New York + 7 hours**, including
  US daylight saving. On another broker, set `InpNYCloseServer=false`; auto mode
  estimates the current server/UTC offset. To cover history spanning offset changes,
  use the NY-close model only if it actually matches the broker. A fixed override
  `InpServerUTCOffsetMinutes` takes precedence. Auto current-offset mode cannot
  reconstruct an unknown broker's historical DST rules. ORB uses 09:30-16:00 New York;
  session pivot/VWAP reset at 18:00 New York. DST conversion assumes modern US rules.
- Futures scalp loads 1,200 closed M5 bars and 600 closed M1 bars where needed. Daily
  methods load 1,300 closed D1 bars, then keep the last **400** eligible bars for
  path-dependent Daily scoring. H2/H4 are rebuilt from 5,000 closed H1 bars on
  **00:00 UTC** boundaries, matching the scanner's default `resample_1h` mode.
  Insufficient history fails the scan; open the required timeframes, load history,
  then rerun. Stale data is explicitly logged.
- `InpFreezeDailyAtEvening=true` matches an intraday/pre-NY scan: Daily bars must
  have completed by the latest 00:05 UTC. Set false for an evening scan with current
  completed Daily bars. Weekly bars are reconstructed from the Daily scoring window
  exactly as in the source, including its Friday-presence rule.
- EMA is seeded with an initial arithmetic mean; ATR and RSI use Wilder smoothing.
  Pivots require confirmation bars. Forming bars are excluded.
- Daily defaults preserve source grids: FX +/-15 pips, gold every $100 +/-$10 and
  every $50 +/-$8, S&P +/-20 points, oil +/-$1.00. Gold chart-pattern scale remains
  $20. Daily C3 tolerance uses `max(2 source feature ticks, 0.10 recognition H4 ATR)`.
  Set `InpFeatureTick=0.10` for daily GC futures or `0.25` for daily ES futures;
  zero uses source CFD defaults. Grid inputs can override broker-specific units.
- C4 includes the source candle patterns, reversal + marubozu, double tops/bottoms,
  and head-and-shoulders with closed neckline confirmation. D1 takes precedence over
  H4, then H2. C6 uses Daily structure for gold/S&P/oil and H4 structure for FX.
- Daily levels are **reference plans**, not trade instructions: the report contains
  the 38.2/50/61.8 ladder, 78.6/89 stops and -27/-61.8 extensions. A qualified FX B
  can have no valid impulse; its stop/target/risk fields remain blank until the owner
  supplies a plan. The source defines no automatic levels in that situation.
- Trend displays two entries. Its source rulebook describes the first 9 EMA leg as
  having no stop and trailing. The displayed stop is based on the 15 EMA; the numeric risk measures the first
  entry against that reference stop and is hypothetical for a no-stop first leg.
  The CSV note labels this exception. These scripts execute neither.
- VWAP uses real volume where available and labels a tick-volume fallback as a proxy.
  A spot CFD's feed/volume is different from GC futures, so its signals can differ.

## Scope and validation

This is a **technical signal conversion**. COT/news ranking, red-folder event risk,
DXY/Fed/safe-haven context, account journal, portfolio allocation, trade-gate owner
confirmation, continuous-contract roll flags and personality/dynamics reports are
not reproduced. The source's dynamics layer is diagnostic only and did not change
C1-C6 qualification. MT5 scripts use broker bars; the original non-FX scanner uses
TradingView. Equal formulas do not guarantee equal signals across those feeds.

The corrected futures-profile/scalp unit checks passed 28 tests (one existing demo
integration test deselected). They compare generated product settings with the
original scanner configuration; they do not execute MQL5.

The delivery validation records MetaEditor compilation for every final file and
checks that each output has one entry point, no strategy router, no external include,
and no order mutation API. Compile logs and hashes are in `validation/`.
Compilation is not a historical performance test or a Python-versus-MQL runtime
parity test. Broker runtime/paper validation and performance validation are pending.
No script has been installed into the terminal or run against a trading account.

## Maintain the source

Edit `templates/common.mql`, `templates/daily.mql`, or the relevant individual
`templates/scalp/*.mql`, then run `python build_scripts.py`. Generation duplicates
math/data helpers to make each resulting file completely standalone; it does not
combine entry methods. Recompile all files after regeneration, then run
`python validate_delivery.py` to generate a fresh validation report and ZIP.

MQL5 lifecycle reference: [OnStart](https://www.mql5.com/en/docs/event_handlers/onstart).
History reference: [CopyRates](https://www.mql5.com/en/docs/series/copyrates).

## Correction to the first delivery

The former `Gold_Scalp_*` files used GC-only defaults. They were **futures scalp
methods**, not the owner's separate gold scaling indicator. The corrected files
are named `Futures_Scalp_*` and require explicit product selection. The old files
are preserved locally in `legacy_gold_only/` and excluded from the corrected ZIP.
Remove the old `Gold_Scalp_*` copies from your MT5 Scripts folder when installing
these replacements so there is no confusion. The daily `Daily_Gold_A/B` names are
correct and remain separate daily position methods.

The original Python `futures_scalp_scanner.py` already supports ES, NQ, CL and GC:
`python futures_scalp_scanner.py --symbols ES --setups momo` selects S&P MOMO alone.
The owner supplied the Gold / ES scaling Pine v2.8.0; its independent Python/MT5
bridge is now included. The separate key-level/Fibonacci conversion remains
**pending its indicator source**. Existing
C2/C3 daily confluence calculations do not establish that those missing indicators'
full trading rules have been converted. No replacement rules have been invented.
