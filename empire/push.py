"""Reading the push: displacement, efficiency, sweep and reclaim, absorption, push score.

Measured on daily bars here. [IMPL] The doc allows any timeframe; daily matches the rest of this engine.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def clv(h: float, l: float, c: float) -> float:
    rng = h - l
    return 0.0 if rng <= 0 else ((c - l) - (h - c)) / rng


def efficiency_ratio(close: pd.Series, n: int) -> Optional[float]:
    c = close.dropna()
    if len(c) <= n:
        return None
    path = c.diff().abs().iloc[-n:].sum()
    return None if path <= 0 else float(abs(c.iloc[-1] - c.iloc[-n - 1]) / path)


def push_signals(bars: pd.DataFrame, atr: pd.Series, zones: list, pcfg: dict) -> list[dict]:
    """Signals in the last n bars, each with a direction (+1 up, -1 down)."""
    n = int(pcfg.get("lookback_bars", 10))
    disp_atr, clv_min = float(pcfg.get("displacement_atr", 2.0)), float(pcfg.get("clv_min", 0.6))
    reclaim_bars = int(pcfg.get("reclaim_bars", 3))
    b = bars.dropna(subset=["close"]).iloc[-(n + reclaim_bars + 1):]
    a = atr.reindex(b.index)
    out: list[dict] = []
    close_only = bool(b.get("close_only", pd.Series(False)).astype(bool).any()) if "close_only" in b else False
    tail = b.iloc[-n:]
    for t, row in tail.iterrows():
        at = a.get(t)
        if at is None or not np.isfinite(at) or at <= 0 or close_only:
            continue
        D = (row["high"] - row["low"]) / at
        cl = clv(row["high"], row["low"], row["close"])
        if D > disp_atr and abs(cl) > clv_min:
            out.append({"date": str(t.date()), "type": "displacement", "dir": int(np.sign(cl)),
                        "detail": f"range {D:.1f} ATR, close location {cl:+.2f}"})
    # sweep and reclaim: trade beyond a zone by more than its half width, close back inside within 3 bars
    if not close_only:
        for z in zones:
            top, bot, w = z.level + z.half_width, z.level - z.half_width, z.half_width
            arr = b.iloc[-(n + reclaim_bars):]
            H, L, C = arr["high"].values, arr["low"].values, arr["close"].values
            for i in range(len(arr)):
                if H[i] > top + w:
                    back = [j for j in range(i, min(len(arr), i + reclaim_bars + 1)) if C[j] <= top]
                    if back:
                        out.append({"date": str(arr.index[back[0]].date()), "type": "sweep and reclaim", "dir": -1,
                                    "detail": f"swept above {z.label}, closed back inside"})
                        break
                if L[i] < bot - w:
                    back = [j for j in range(i, min(len(arr), i + reclaim_bars + 1)) if C[j] >= bot]
                    if back:
                        out.append({"date": str(arr.index[back[0]].date()), "type": "sweep and reclaim", "dir": +1,
                                    "detail": f"swept below {z.label}, closed back inside"})
                        break
    # absorption: volume in its top 10 percent while the bar's absolute return is in its bottom half
    vol = bars["volume"] if "volume" in bars else None
    if vol is not None and vol.notna().sum() > 250:
        v = vol.iloc[-250:]
        r = bars["close"].pct_change().abs().iloc[-250:]
        vq, rq = v.quantile(0.9), r.quantile(0.5)
        for t in tail.index:
            if vol.get(t, np.nan) >= vq and r.get(t, np.inf) <= rq:
                row = bars.loc[t]
                cl = clv(row["high"], row["low"], row["close"])
                out.append({"date": str(t.date()), "type": "absorption", "dir": int(np.sign(cl)) if abs(cl) > 0.2 else 0,
                            "detail": "heavy volume, little movement: a defended level"})
    return out


def push_score(signals: list[dict]) -> int:
    return int(sum(s["dir"] for s in signals))
