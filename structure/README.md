# Market structure reference (Python · MQL5 · Pine)

Three versions of the same market structure rules, kept side by side so any application (a script, an MT5
Expert Advisor, a TradingView chart) can use them and get the same answer. **If a rule changes, change all
three files and this page together.**

| Version | File | Use it for |
| --- | --- | --- |
| Python | `python/market_structure.py` | Scripts, backtests, research. Needs only numpy. |
| MQL5 | `mql5/MarketStructure.mqh` (logic) + `mql5/MarketStructure.mq5` (chart indicator) | MT5 charts; `#include` the `.mqh` in any EA or indicator. Read only: places no orders. |
| Pine | `pine/market_structure.pine` | TradingView chart (standalone indicator). The same logic also runs inside `tradingview/kl_fib_confluence_setup.pine`. |

## The rules (owner, approved October 15, 2026)

| Rule | Definition | Default |
| --- | --- | --- |
| **Swing point** | A bar whose high has `width` bars on each side with lower highs (a low mirrors it). It counts only once price has moved at least `react` × ATR away from it. While a high waits for that reaction, a higher pivot high replaces it (lows mirror). | `width` 3, `react` 1.5, ATR 14 |
| **Labels** | New swing high: **HH** if above the previous swing high, else **LH**. New swing low: **HL** if above the previous swing low, else **LL**. The first ones are **H** / **L**. | |
| **Trend** | **Up:** the last 2 swing highs and the last 2 swing lows both rise, and the close is not below the last swing low. **Down:** both fall and the close is not above the last swing high. Anything else is **ranging**. Closes, not wicks, confirm. | |
| **Weekly** | Context. When it agrees with the Daily it adds one confluence. It never blocks a trade. | |
| **Daily** | Sets the direction. Ranging means no trade. | |
| **4H health** | A 4H pullback against the Daily is the setup forming. A 4H **close** beyond the last Daily higher low (uptrend) / lower high (downtrend) means the Daily trend is challenged. A Daily close there ends it. | |
| **Daily POI** | The last Daily higher low (uptrend) / lower high (downtrend) ± the key box half-width, widened to take in the last 3 Daily swing highs / lows and key level boxes within `poiAtr` × Daily ATR. | `poiAtr` 0.5 |
| **Key levels** | Box half-width and grid step used by the POI. | FX 10 pips / 250 pips (JPY 0.10 / 2.50), gold $10 / $50, S&P 10 / 50 points |

ATR is Wilder's: true range (high − low on the first bar), smoothed by an RMA seeded with the simple average of
the first 14 true ranges. This matches TradingView `ta.atr` and MT5 `iATR`.

All three run bar by bar, in the same order on each bar: update the waiting high, update the waiting low,
confirm the high, confirm the low, then read the trend. Higher timeframes use completed bars only.

## Where each rule lives

| Rule | Python | MQL5 | Pine |
| --- | --- | --- | --- |
| ATR | `atr()` | `MsAtr()` | `ta.atr(14)` |
| Swings, labels, trend | `analyze()` → `Structure` | `CMarketStructure::Run()` | `f_swings()` |
| Load a timeframe | your data loader (oldest bar first) | `MsLoad(symbol, tf, bars, ms)` | `request.security(..., lookahead_on)` with `[1]` |
| Daily POI | `poi()` | `MsPOI()` | POI block |
| 4H health | `health()` | `MsHealth()` | `challenged` |
| Full top-down read | `read_market()` | indicator `OnCalculate` | table |

## Known differences

- **Ties.** Python and MQL5 need a pivot strictly above (below) every neighbour. Pine uses `ta.pivothigh` /
  `ta.pivotlow`, which TradingView may treat differently when two highs are exactly equal. That's rare on real prices.
- **The scanner (`scanner.py`).** It applies the same rules with its own batch version (`find_swings`,
  `daily_bias`, cfg-0.13.0). Its ATR is seeded one bar later, it measures the reaction against the ATR at the
  swing bar, and its Daily bias remembers its invalidation level between bars. Swings and trend can therefore
  occasionally differ by a bar or so from these three files. Switching the scanner over to
  `python/market_structure.py` would remove that difference; it hasn't been done yet.

## Using it

**Python**
```python
import sys; sys.path.insert(0, "structure/python")
from market_structure import analyze, poi, read_market
daily = analyze(d_high, d_low, d_close)          # completed Daily bars, oldest first
print(daily.last_trend, [s.label for s in daily.swings[-4:]])
```

**MQL5 (EA or indicator)**
```cpp
#include "MarketStructure.mqh"
CMarketStructure d;  d.Init(3, 1.5, 14);
if(MsLoad(_Symbol, PERIOD_D1, 1500, d) && d.Trend() == MS_UP) { /* longs only */ }
```
To install the chart indicator, copy both `mql5` files into `MQL5/Indicators/MarketStructure/`, compile
`MarketStructure.mq5` in MetaEditor (F7), and drag it onto a chart. It draws the labels and the POI box, and
writes the Weekly / Daily / 4H read in the chart's top-left corner.

**Pine:** paste `pine/market_structure.pine` into the Pine Editor, Save, then Add to chart.

The MQL5 and Pine files have not been compiled here. Compile them in MetaEditor and TradingView, and send
back any error text with its line number.
