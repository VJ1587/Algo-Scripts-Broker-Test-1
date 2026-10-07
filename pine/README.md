# Gold / ES Scaling Scanner (TradingView Pine Script)

`scaling_scanner.pine` is a TradingView indicator (Pine Script v6) that sends alerts when a
trend-continuation setup forms on XAUUSD or ES / SPX500. It places no orders. Its purpose is to help
scale the account while these instruments are trending.

## Install

1. TradingView → Pine Editor → New → paste `scaling_scanner.pine` → **Add to chart**.
2. Open the chart(s) you want to scan:
   - **Setup A:** XAUUSD (or ES1! / SPX500) on the **2H** chart (EMA14).
   - **Setup B:** the same symbol on the **15m** chart (EMA50 rubber band).
   With Setup on Auto, the script picks A on 1H and above and B below 1H.
3. Settings → **Risk and scaling** → set **Starting equity** and **Track P&L from** (today).
4. Create the alert: Alerts → Create → Condition = **ScaleScan** → **Any alert() function call** →
   pick delivery (app push, email, webhook). Create one alert per chart (for example 4 alerts for
   gold 2H, gold 15m, ES 2H and ES 15m). Alerts depend on your TradingView plan's alert limits.

## Rules as coded (owner Q&A, October 7, 2026)

| Rule | How the script checks it |
| --- | --- |
| HTF direction (1) | Daily swing structure; Weekly only when the Daily has no trend. When they disagree, the Daily wins. Read from the last **closed** D/W bar, so it does not repaint. |
| Sellers / buyers in control (6) | Structure flips bullish only after a higher low **and then** a close above the last swing high (mirror for bearish). No buys while chart structure is bearish, and no sells while it is bullish. |
| EMA side (4) | Buys only on a close above the EMA (14 on the 2H, 50 on the 15m); sells only below it. |
| Impulse + correction (3) | Impulse = a close that breaks the last swing high (buys) / low (sells). Correction = price pulls back at least 1 × ATR from the leg extreme and holds the last swing low / high. |
| 3 confluences (5) | All three on the signal candle: (a) a candle pattern in the bias direction, (b) inside a key/mid level zone, (c) at or near the EMA (within 0.5 × ATR by default). |
| Candle patterns | Engulfing, morning/evening star, three soldiers/crows, piercing/dark cloud, harami, tweezer, inside-bar break, hammer/pin bar/shooting star, marubozu. |
| Levels (8, 9) | Gold: majors every $100 with a ±$15 zone, mids every $50 with ±$10. ES: majors every 100 pts with ±10, mids every 50 with ±7. |
| Breakouts (7) | **BREAKOUT** alert when a candle closes through a level zone in the bias direction. **RETEST** alert when price later returns to that zone and prints a pattern, within 30 bars. A close back through the zone cancels it. |
| Stop | Gold $50 (500 pips at $0.10 per pip). ES 50 points. |
| Breakeven (10) | **MOVE STOP TO BREAKEVEN** alert once the trade is +$2 (gold) / +2 pts (ES) in profit. |
| Exit (10) | **EXIT: STRUCTURE BROKEN** alert when a candle closes through the last swing low (longs) / high (shorts). |
| Re-entry (10) | After a stop, if bias and structure still agree, the stop alert says so; the next signal is tagged **RE-ENTRY**. |
| Multiple trades (11) | No daily limit. One tracked trade per chart. Same-direction signals while it is open are sent as optional add-ons, at most one every 3 bars. |
| Scaling (12) | Risk % by equity growth: 1 % base, 1.5 % at +25 %, 2 % at +50 %, 3 % at +100 %. It drops back a tier if equity falls. Lots = equity × risk % ÷ (stop × $ per point per lot), rounded down to the lot step. |
| Alert timing | On candle close only, in all sessions. |

## Things to verify before relying on it

- **Contract values.** Gold is set to $100 per $1 move per 1.00 lot (100 oz) and ES to $50 per point
  (MES $5). I believe these are the standard specs, but check your broker's contract
  specification. An SPX500 CFD is usually *not* $50 per point.
- **Equity is not live.** Pine cannot read the broker account. Equity comes from the P&L of the
  tracked trades since **Track P&L from**, which assumes you took every alert at the candle close.
  For accurate sizing, type your real equity into **Current equity override** whenever it changes.
- **Breakeven at +$2** sits inside normal gold noise, so expect many breakeven stop-outs; the
  re-entry alerts are meant to cover that.
- **Untested.** The script has been syntax-checked but not yet run on live charts or forward
  tested. Run it on a demo or paper account first, and review a few weeks of alerts against the
  chart before you size up.
- The defaults the Q&A left open are all inputs: swing length (5), ATR pullback (1.0), near-EMA
  tolerance (0.5 ATR), retest window (30 bars), add-on cooldown (3 bars) and the mid-zone buffer
  for ES (±7).
