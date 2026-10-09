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
