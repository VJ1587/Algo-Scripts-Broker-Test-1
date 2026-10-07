# Gold / ES Scaling Scanner (TradingView Pine Script)

`scaling_scanner.pine` (v2.1.0) is a TradingView indicator (Pine Script v6) that sends trade alerts
when a trend-continuation setup forms on XAUUSD or ES / SPX500. It places no orders.

## Install

1. TradingView → Pine Editor → New → paste `scaling_scanner.pine` → **Add to chart**.
2. Use **one 15m chart per instrument**: XAUUSD 15m, plus ES1! (or SPX500) 15m. One copy of the
   script scans both Setup A (2H, EMA14) and Setup B (15m, EMA50), so each instrument numbers its
   own trades.
3. Settings → **Risk and scaling** → set **Starting equity** and **Track P&L from** (today).
4. Create the alert: Alerts → Create → Condition = **ScaleScan** → **Any alert() function call** →
   choose delivery (app push, email, webhook). That's one alert per instrument.
5. To get only the public signal format, turn off **Add private lines to entry alerts**.

## Alert format

```
1️⃣st Trade of The Week

🗓️ Wednesday, October 7th @ 3:36pm

💰 XAU/USD Market Execution Sell

🔵 Entry: 4110 to 4100

🛑 Stop Loss: 4125 (200 pips)

✅ Take Profit 1: 4010
...
✅ Take Profit 5: 3610

(🔹 Price is at 4105 at the time of this signal)

💎 If the market goes ABOVE 4105 AFTER you see this signal, feel free to enter a Market Execution Sell ...

📝 Take Profit 1 = Most Likely To Win
📝 Take Profit 5 = Least Likely To Win AT THE MOMENT
📝 Take Profits are partials, not mandatory.
🚨 Multiple Take Profits DO NOT MEAN enter multiple trades at the same time.

— Private —
📊 Setup B (15m EMA50 rubber band) PULLBACK: Bearish engulfing | 4100 major zone | at EMA50 | ✨ ...
📏 Size: 0.05 lots (risk 1% = $100 of $10,000)
🧭 Daily DOWN / Weekly DOWN | Setup B structure DOWN
```

- **Numbering** restarts every Sunday, New York time. Times are New York time.
- **Trade type:** Setup A (2H) signals say "Swing Trade / Position Trade - …". Setup B (15m) signals
  say just "Market Execution Buy/Sell".
- **Follow-up alerts** carry each trade's own number: Buy/Sell Limit filled, move stop to breakeven, Take Profit N
  hit, stopped at breakeven / stop loss (with a re-entry note if conditions are still valid), and
  structure broken (get out).

## Rules as coded (owner Q&A, October 7, 2026)

| Rule | How the script checks it |
| --- | --- |
| HTF direction | Daily swing structure; Weekly only when the Daily has no trend. When they disagree, the Daily wins. Read from the last **closed** D/W bar, so it does not repaint. |
| Sellers / buyers in control | On each setup's timeframe, structure flips bullish only after a higher low **and then** a close above the last swing high (mirror for bearish). No buys while that structure is bearish. |
| EMA side | Buys only on a close above the EMA (14 on the 2H, 50 on the 15m); sells only below it. |
| Impulse + correction | Impulse = a close that breaks the last swing high (buys) / low (sells). Correction = a pullback of at least 1 × ATR from the leg extreme that holds the last swing low / high. |
| Confluences | The signal candle must be **inside a key/mid level zone** (always required), with a candle pattern in the bias direction and price at or near the EMA (within 0.5 × ATR by default). So the EMA only counts at a level. |
| Bonus (not required) | **Stretched + snap-back:** price got at least 2 × ATR away from the EMA since it last touched it. **Impulse → Correction → Continuation into [level]:** the signal candle closes beyond the prior candle's high (buys) / low (sells), heading into the next level. |
| Candle patterns | Engulfing, morning/evening star, three soldiers/crows, piercing/dark cloud, harami, tweezer, inside-bar break, hammer/pin bar/shooting star, marubozu. |
| Levels | Gold: majors every $100 with a ±$15 zone, mids every $50 with ±$10. ES: majors every 100 pts with ±10, mids every 50 with ±7. |
| Order type | **PULLBACK** = Market Execution, entry range = price ±$5 (±3 pts ES). **BREAKOUT** (close through a level zone) = Buy/Sell Limit at the near edge of the broken zone, cancelled after 30 setup bars unfilled. **RETEST** (return to the broken zone with a pattern) = Market Execution. |
| Stop loss | $1 / 1 pt beyond the last swing low (buys) / high (sells) on the setup timeframe, **max 500 pips** ($50) gold / 50 pts ES. |
| Take profits | TP1–TP5 = the next 5 **major** levels in the trade direction, front-run by $10 gold / 5 pts ES (a buy toward 4400 gets TP 4390). TPs closer than 0.5 × the stop distance are skipped. |
| Breakeven | Alert once the trade is +$2 (gold) / +2 pts (ES) in profit. |
| Exit | Alert when a setup-timeframe candle closes through the last swing low (longs) / high (shorts). |
| Re-entry | After a stop, if bias and structure still agree, the alert says so; the next signal is tagged RE-ENTRY. |
| Multiple trades / scaling | No daily limit. **Setup B alerts on its own at every 15m close; it never waits for the 2H.** Setup A checks every 2H close. Every entry is a new numbered trade, tracked separately (its own breakeven, TP and exit alerts), up to 5 open per setup. A new entry can fire on every setup candle (cooldown 1 bar). When trades are already open, the private lines list them ("Scaling in. Already open: #3 LONG (BE), …"). |
| Scaling | Risk % by equity growth: 1% base, 1.5% at +25%, 2% at +50%, 3% at +100%. It drops back a tier if equity falls. Size = equity × risk % ÷ (stop distance × $ per point per lot), rounded down. |
| Alert timing | On candle close only, in all sessions. A 2H signal fires on the 15m candle that closes the 2H candle. |

## Things to verify before relying on it

- **Not yet compiled in TradingView.** The script was syntax-checked with an offline Pine parser (v5
  grammar), not with TradingView's v6 compiler. If the editor shows an error, send it over.
- **2H timing.** Setup A is evaluated on the 15m candle that closes each 2H candle. I believe
  TradingView returns the finished 2H values on that candle, both live and in history, but this
  needs checking. If the chart shows no historical "A" labels at all, tell me: it means the 2H
  values arrive one 15m candle later, and the timing needs adjusting.
- **Contract values.** Gold is set to $100 per $1 move per 1.00 lot (100 oz) and ES to $50 per point
  (MES $5). I believe these are the standard specs, but check your broker's contract
  specification. An SPX500 CFD is usually not $50 per point.
- **Equity is not live.** Pine cannot read the broker account. Tracked equity assumes the full
  position closes at the stop or at the structure exit (TP partials aren't modelled). For accurate
  sizing, type your real equity into **Current equity override**.
- **Breakeven at +$2** sits inside normal gold noise, so expect many breakeven stop-outs. The
  re-entry tag is meant to cover that.
- **Forward-test first** on demo or paper, and compare a few weeks of alerts with your own read of
  the chart before you size up.
