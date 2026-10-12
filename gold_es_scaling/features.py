"""Pine v2.8.0 numeric conventions and causal, confirmed-swing features."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def pine_round(value: float, step: float = 1.0) -> float:
    return math.floor(value / step + 0.5) * step


def ema(close, period):
    out = np.empty(len(close), dtype=float)
    if not len(close):
        return out
    out[0] = close[0]
    alpha = 2 / (period + 1)
    for i in range(1, len(close)):
        out[i] = alpha * close[i] + (1 - alpha) * out[i - 1]
    return out


def atr(frame, period=14):
    h, lows, c = (frame[key].to_numpy(float) for key in ("high", "low", "close"))
    out = np.full(len(c), np.nan)
    if not len(c):
        return out
    tr = np.r_[
        h[0] - lows[0],
        np.maximum.reduce(
            [h[1:] - lows[1:], abs(h[1:] - c[:-1]), abs(lows[1:] - c[:-1])]
        ),
    ]
    if len(c) >= period:
        out[period - 1] = tr[:period].mean()
        for i in range(period, len(c)):
            out[i] = ((period - 1) * out[i - 1] + tr[i]) / period
    return out


def structures(frame: pd.DataFrame, width: int):
    h, lows, c = (frame[key].to_numpy(float) for key in ("high", "low", "close"))
    rows = []
    last_h = prev_h = last_l = prev_l = math.nan
    trend = 0
    prior_close = prior_h = prior_l = math.nan
    for t in range(len(frame)):
        q = t - width
        if q >= width:
            # Rightmost equal pivot convention; pending pivots are never exposed.
            if h[q] >= max(h[q - width : q]) and h[q] > max(h[q + 1 : t + 1]):
                prev_h, last_h = last_h, h[q]
            if lows[q] <= min(lows[q - width : q]) and lows[q] < min(
                lows[q + 1 : t + 1]
            ):
                prev_l, last_l = last_l, lows[q]
        bull = c[t] > last_h and prior_close <= prior_h
        bear = c[t] < last_l and prior_close >= prior_l
        if trend != 1 and c[t] > last_h and (trend == 0 or last_l > prev_l):
            trend = 1
        elif trend != -1 and c[t] < last_l and (trend == 0 or last_h < prev_h):
            trend = -1
        rows.append(
            dict(
                trend=trend,
                high=last_h,
                low=last_l,
                prev_high=prev_h,
                prev_low=prev_l,
                bull=bool(bull),
                bear=bool(bear),
            )
        )
        prior_close, prior_h, prior_l = c[t], last_h, last_l
    return rows


def patterns(frame: pd.DataFrame):
    o, h, lows, c = (
        frame[key].to_numpy(float) for key in ("open", "high", "low", "close")
    )
    b = abs(c - o)
    avg = pd.Series(b).rolling(14).mean().to_numpy()
    a = atr(frame)
    result = []
    for t in range(len(frame)):
        rg, uw, lw = h[t] - lows[t], h[t] - max(o[t], c[t]), min(o[t], c[t]) - lows[t]
        bull = bear = ""
        if (
            t >= 1
            and c[t] > o[t]
            and c[t - 1] < o[t - 1]
            and c[t] >= o[t - 1]
            and o[t] <= c[t - 1]
            and b[t] > b[t - 1]
        ):
            bull = "Bullish engulfing"
        elif (
            t >= 2
            and c[t - 2] < o[t - 2]
            and b[t - 2] >= avg[t]
            and b[t - 1] <= 0.3 * b[t - 2]
            and c[t] > o[t]
            and c[t] > (o[t - 2] + c[t - 2]) / 2
        ):
            bull = "Morning star"
        elif (
            t >= 2
            and all(c[i] > o[i] for i in (t, t - 1, t - 2))
            and c[t] > c[t - 1] > c[t - 2]
            and o[t] >= o[t - 1]
            and o[t] <= c[t - 1]
            and o[t - 1] >= o[t - 2]
            and o[t - 1] <= c[t - 2]
            and all(b[i] >= 0.5 * avg[t] for i in (t, t - 1, t - 2))
        ):
            bull = "Three white soldiers"
        elif (
            t >= 1
            and c[t - 1] < o[t - 1]
            and b[t - 1] >= 0.8 * avg[t]
            and c[t] > o[t]
            and o[t] <= c[t - 1]
            and c[t] > (o[t - 1] + c[t - 1]) / 2
            and c[t] < o[t - 1]
        ):
            bull = "Piercing line"
        elif (
            t >= 1
            and c[t - 1] < o[t - 1]
            and b[t - 1] >= avg[t]
            and c[t] > o[t]
            and o[t] >= c[t - 1]
            and c[t] <= o[t - 1]
        ):
            bull = "Bullish harami"
        elif (
            t >= 1
            and c[t - 1] < o[t - 1]
            and c[t] > o[t]
            and abs(lows[t] - lows[t - 1]) <= 0.1 * a[t]
        ):
            bull = "Tweezer bottom"
        elif (
            t >= 2
            and h[t - 1] <= h[t - 2]
            and lows[t - 1] >= lows[t - 2]
            and c[t] > h[t - 2]
        ):
            bull = "Inside bar break (up)"
        elif rg > 0 and lw >= 2 * b[t] and lw >= 0.6 * rg and uw <= 0.25 * rg:
            bull = "Hammer / bullish pin bar"
        elif c[t] > o[t] and rg > 0 and b[t] >= 0.85 * rg and rg >= 0.8 * a[t]:
            bull = "Bullish marubozu"
        if (
            t >= 1
            and c[t] < o[t]
            and c[t - 1] > o[t - 1]
            and c[t] <= o[t - 1]
            and o[t] >= c[t - 1]
            and b[t] > b[t - 1]
        ):
            bear = "Bearish engulfing"
        elif (
            t >= 2
            and c[t - 2] > o[t - 2]
            and b[t - 2] >= avg[t]
            and b[t - 1] <= 0.3 * b[t - 2]
            and c[t] < o[t]
            and c[t] < (o[t - 2] + c[t - 2]) / 2
        ):
            bear = "Evening star"
        elif (
            t >= 2
            and all(c[i] < o[i] for i in (t, t - 1, t - 2))
            and c[t] < c[t - 1] < c[t - 2]
            and o[t] <= o[t - 1]
            and o[t] >= c[t - 1]
            and o[t - 1] <= o[t - 2]
            and o[t - 1] >= c[t - 2]
            and all(b[i] >= 0.5 * avg[t] for i in (t, t - 1, t - 2))
        ):
            bear = "Three black crows"
        elif (
            t >= 1
            and c[t - 1] > o[t - 1]
            and b[t - 1] >= 0.8 * avg[t]
            and c[t] < o[t]
            and o[t] >= c[t - 1]
            and c[t] < (o[t - 1] + c[t - 1]) / 2
            and c[t] > o[t - 1]
        ):
            bear = "Dark cloud cover"
        elif (
            t >= 1
            and c[t - 1] > o[t - 1]
            and b[t - 1] >= avg[t]
            and c[t] < o[t]
            and o[t] <= c[t - 1]
            and c[t] >= o[t - 1]
        ):
            bear = "Bearish harami"
        elif (
            t >= 1
            and c[t - 1] > o[t - 1]
            and c[t] < o[t]
            and abs(h[t] - h[t - 1]) <= 0.1 * a[t]
        ):
            bear = "Tweezer top"
        elif (
            t >= 2
            and h[t - 1] <= h[t - 2]
            and lows[t - 1] >= lows[t - 2]
            and c[t] < lows[t - 2]
        ):
            bear = "Inside bar break (down)"
        elif rg > 0 and uw >= 2 * b[t] and uw >= 0.6 * rg and lw <= 0.25 * rg:
            bear = "Shooting star / bearish pin bar"
        elif c[t] < o[t] and rg > 0 and b[t] >= 0.85 * rg and rg >= 0.8 * a[t]:
            bear = "Bearish marubozu"
        result.append((bull, bear))
    return result, a
