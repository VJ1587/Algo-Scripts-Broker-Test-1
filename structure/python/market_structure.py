"""Market structure reference implementation (Python).

One of three matching implementations of the owner's market structure rules (October 2026):
    structure/python/market_structure.py   (this file)
    structure/mql5/MarketStructure.mqh     (MetaTrader 5 include) + MarketStructure.mq5 (chart indicator)
    structure/pine/market_structure.pine   (TradingView indicator)
The rules and defaults are written out in structure/README.md. Change all three together.

Rules
    Swing      A pivot high has `width` bars on each side with lower highs (a pivot low mirrors it). It is a
               candidate until price moves at least `react_atr` x ATR away from it; only then is it a swing.
               While a high waits for its reaction, a higher pivot high replaces it (lows mirror this).
    Labels     A new swing high is HH if above the previous swing high, else LH. A new swing low is HL if
               above the previous swing low, else LL. The first of each is H / L.
    Trend      Up when the last 2 swing highs AND the last 2 swing lows both rise and the close has not
               broken the last swing low. Down mirrors it. Anything else is ranging (0).
    POI        Last higher low (uptrend) / lower high (downtrend) +/- the key box half-width, widened to take
               in earlier swings and key level boxes within `poi_atr` x ATR of it.
    Health     A lower-timeframe (4H) close beyond the last Daily higher low (uptrend) / lower high
               (downtrend) means the Daily trend is challenged. A pullback that stays inside is healthy.

Only numpy is needed. Arrays run oldest bar first. Feed completed bars only (drop the bar still forming).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

UP, DOWN, RANGE = 1, -1, 0


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14) -> np.ndarray:
    """Wilder ATR, matching TradingView ta.atr and MT5 iATR: true range (high - low on the first bar),
    smoothed by an RMA seeded with the simple mean of the first n true ranges. NaN before bar n - 1."""
    h, l, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    tr = h - l
    if len(c) > 1:
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    out = np.full(len(c), np.nan)
    if len(c) < n:
        return out
    out[n - 1] = tr[:n].mean()
    for t in range(n, len(c)):
        out[t] = (out[t - 1] * (n - 1) + tr[t]) / n
    return out


@dataclass
class Swing:
    kind: str          # "H" or "L"
    index: int         # bar of the swing high / low
    confirm: int       # bar at which the reaction rule was met (the swing becomes usable here)
    price: float
    label: str         # H, HH, LH / L, HL, LL


@dataclass
class Structure:
    swings: list[Swing] = field(default_factory=list)          # in confirmation order
    trend: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))   # per bar: 1 up, -1 down, 0 range

    def highs(self, upto: Optional[int] = None) -> list[Swing]:
        return [s for s in self.swings if s.kind == "H" and (upto is None or s.confirm <= upto)]

    def lows(self, upto: Optional[int] = None) -> list[Swing]:
        return [s for s in self.swings if s.kind == "L" and (upto is None or s.confirm <= upto)]

    @property
    def last_trend(self) -> int:
        return int(self.trend[-1]) if len(self.trend) else RANGE

    @property
    def last_higher_low(self) -> Optional[float]:
        lows = self.lows()
        return lows[-1].price if lows else None

    @property
    def last_lower_high(self) -> Optional[float]:
        highs = self.highs()
        return highs[-1].price if highs else None


def _is_pivot(x: np.ndarray, k: int, width: int, high: bool) -> bool:
    """Strict pivot: every bar within `width` on both sides is lower (high) / higher (low)."""
    if k - width < 0 or k + width >= len(x):
        return False
    nb = np.r_[x[k - width:k], x[k + 1:k + width + 1]]
    return bool(np.all(x[k] > nb)) if high else bool(np.all(x[k] < nb))


def analyze(high: Sequence[float], low: Sequence[float], close: Sequence[float], width: int = 3,
            react_atr: float = 1.5, atr_len: int = 14) -> Structure:
    """Run the swing, label and trend rules bar by bar, exactly as the Pine and MQL5 versions do."""
    h, l, c = (np.asarray(x, dtype=float) for x in (high, low, close))
    a = atr(h, l, c, atr_len)
    n = len(c)
    out = Structure(trend=np.zeros(n, dtype=int))
    cand_h: Optional[tuple[float, int]] = None     # (price, bar) waiting for its reaction
    cand_l: Optional[tuple[float, int]] = None
    hs: list[float] = []
    ls: list[float] = []
    for t in range(n):
        k = t - width
        if k >= 0 and _is_pivot(h, k, width, True) and (cand_h is None or h[k] > cand_h[0]):
            cand_h = (float(h[k]), k)
        if k >= 0 and _is_pivot(l, k, width, False) and (cand_l is None or l[k] < cand_l[0]):
            cand_l = (float(l[k]), k)
        lo_w = l[max(0, t - width + 1):t + 1].min()      # lowest low of the last `width` bars
        hi_w = h[max(0, t - width + 1):t + 1].max()
        if cand_h is not None and np.isfinite(a[t]) and lo_w <= cand_h[0] - react_atr * a[t]:
            lab = "H" if not hs else ("HH" if cand_h[0] > hs[-1] else "LH")
            out.swings.append(Swing("H", cand_h[1], t, cand_h[0], lab))
            hs.append(cand_h[0])
            cand_h = None
        if cand_l is not None and np.isfinite(a[t]) and hi_w >= cand_l[0] + react_atr * a[t]:
            lab = "L" if not ls else ("HL" if cand_l[0] > ls[-1] else "LL")
            out.swings.append(Swing("L", cand_l[1], t, cand_l[0], lab))
            ls.append(cand_l[0])
            cand_l = None
        if len(hs) >= 2 and len(ls) >= 2:
            if hs[-1] > hs[-2] and ls[-1] > ls[-2] and c[t] >= ls[-1]:
                out.trend[t] = UP
            elif hs[-1] < hs[-2] and ls[-1] < ls[-2] and c[t] <= hs[-1]:
                out.trend[t] = DOWN
    return out


def poi(st: Structure, daily_atr: float, box_half: float, mid_step: float,
        poi_atr: float = 0.5) -> Optional[tuple[float, float, int]]:
    """Daily POI from a Daily Structure: (low, high, levels clustered), or None when the Daily is ranging.
    box_half: key level box half-width (FX 10 pips = 0.0010, gold $10, S&P 10 points).
    mid_step: key level grid step (FX 250 pips = 0.0250, gold $50, S&P 50 points)."""
    d = st.last_trend
    anchor = st.last_higher_low if d == UP else st.last_lower_high if d == DOWN else None
    if anchor is None or not np.isfinite(daily_atr):
        return None
    radius = poi_atr * daily_atr
    lo, hi, count = anchor - box_half, anchor + box_half, 1
    recent = [s.price for s in st.highs()[-3:]] + [s.price for s in st.lows()[-3:]]
    for p in recent:
        if p != anchor and abs(p - anchor) <= radius:
            lo, hi, count = min(lo, p), max(hi, p), count + 1
    base = round(anchor / mid_step) * mid_step
    for k in (-1, 0, 1):
        kl = base + k * mid_step
        if abs(kl - anchor) <= radius:
            lo, hi, count = min(lo, kl - box_half), max(hi, kl + box_half), count + 1
    return lo, hi, count


def health(daily: Structure, close_4h: float) -> str:
    """'healthy', 'challenged' (4H closed beyond the last Daily higher low / lower high) or 'no trend'."""
    d = daily.last_trend
    if d == UP:
        return "challenged" if close_4h < daily.last_higher_low else "healthy"
    if d == DOWN:
        return "challenged" if close_4h > daily.last_lower_high else "healthy"
    return "no trend"


def read_market(weekly: Structure, daily: Structure, h4: Structure, close_4h: float) -> dict:
    """The full top-down read: direction from the Daily, Weekly as conviction, 4H as the health check."""
    d = daily.last_trend
    return {
        "direction": d,                                    # 1 long, -1 short, 0 = no trade
        "weekly_agrees": d != RANGE and weekly.last_trend == d,
        "h4_trend": h4.last_trend,                         # against the Daily = pullback (setup forming)
        "health": health(daily, close_4h),
    }


if __name__ == "__main__":
    # Synthetic uptrend zigzag: prints the swings and the final trend.
    rng = np.random.default_rng(1)
    base = np.cumsum(np.tile(np.r_[np.full(8, 0.4), np.full(5, -0.45)], 8)) + 100
    hi = base + rng.uniform(0.1, 0.4, len(base))
    lo = base - rng.uniform(0.1, 0.4, len(base))
    st = analyze(hi, lo, base)
    for s in st.swings:
        print(f"{s.label:>2} at bar {s.index:>3} price {s.price:.2f} (confirmed bar {s.confirm})")
    print("trend:", {UP: "up", DOWN: "down", RANGE: "range"}[st.last_trend])
