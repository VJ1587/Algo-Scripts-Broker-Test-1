#!/usr/bin/env python3
"""
Dynamic Structure and Personality Layer (Dyn v0.1)
==================================================

Implements "Dynamic Structure and Personality Layer: Build Instructions v0.1" (October 8, 2026) on top of
the Daily Instrument Scanner (Addendum v0.1, Trading Algorithm Specification v1.0).

Rollout is side by side [Dyn 0.1]: every reading here is computed and logged next to the v1.0 result.
Nothing in this module changes a qualification, a grade, a rank, a score or a total. The scanner's
existing rules keep deciding until the owner switches a formula on in a later config version.

Layout
    Section 2  shared definitions      pr_rank, band_of, smoothed_rate, shrink, log_returns, atr, ema
    Section 3  personality traits      energy, noise, wickiness, swing rhythm, retracement profile,
                                       break follow through, dollar tie; shared traits per underlying
    Section 4  structure readings      vol_swings (S1), structure_score (S2), timeframe ladder (S3),
                                       pullback location (S4), break significance (S5), maturity (S6), coil (S7)
    Section 5  scaled parameters       T1 to T6 and the zone widths (T4)
    Section 6  cross market context    dollar basket, breadth, correlation breaks, context points
    Section 7  plug in                 apply_layer (called once per scan by scanner.py) and the outputs

Rule labels: [Dyn S1], [Dyn P5] and so on cite the build instructions. [IMPL] marks a choice the
instructions leave open; the README lists them. Every output field starts with "dyn_". A missing input,
a zero denominator or a sample that is too small gives "unavailable" plus a flag, never a made up value
[Dyn 0.7].

No look ahead [Dyn 0.3]: every function works on completed bars in time order with trailing windows only.
A swing is usable from the close of the bar that confirms it and is never moved afterwards.

The pure functions (Sections 2 to 6) take plain arrays and never import the scanner, so the Pine and MQL5
ports can be checked against them swing for swing [Dyn 7.5]. Only Section 7 touches scanner objects, and it
receives the v1.0 helpers it needs (pivots, Weekly bars, roll windows, grid levels) through `Hooks`.
"""
from __future__ import annotations

import csv
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

LOG = logging.getLogger("scanner.dynamics")
DYN_VERSION = "0.1"
UNAVAILABLE = "unavailable"
LONG, SHORT = "long", "short"
NY = ZoneInfo("America/New_York")
TF_2H, TF_4H, TF_D, TF_W = "2H", "4H", "D", "W"
TF_D_FULL = "D_full"          # the long Daily series (1,300 bars) the scanner loads for this layer


# =============================================================================
# Config  [Dyn 7.4]
# =============================================================================

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "trait_refresh": "weekly",          # weekly (first run after the Saturday COT pull) | always
    "percentile_window": 756,           # PR(x): trailing Daily values  [Dyn 2]
    "percentile_min_values": 100,       # [IMPL] PR needs at least this many values; fewer -> unavailable
    "shrinkage_k": 20,                  # [Dyn 2]
    "min_observations": 30,             # [Dyn 2] below this: shrink toward the group and flag "thin"
    "weekly_min_bars": 150,             # [Dyn 1.3] owner exception to the 250 bar rule
    "daily_pull_bars": 1300,            # [Dyn 1.1] flag symbols that return fewer Daily bars
    "bands": {"core": [0.25, 0.75], "stretched": [0.10, 0.90]},
    "energy": {"volatile_ratio": 1.5},                                   # P1
    "noise": {"er_bars": 20, "window": 250},                              # P2
    "wick": {"window": 250},                                              # P3
    "swing": {"theta_base": 1.5, "theta_min": 1.0, "theta_max": 3.0, "min_gap_bars": 2, "legs_window": 40},  # S1, P4
    "structure_score": {"atr_scale": 1.0},                               # S2
    "clarity": {"legs": 8, "noisy_below": 0.40, "normal_above": 0.50},   # S3
    "retracement": {"levels": [0.382, 0.5, 0.618], "fail": 0.893, "percentiles": [25, 50, 75, 90]},  # P5
    "breaks": {"deltas": [0.0, 0.10, 0.25, 0.50], "follow_through_atr": 1.0, "min_phi": 0.60, "default_depth": 0.50},  # P6
    "scaled": {"impulse_atr": {"pct": 60, "lo": 1.5, "hi": 3.0},         # T1
               "impulse_eff": {"pct": 60, "lo": 0.45, "hi": 0.75},       # T2
               "zone_frac": {"base": 0.10, "lo": 0.05, "hi": 0.15},      # T3
               "stop_buffer": {"base": 0.10, "lo": 0.05, "hi": 0.20},    # T5
               "ladder_expiry": {"mult": 1.5, "lo": 4, "hi": 12}},       # T6
    "zone_alt": {"atr_period_daily": 20, "fraction": 0.25},              # T4 alternative width
    "maturity": {"ema_long": 50, "ema_fast": 8, "ema_slow": 14, "stretched_rank": 0.90, "exhaustion_body": 0.25},  # S6
    "coil": {"atr_short": 5, "atr_long": 50, "ratio": 0.70, "release_range_atr": 2.0, "release_clv": 0.6},  # S7
    "context": {"usd_weight": 0.6, "peer_weight": 0.4, "peer_corr_min": 0.50, "breadth_min": 0.30,
                "disagree_min": 0.20, "usd_corr_bars": 60, "corr_short": 20, "corr_long": 250,
                "corr_break_z": 2.0, "corr_break_rho": 0.30,
                "points": {"plus2": 0.30, "plus1": 0.10, "minus1": -0.10, "minus2": -0.30},
                "links": [["EURUSD", "GBPUSD"], ["XAUUSD", "USD"], ["AUDUSD", "XAUUSD"]]},   # X1 to X5
    "stability_min_spearman": 0.60,     # E1 report only
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def dyn_cfg(cfg: dict) -> dict:
    """The dynamics block merged over the defaults above."""
    return _merge(DEFAULTS, cfg.get("dynamics") or {})


def enabled(cfg: dict) -> bool:
    return bool(dyn_cfg(cfg).get("enabled", False))


# =============================================================================
# Section 2: shared definitions  [Dyn 2]
# =============================================================================

def clip(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def smoothed_rate(hits: int, trials: int) -> float:
    """p = (hits + 1) / (trials + 2). Three hits in three trials is 0.80, not 1.00."""
    return (hits + 1) / (trials + 2)


def shrink(x_own: Optional[float], n: int, x_group: Optional[float], k: int = 20, min_n: int = 30) -> tuple[Optional[float], bool]:
    """x_used = (n x_own + k x_group) / (n + k) when n < min_n. Returns (x_used, thin). "thin" follows the
    observation count only; a statistic with no observations at its level stays None (unavailable)."""
    thin = n < min_n
    if x_own is None:
        return None, thin
    if not thin or x_group is None:
        return x_own, thin
    return (n * x_own + k * x_group) / (n + k), True


def pr_rank(values, window: int, min_values: int = 100) -> Optional[float]:
    """Share of the trailing `window` values (current included) that are <= the current value."""
    v = np.asarray(values, float)
    v = v[np.isfinite(v)]
    if len(v) < min_values:
        return None
    w = v[-window:]
    return float((w <= w[-1]).mean())


def band_of(pr: Optional[float], bands: dict) -> str:
    if pr is None:
        return UNAVAILABLE
    core, st = bands["core"], bands["stretched"]
    if core[0] <= pr <= core[1]:
        return "in character"
    if st[0] <= pr <= st[1]:
        return "stretched"
    return "out of character"


def log_returns(close) -> np.ndarray:
    c = np.asarray(close, float)
    r = np.full(len(c), np.nan)
    if len(c) > 1:
        r[1:] = np.log(c[1:] / c[:-1])
    return r


def wilder(tr: np.ndarray, n: int) -> np.ndarray:
    """Wilder smoothing of a true range series (tr[0] undefined), seeded with the mean of the first n valid
    values; the same recursion as the scanner's ATR so both agree to the last digit."""
    out = np.full(len(tr), np.nan)
    if len(tr) < n + 1:
        return out
    out[n] = float(np.mean(tr[1:n + 1]))
    for t in range(n + 1, len(tr)):
        out[t] = ((n - 1) * out[t - 1] + tr[t]) / n
    return out


def true_range(df: pd.DataFrame) -> np.ndarray:
    h, l, c = df["high"].values.astype(float), df["low"].values.astype(float), df["close"].values.astype(float)
    tr = np.full(len(c), np.nan)
    if len(c) > 1:
        pc = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    """Wilder ATR, identical to scanner.atr (v1.0 Section 2)."""
    return wilder(true_range(df), n)


def close_only_atr(values, n: int = 14) -> np.ndarray:
    """[Dyn X1] 14 bar Wilder average of |U[t] - U[t-1]| for a close only series."""
    v = np.asarray(values, float)
    tr = np.full(len(v), np.nan)
    if len(v) > 1:
        tr[1:] = np.abs(np.diff(v))
    return wilder(tr, n)


def ema(values, n: int) -> np.ndarray:
    """alpha = 2/(n+1), seeded with the mean of the first n values; identical to scanner.ema."""
    v = np.asarray(values, float)
    out = np.full(len(v), np.nan)
    if len(v) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = float(np.mean(v[:n]))
    for t in range(n, len(v)):
        out[t] = a * v[t] + (1 - a) * out[t - 1]
    return out


def pct(vals, q: float) -> Optional[float]:
    v = np.asarray([x for x in vals if x is not None], float)
    v = v[np.isfinite(v)]
    return float(np.percentile(v, q)) if len(v) else None


def med(vals) -> Optional[float]:
    return pct(vals, 50)


def trade_dates(idx) -> pd.DatetimeIndex:
    """[IMPL] Daily bars from different feeds open at different hours, so cross instrument alignment uses the
    trade date: (New York time + 7h).date(), the convention the empire data layer uses. A 00:00 UTC bar keeps
    its UTC date; a 17:00 New York bar belongs to the next calendar day's session."""
    i = pd.DatetimeIndex(idx)
    i = i.tz_localize("UTC") if i.tz is None else i.tz_convert("UTC")
    ny = i.tz_convert(NY) + pd.Timedelta(hours=7)
    return pd.DatetimeIndex(ny.tz_localize(None).normalize())


def close_by_date(df: pd.DataFrame) -> pd.Series:
    s = pd.Series(df["close"].values.astype(float), index=trade_dates(df.index))
    return s[~s.index.duplicated(keep="last")].sort_index()


def spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    d = pd.concat([a, b], axis=1, sort=False).dropna()
    if len(d) < 5:
        return None
    ra, rb = d.iloc[:, 0].rank(), d.iloc[:, 1].rank()
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra.values, rb.values)[0, 1])


def clean(x):
    """Native Python types with finite floats, so the JSON never stores numpy booleans as text."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return round(float(x), 8) if math.isfinite(float(x)) else None
    if isinstance(x, (pd.Timestamp, np.datetime64)):
        return str(x)
    return x


# =============================================================================
# Section 4 (first): S1 and S2 are needed by the traits, so they come before Section 3
# =============================================================================

@dataclass
class Swing:
    kind: str          # "H" or "L"
    price: float
    k: int             # bar index of the extreme
    confirm: int       # bar index whose close confirmed it; usable from that close


def theta_for(noise: Optional[float], noise_ref: Optional[float], scfg: dict) -> tuple[float, str]:
    """[Dyn S1] theta = clip(1.5 * noise / noise_ref, 1.0, 3.0). Returns (theta, flag) where the flag names
    the fallback to the base when either noise reading is missing."""
    base = float(scfg["theta_base"])
    if noise is None or noise_ref is None or noise_ref <= 0:
        return base, "theta: noise reference unavailable, base used"   # [IMPL]
    return clip(base * noise / noise_ref, float(scfg["theta_min"]), float(scfg["theta_max"])), ""


def vol_swings(high, low, close, atr_arr, theta: float, min_gap: int = 2) -> list[Swing]:
    """[Dyn S1] Volatility scaled swing state machine.
    Looking for a HIGH: track H* (highest High since the last confirmed low, at bar k*). Confirm a swing high at
    the first bar t where H* - Close[t] >= theta * ATR14[t] and t - k* >= min_gap. Then look for a low from
    k* + 1. Looking for a LOW mirrors it. Swings alternate by construction and never move.
    [IMPL] Start: both trackers run until the first swing; if both conditions hold on the same bar the earlier
    extreme wins. [IMPL] At most one swing is confirmed per bar; the new search is tested from the next bar."""
    h, l, c, a = (np.asarray(x, float) for x in (high, low, close, atr_arr))
    n = len(c)
    out: list[Swing] = []
    if n == 0:
        return out
    state: Optional[str] = None          # None = either, "H" = looking for a high, "L" = looking for a low
    hk, hv, lk, lv = 0, h[0], 0, l[0]
    for t in range(n):
        if state != "L" and h[t] > hv:
            hk, hv = t, h[t]
        if state != "H" and l[t] < lv:
            lk, lv = t, l[t]
        at = a[t]
        if not np.isfinite(at) or at <= 0:
            continue
        thr = theta * at
        ch = state != "L" and (hv - c[t] >= thr) and (t - hk >= min_gap)
        cl = state != "H" and (c[t] - lv >= thr) and (t - lk >= min_gap)
        if ch and cl:
            if hk <= lk:
                cl = False
            else:
                ch = False
        if ch:
            out.append(Swing("H", float(hv), int(hk), t))
            state = "L"
            seg = l[hk + 1:t + 1]
            j = int(np.argmin(seg))
            lk, lv = hk + 1 + j, float(seg[j])
        elif cl:
            out.append(Swing("L", float(lv), int(lk), t))
            state = "H"
            seg = h[lk + 1:t + 1]
            j = int(np.argmax(seg))
            hk, hv = lk + 1 + j, float(seg[j])
    return out


def structure_score(swings: list[Swing], atr_now: float, scale: float = 1.0) -> dict:
    """[Dyn S2] uH = tanh((High_n - High_n-1) / ATR), uL likewise; score = (uH + uL) / 2; state long when both
    rise, short when both fall, else mixed. Check: highs up 1.2 ATR, lows up 0.3 ATR -> 0.56."""
    hs = [s.price for s in swings if s.kind == "H"][-2:]
    ls = [s.price for s in swings if s.kind == "L"][-2:]
    if len(hs) < 2 or len(ls) < 2 or atr_now is None or not np.isfinite(atr_now) or atr_now <= 0:
        return {"score": None, "state": UNAVAILABLE, "uH": None, "uL": None}
    uh = math.tanh((hs[1] - hs[0]) / (scale * atr_now))
    ul = math.tanh((ls[1] - ls[0]) / (scale * atr_now))
    state = LONG if (uh > 0 and ul > 0) else SHORT if (uh < 0 and ul < 0) else "mixed"
    return {"score": (uh + ul) / 2, "state": state, "uH": uh, "uL": ul}


# =============================================================================
# Section 3: personality traits  [Dyn 3]
# =============================================================================

def efficiency_ratio(close, n: int = 20) -> np.ndarray:
    """[Dyn P2] ER[t] = |C[t] - C[t-n]| / sum of |C[j] - C[j-1]| over the last n bars; NaN on a zero path."""
    c = np.asarray(close, float)
    out = np.full(len(c), np.nan)
    d = np.abs(np.diff(c))
    for t in range(n, len(c)):
        path = d[t - n:t].sum()
        if path > 0:
            out[t] = abs(c[t] - c[t - n]) / path
    return out


def noise_of(close, er_bars: int = 20, window: int = 250, min_values: int = 50) -> Optional[float]:
    """[Dyn P2] noise = 1 - median(ER over the last `window` bars)."""
    er = efficiency_ratio(close, er_bars)[-window:]
    er = er[np.isfinite(er)]
    if len(er) < min_values:
        return None
    return float(1 - np.median(er))


def wick_of(df: pd.DataFrame, window: int = 250, min_values: int = 50) -> Optional[float]:
    """[Dyn P3] mean over the last `window` bars with High > Low of 1 - |C - O| / (H - L)."""
    sub = df.iloc[-window:]
    rng = (sub["high"] - sub["low"]).values.astype(float)
    body = (sub["close"] - sub["open"]).abs().values.astype(float)
    ok = rng > 0
    if ok.sum() < min_values:
        return None
    return float(np.mean(1 - body[ok] / rng[ok]))


def natr_of(df: pd.DataFrame, n: int = 14) -> Optional[float]:
    """[Dyn P1] NATR = ATR14 / Close on the latest completed bar."""
    a = atr(df, n)
    if not len(a) or not np.isfinite(a[-1]):
        return None
    c = float(df["close"].values[-1])
    return float(a[-1] / c) if c > 0 else None


def swing_legs(swings: list[Swing], close, atr_arr, times, last_n: int = 40) -> list[dict]:
    """[Dyn P4] One record per leg between consecutive swings: size in ATR at the confirming bar, bars, and the
    v1.0 I01 efficiency of the closes across the leg. The last `last_n` legs."""
    c, a = np.asarray(close, float), np.asarray(atr_arr, float)
    legs = []
    for i in range(1, len(swings)):
        p, q = swings[i - 1], swings[i]
        ac = a[q.confirm]
        if not np.isfinite(ac) or ac <= 0:
            continue
        path = float(np.abs(np.diff(c[p.k:q.k + 1])).sum())
        if path <= 0:
            continue
        legs.append({"size": abs(q.price - p.price) / ac, "bars": int(q.k - p.k),
                     "eff": abs(c[q.k] - c[p.k]) / path, "date": pd.Timestamp(times[q.k])})
    return legs[-last_n:] if last_n else legs


def leg_stats(legs: list[dict]) -> dict:
    out = {"n": len(legs)}
    for key in ("size", "bars", "eff"):
        vals = [x[key] for x in legs]
        out[f"{key}_med"] = med(vals)
        out[f"{key}_p60"] = pct(vals, 60)
    return out


def enumerate_impulses(seq, close, high, low, atr_arr, times, icfg: dict, fail: float = 0.893) -> list[dict]:
    """[Dyn P5] Every A -> B pair of the v1.0 alternating two bar pivots (`seq`: objects with kind, price, k,
    confirm) that passes the I01 gates (span, 2 x ATR, efficiency, break of the preceding swing), walked forward
    bar by bar from the bar after B: rho = (B - P) / (B - A) with P the bar Low for a long impulse, the bar High
    for a short; "continued" when price trades beyond B, "failed" when rho_max >= 0.893, undecided impulses
    are excluded. The recency gate and the void beyond origin switch are ignored: this is a statistic.
    [IMPL] When a bar both retraces to the fail level and trades beyond B, continued wins."""
    c, h, l, a = (np.asarray(x, float) for x in (close, high, low, atr_arr))
    n = len(c)
    out = []
    for i in range(1, len(seq)):
        A, B = seq[i - 1], seq[i]
        if A.kind == B.kind:
            continue
        direction = LONG if B.kind == "H" else SHORT
        span = B.k - A.k
        if not (int(icfg["min_span_bars"]) <= span <= int(icfg["max_span_bars"])):
            continue
        ab = a[B.k]
        if not np.isfinite(ab) or ab <= 0 or abs(B.price - A.price) < float(icfg["min_atr_mult"]) * ab:
            continue
        path = float(np.abs(np.diff(c[A.k:B.k + 1])).sum())
        if path <= 0 or abs(c[B.k] - c[A.k]) / path < float(icfg["min_efficiency"]):
            continue
        prev_same = [p for p in seq[:i - 1] if p.kind == B.kind]
        if not prev_same:
            continue
        if direction == LONG and not B.price > prev_same[-1].price:
            continue
        if direction == SHORT and not B.price < prev_same[-1].price:
            continue
        rng = B.price - A.price
        rho_max, bar_rho, outcome = 0.0, B.k, None
        for j in range(B.k + 1, n):
            P = l[j] if direction == LONG else h[j]
            rho = (B.price - P) / rng
            if rho > rho_max:
                rho_max, bar_rho = rho, j
            if (direction == LONG and h[j] > B.price) or (direction == SHORT and l[j] < B.price):
                outcome = "continued"
                break
            if rho_max >= fail:
                outcome = "failed"
                break
        if outcome is None:
            continue
        out.append({"direction": direction, "rho_max": float(rho_max), "continued": outcome == "continued",
                    "bars_to_rho": int(bar_rho - B.k), "date": pd.Timestamp(times[B.k])})
    return out


def retracement_profile(obs: list[dict], levels: list[float], pcts: list[int]) -> dict:
    """[Dyn P5] reach(r) and hold(r) as smoothed rates; percentiles of rho_max among continued impulses;
    median bars from B to the rho_max bar among continued impulses (feeds T6) [IMPL]."""
    N = len(obs)
    cont = [o["rho_max"] for o in obs if o["continued"]]
    out: dict[str, Any] = {"n": N, "n_continued": len(cont), "reach": {}, "hold": {}, "rho_pct": {},
                           "rho_continued": sorted(round(x, 4) for x in cont),
                           "bars_to_rho_med": med([o["bars_to_rho"] for o in obs if o["continued"]])}
    for r in levels:
        key = f"{r:.3f}"
        if N == 0:
            out["reach"][key] = out["hold"][key] = None
            continue
        reached = [o for o in obs if o["rho_max"] >= r]
        out["reach"][key] = smoothed_rate(len(reached), N)
        out["hold"][key] = smoothed_rate(sum(o["continued"] for o in reached), len(reached))
    for p in pcts:
        out["rho_pct"][str(p)] = pct(cont, p)
    return out


def break_events(close, high, low, atr_arr, swings: list[Swing], times, travel_atr: float = 1.0) -> list[dict]:
    """[Dyn P6] A break is the first Daily close beyond the most recent confirmed S1 swing (confirmed at or before
    that bar); depth in ATR at the break bar. Followed through when a later bar travels a further `travel_atr`
    ATR past the break close before any Daily close back inside (beyond the swing level the other way). An
    undecided break at the end of the data is excluded."""
    c, h, l, a = (np.asarray(x, float) for x in (close, high, low, atr_arr))
    highs = [s for s in swings if s.kind == "H"]
    lows = [s for s in swings if s.kind == "L"]
    ih = il = -1
    broken_h: set[int] = set()
    broken_l: set[int] = set()
    out = []
    n = len(c)

    def outcome(t: int, sign: int, level: float) -> Optional[bool]:
        target = c[t] + sign * travel_atr * a[t]
        for j in range(t + 1, n):
            if (sign > 0 and h[j] >= target) or (sign < 0 and l[j] <= target):
                return True
            if (sign > 0 and c[j] < level) or (sign < 0 and c[j] > level):
                return False
        return None

    for t in range(n):
        while ih + 1 < len(highs) and highs[ih + 1].confirm <= t:
            ih += 1
        while il + 1 < len(lows) and lows[il + 1].confirm <= t:
            il += 1
        if not np.isfinite(a[t]) or a[t] <= 0:
            continue
        if ih >= 0 and ih not in broken_h and c[t] > highs[ih].price:
            broken_h.add(ih)
            res = outcome(t, +1, highs[ih].price)
            if res is not None:
                out.append({"direction": LONG, "depth": (c[t] - highs[ih].price) / a[t], "followed": res,
                            "date": pd.Timestamp(times[t])})
        if il >= 0 and il not in broken_l and c[t] < lows[il].price:
            broken_l.add(il)
            res = outcome(t, -1, lows[il].price)
            if res is not None:
                out.append({"direction": SHORT, "depth": (lows[il].price - c[t]) / a[t], "followed": res,
                            "date": pd.Timestamp(times[t])})
    return out


def break_profile(events: list[dict], deltas: list[float], min_phi: float, default_depth: float) -> dict:
    """[Dyn P6] phi(delta) for each depth; break_depth_min = smallest delta with phi >= min_phi, else default."""
    out: dict[str, Any] = {"n": len(events), "phi": {}}
    for d in deltas:
        sel = [e for e in events if e["depth"] >= d]
        out["phi"][f"{d:.2f}"] = smoothed_rate(sum(e["followed"] for e in sel), len(sel)) if sel else None
    chosen = None
    for d in deltas:
        p = out["phi"][f"{d:.2f}"]
        if p is not None and p >= min_phi:
            chosen = d
            break
    out["break_depth_min"] = default_depth if chosen is None else chosen
    out["break_depth_min_default"] = chosen is None
    return out


def usd_basket(daily_by_symbol: dict[str, pd.DataFrame], majors: list[str]) -> tuple[Optional[pd.DataFrame], list[str]]:
    """[Dyn X1] q = +1 for USDxxx, -1 for xxxUSD; r_USD = mean of q r over the majors on dates where all have a
    bar; U = 100 exp(cumsum r_USD). Returns (frame with r_usd and U indexed by trade date, missing majors)."""
    missing = [m for m in majors if m not in daily_by_symbol or daily_by_symbol[m] is None or daily_by_symbol[m].empty]
    if missing:
        return None, missing
    closes = pd.DataFrame({m: close_by_date(daily_by_symbol[m]) for m in majors}).dropna()
    if len(closes) < 30:
        return None, ["fewer than 30 common dates"]
    r = np.log(closes).diff().dropna()
    q = pd.Series({m: (1.0 if m.startswith("USD") else -1.0) for m in majors})
    r_usd = (r * q).sum(axis=1) / len(majors)
    return pd.DataFrame({"r_usd": r_usd, "U": 100.0 * np.exp(r_usd.cumsum())}), []


def correlation(a: pd.Series, b: pd.Series, window: int, min_frac: float = 0.8) -> Optional[float]:
    d = pd.concat([a, b], axis=1, sort=True).dropna().iloc[-window:]
    if len(d) < min_frac * window:
        return None
    x, y = d.iloc[:, 0].values, d.iloc[:, 1].values
    if x.std() == 0 or y.std() == 0:
        return None
    return float(np.corrcoef(x, y)[0, 1])


@dataclass
class SymBars:
    """What the trait build needs for one instrument. `seq4` is the v1.0 alternating two bar pivot sequence
    on the 4H bars (supplied by the scanner through Hooks)."""
    symbol: str
    group_key: str                  # fx_major | fx_cross | gold | spx | oil
    underlying: Optional[str]
    roll: Optional[str]
    dfd: pd.DataFrame
    df4: pd.DataFrame
    seq4: list


def raw_traits(bars: dict[str, SymBars], basket: Optional[pd.DataFrame], majors: list[str], icfg: dict,
               dcfg: dict) -> tuple[dict[str, dict], dict]:
    """[Dyn 3] Stage 1 (bar based traits, then the FX major references) and stage 2 (swings and observation
    lists) for every instrument, on its own bars. No pooling or shrinkage yet. Returns (raw by symbol, refs)."""
    ncfg, wcfg, scfg = dcfg["noise"], dcfg["wick"], dcfg["swing"]
    rcfg, bcfg, ccfg = dcfg["retracement"], dcfg["breaks"], dcfg["context"]
    raw: dict[str, dict] = {}
    for sym, sb in bars.items():
        raw[sym] = {"n_bars_d": len(sb.dfd), "n_bars_4h": len(sb.df4), "flags": [],
                    "natr": natr_of(sb.dfd),
                    "noise_4h": noise_of(sb.df4["close"].values, ncfg["er_bars"], ncfg["window"]),
                    "noise_d": noise_of(sb.dfd["close"].values, ncfg["er_bars"], ncfg["window"]),
                    "wick_4h": wick_of(sb.df4, wcfg["window"]), "wick_d": wick_of(sb.dfd, wcfg["window"])}
    refs = {}
    for key in ("natr", "noise_4h", "noise_d", "wick_4h", "wick_d"):
        vals = [raw[m][key] for m in majors if m in raw and raw[m][key] is not None]
        refs[key] = float(np.median(vals)) if len(vals) >= 4 else None     # [IMPL] at least 4 of the 7 majors
        refs[f"{key}_n"] = len(vals)
    for sym, sb in bars.items():
        r = raw[sym]
        th4, f4 = theta_for(r["noise_4h"], refs["noise_4h"], scfg)
        thd, fd = theta_for(r["noise_d"], refs["noise_d"], scfg)
        r["theta_4h"], r["theta_d"] = th4, thd
        r["flags"] += [x for x in (f4, fd) if x]
        a4, ad = atr(sb.df4), atr(sb.dfd)
        sw4 = vol_swings(sb.df4["high"].values, sb.df4["low"].values, sb.df4["close"].values, a4, th4, int(scfg["min_gap_bars"]))
        swd = vol_swings(sb.dfd["high"].values, sb.dfd["low"].values, sb.dfd["close"].values, ad, thd, int(scfg["min_gap_bars"]))
        r["legs_4h"] = swing_legs(sw4, sb.df4["close"].values, a4, sb.df4.index, int(scfg["legs_window"]))
        r["legs_d"] = swing_legs(swd, sb.dfd["close"].values, ad, sb.dfd.index, int(scfg["legs_window"]))
        r["impulses"] = enumerate_impulses(sb.seq4, sb.df4["close"].values, sb.df4["high"].values, sb.df4["low"].values,
                                           a4, sb.df4.index, icfg, float(rcfg["fail"]))
        r["breaks"] = break_events(sb.dfd["close"].values, sb.dfd["high"].values, sb.dfd["low"].values, ad, swd,
                                   sb.dfd.index, float(bcfg["follow_through_atr"]))
        r["usd_corr_60"] = r["usd_corr_250"] = None
        if basket is not None:
            cbd = close_by_date(sb.dfd)
            ri = pd.Series(log_returns(cbd.values), index=cbd.index)
            r["usd_corr_60"] = correlation(ri, basket["r_usd"], int(ccfg["usd_corr_bars"]))
            r["usd_corr_250"] = correlation(ri, basket["r_usd"], int(ccfg["corr_long"]))
        else:
            r["flags"].append("dollar basket unavailable")
    return raw, refs


def _in_roll(o: dict, roll: Optional[str], rules: dict, in_roll_window: Optional[Callable]) -> bool:
    if not roll or in_roll_window is None:
        return False
    d = o["date"]
    return bool(in_roll_window(d.date() if hasattr(d, "date") else d, roll, rules))


def summarize_traits(raw: dict[str, dict], bars: dict[str, SymBars], refs: dict, dcfg: dict, roll_rules: dict,
                     in_roll_window: Optional[Callable], majors: list[str]) -> dict[str, dict]:
    """[Dyn 3, 3.1, 5] Pool observations per underlying, average bar based traits, shrink thin statistics toward
    the group, flag "thin", and derive the scaled parameters T1 to T6. One entry per instrument; instruments
    on the same underlying get identical trait sets."""
    k, min_n = int(dcfg["shrinkage_k"]), int(dcfg["min_observations"])
    rcfg, bcfg, scaled = dcfg["retracement"], dcfg["breaks"], dcfg["scaled"]
    levels, pcts = [float(x) for x in rcfg["levels"]], [int(x) for x in rcfg["percentiles"]]
    deltas = [float(x) for x in bcfg["deltas"]]
    # pools: underlying (or the symbol itself) and group
    members: dict[str, list[str]] = {}
    for sym, sb in bars.items():
        members.setdefault(sb.underlying or sym, []).append(sym)
    group_syms: dict[str, list[str]] = {}
    for sym, sb in bars.items():
        group_syms.setdefault(sb.group_key, []).append(sym)

    def pooled(syms: list[str], key: str) -> list[dict]:
        out = []
        for s in syms:
            sb = bars[s]
            out += [o for o in raw[s][key] if not _in_roll(o, sb.roll, roll_rules, in_roll_window)]
        return out

    def mean_of(syms: list[str], key: str) -> Optional[float]:
        vals = [raw[s][key] for s in syms if raw[s][key] is not None]
        return float(np.mean(vals)) if vals else None

    def profile_stats(obs: list[dict]) -> dict:
        p = retracement_profile(obs, levels, pcts)
        flat = {f"reach_{r}": v for r, v in p["reach"].items()}
        flat.update({f"hold_{r}": v for r, v in p["hold"].items()})
        flat.update({f"rho_p{q}": v for q, v in p["rho_pct"].items()})
        flat["bars_to_rho_med"] = p["bars_to_rho_med"]
        return flat

    def phi_stats(events: list[dict]) -> dict:
        p = break_profile(events, deltas, float(bcfg["min_phi"]), float(bcfg["default_depth"]))
        return {f"phi_{d}": v for d, v in p["phi"].items()}

    group_cache: dict[tuple[str, str], Any] = {}

    def group_stat(gkey: str, key: str, fn) -> dict:
        ck = (gkey, key)
        if ck not in group_cache:
            group_cache[ck] = fn(pooled(group_syms[gkey], key))
        return group_cache[ck]

    def shrunk(own: dict, n: int, grp: dict, flags: list[str], label: str) -> dict:
        out = {}
        for key, val in own.items():
            out[key], _ = shrink(val, n, grp.get(key), k, min_n)
            if val is None:
                flags.append(f"{label} {key}: unavailable (no observations)")
        if n < min_n:
            flags.append(f"thin: {label} ({n} observations)")
        return out

    out: dict[str, dict] = {}
    for ukey, syms in members.items():
        gkey = bars[syms[0]].group_key
        flags: list[str] = []
        for s in syms:
            flags += [f"{s}: {x}" for x in raw[s]["flags"]]
        natr = mean_of(syms, "natr")
        energy_rel = (natr / refs["natr"]) if (natr is not None and refs.get("natr")) else None
        tr: dict[str, Any] = {
            "underlying": ukey, "members": syms, "group": gkey,
            "bars": {s: {"daily": raw[s]["n_bars_d"], "h4": raw[s]["n_bars_4h"]} for s in syms},
            "natr": natr, "energy_rel": energy_rel,
            "vol_class": (UNAVAILABLE if energy_rel is None else
                          "volatile" if energy_rel >= float(dcfg["energy"]["volatile_ratio"]) else "standard"),
            "noise_4h": mean_of(syms, "noise_4h"), "noise_d": mean_of(syms, "noise_d"),
            "wick_4h": mean_of(syms, "wick_4h"), "wick_d": mean_of(syms, "wick_d"),
            "theta_4h": mean_of(syms, "theta_4h"), "theta_d": mean_of(syms, "theta_d"),
            "usd_corr_60": mean_of(syms, "usd_corr_60"), "usd_corr_250": mean_of(syms, "usd_corr_250"),
            "conviction": None, "level_respect": None,        # P7: empire modules not wired in yet
        }
        # P4 swing rhythm, pooled per underlying; thin test on the larger single series count [Dyn 3.1]
        for tf in ("4h", "d"):
            key = f"legs_{tf}"
            obs = pooled(syms, key)
            n_single = max(len(raw[s][key]) for s in syms)
            own = {kk: v for kk, v in leg_stats(obs).items() if kk != "n"}
            grp = {kk: v for kk, v in group_stat(gkey, key, leg_stats).items() if kk != "n"}
            tr[key] = {"n": len(obs), "n_single": n_single, **shrunk(own, n_single, grp, flags, f"legs {tf}")}
        # P5 retracement profile
        obs = pooled(syms, "impulses")
        n_single = max(len(raw[s]["impulses"]) for s in syms)
        own = profile_stats(obs)
        grp = group_stat(gkey, "impulses", profile_stats)
        tr["retracement"] = {"n": len(obs), "n_single": n_single, "n_continued": sum(o["continued"] for o in obs),
                             "rho_continued": sorted(round(o["rho_max"], 4) for o in obs if o["continued"]),
                             **shrunk(own, n_single, grp, flags, "impulses")}
        # P6 break follow through
        ev = pooled(syms, "breaks")
        n_single = max(len(raw[s]["breaks"]) for s in syms)
        own = phi_stats(ev)
        grp = group_stat(gkey, "breaks", phi_stats)
        used = shrunk(own, n_single, grp, flags, "breaks")
        chosen = next((d for d in deltas if used.get(f"phi_{d:.2f}") is not None and used[f"phi_{d:.2f}"] >= float(bcfg["min_phi"])), None)
        tr["breaks"] = {"n": len(ev), "n_single": n_single, **used,
                        "break_depth_min": float(bcfg["default_depth"]) if chosen is None else chosen,
                        "break_depth_min_default": chosen is None}
        # Section 5 scaled parameters from the used traits
        sp: dict[str, Any] = {}
        c1, c2, c3, c5, c6 = scaled["impulse_atr"], scaled["impulse_eff"], scaled["zone_frac"], scaled["stop_buffer"], scaled["ladder_expiry"]
        v = tr["legs_4h"].get("size_p60")
        sp["dyn_impulse_min_atr"] = None if v is None else clip(v, float(c1["lo"]), float(c1["hi"]))
        v = tr["legs_4h"].get("eff_p60")
        sp["dyn_impulse_min_eff"] = None if v is None else clip(v, float(c2["lo"]), float(c2["hi"]))
        n4, nref = tr["noise_4h"], refs.get("noise_4h")
        sp["dyn_zone_frac"] = None if (n4 is None or not nref) else clip(float(c3["base"]) * n4 / nref, float(c3["lo"]), float(c3["hi"]))
        w4, wref = tr["wick_4h"], refs.get("wick_4h")
        sp["dyn_stop_buffer"] = None if (w4 is None or not wref) else clip(float(c5["base"]) * w4 / wref, float(c5["lo"]), float(c5["hi"]))
        b = tr["retracement"].get("bars_to_rho_med")
        sp["dyn_ladder_expiry"] = None if b is None else int(clip(round(float(c6["mult"]) * b), int(c6["lo"]), int(c6["hi"])))
        tr["scaled"] = sp
        tr["flags"] = flags
        tr["thin"] = any(f.startswith("thin") for f in flags)
        for s in syms:
            out[s] = tr
    return out


def trait_table(raw: dict[str, dict], dcfg: dict) -> pd.DataFrame:
    """Scalar traits per instrument, raw (no pooling, no shrinkage), for the E1 stability check."""
    rcfg, bcfg = dcfg["retracement"], dcfg["breaks"]
    rows = {}
    for sym, r in raw.items():
        l4 = leg_stats(r["legs_4h"])
        prof = retracement_profile(r["impulses"], [float(x) for x in rcfg["levels"]], [50])
        brk = break_profile(r["breaks"], [float(x) for x in bcfg["deltas"]], float(bcfg["min_phi"]), float(bcfg["default_depth"]))
        rows[sym] = {"natr": r["natr"], "noise_4h": r["noise_4h"], "noise_d": r["noise_d"], "wick_4h": r["wick_4h"],
                     "wick_d": r["wick_d"], "leg_size_p60_4h": l4["size_p60"], "leg_eff_p60_4h": l4["eff_p60"],
                     "leg_bars_med_4h": l4["bars_med"], "reach_0.500": prof["reach"].get("0.500"),
                     "hold_0.500": prof["hold"].get("0.500"), "rho_p50": prof["rho_pct"].get("50"),
                     "phi_0.00": brk["phi"].get("0.00"), "usd_corr_250": r["usd_corr_250"]}
    return pd.DataFrame(rows).T.astype(float)


class _Shifted:
    """A pivot re-indexed into the second half of a series (k and confirm minus the offset)."""

    def __init__(self, p, off: int):
        self.kind, self.price, self.k, self.confirm = p.kind, p.price, p.k - off, p.confirm - off


def stability(bars: dict[str, SymBars], basket: Optional[pd.DataFrame], majors: list[str], icfg: dict, dcfg: dict) -> dict:
    """[Dyn E1] Rank instruments on each trait in the first and second half of history (each series split by
    its own bar count [IMPL]); Spearman >= 0.60 keeps the trait. Report only."""
    halves = []
    for part in (0, 1):
        sl = {}
        for sym, sb in bars.items():
            nd, n4 = len(sb.dfd), len(sb.df4)
            d = sb.dfd.iloc[: nd // 2] if part == 0 else sb.dfd.iloc[nd // 2:]
            h4 = sb.df4.iloc[: n4 // 2] if part == 0 else sb.df4.iloc[n4 // 2:]
            if part == 0:
                seq = [p for p in sb.seq4 if p.confirm < n4 // 2]
            else:
                seq = [_Shifted(p, n4 // 2) for p in sb.seq4 if p.k >= n4 // 2 + 2 and p.confirm >= n4 // 2]
            sl[sym] = SymBars(sym, sb.group_key, sb.underlying, sb.roll, d, h4, seq)
        raw, _ = raw_traits(sl, basket, majors, icfg, dcfg)
        halves.append(trait_table(raw, dcfg))
    out = {}
    for col in halves[0].columns:
        rho = spearman(halves[0][col], halves[1][col])
        out[col] = {"spearman": rho, "keep": None if rho is None else rho >= float(dcfg["stability_min_spearman"]),
                    "n": int(pd.concat([halves[0][col], halves[1][col]], axis=1).dropna().shape[0])}
    return out


# =============================================================================
# Section 4 (rest): per scan structure readings  [Dyn S3 to S7]
# =============================================================================

def tf_mode_next(prev: Optional[str], clarity: Optional[float], noisy_below: float, normal_above: float) -> str:
    """[Dyn S3] Hysteresis: normal -> noisy below 0.40, noisy -> normal at or above 0.50; unchanged in between."""
    mode = prev or "normal"
    if clarity is None:
        return mode
    if mode == "noisy":
        return "normal" if clarity >= normal_above else "noisy"
    return "noisy" if clarity < noisy_below else "normal"


def pullback_location(a: float, b: float, close_2h: float, rho_continued: list[float], levels: list[float],
                      reach: dict, hold: dict) -> dict:
    """[Dyn S4] depth = (B - Close_2H) / (B - A); rank = share of continued rho_max values <= depth; zone bands;
    reach and hold at the ladder level nearest the current price."""
    rng = b - a
    if rng == 0:
        return {"dyn_pullback_depth": None, "dyn_pullback_rank": None, "dyn_pullback_zone": UNAVAILABLE}
    depth = (b - close_2h) / rng
    out: dict[str, Any] = {"dyn_pullback_depth": depth}
    if rho_continued:
        rank = float(np.mean(np.asarray(rho_continued) <= depth))
        out["dyn_pullback_rank"] = rank
        out["dyn_pullback_zone"] = ("shallow" if rank < 0.25 else "normal" if rank <= 0.75 else
                                    "deep" if rank <= 0.90 else "out of character")
    else:
        out["dyn_pullback_rank"], out["dyn_pullback_zone"] = None, UNAVAILABLE
    nearest = min(levels, key=lambda r: abs(r - depth))
    key = f"{nearest:.3f}"
    out["dyn_nearest_level"] = nearest
    out["dyn_reach"], out["dyn_hold"] = reach.get(key), hold.get(key)
    return out


def break_significance(bias: str, inv: Optional[float], close_d: float, prev_close_d: Optional[float], atr_d: float,
                       grid_major: float, break_depth_min: Optional[float]) -> dict:
    """[Dyn S5] Signed distance of the Daily close beyond the bias invalidation level in ATR (positive = closed
    beyond), break_depth_min beside it, and the "psych level close against bias" flag when the latest Daily
    close crossed a major grid level against the bias."""
    out: dict[str, Any] = {"dyn_break_depth": None, "dyn_break_depth_min": break_depth_min, "dyn_psych_cross": False}
    if bias not in (LONG, SHORT) or inv is None or not np.isfinite(atr_d) or atr_d <= 0:
        return out
    out["dyn_break_depth"] = (inv - close_d) / atr_d if bias == LONG else (close_d - inv) / atr_d
    if prev_close_d is not None and grid_major > 0:
        if bias == LONG:
            lv = math.floor(prev_close_d / grid_major) * grid_major
            out["dyn_psych_cross"] = bool(close_d < lv <= prev_close_d)
        else:
            lv = math.ceil(prev_close_d / grid_major) * grid_major
            out["dyn_psych_cross"] = bool(close_d > lv >= prev_close_d)
    return out


def _touches_grid(low: float, high: float, grid_mid: float, hw: float) -> bool:
    if grid_mid <= 0:
        return False
    return math.floor((high + hw) / grid_mid) >= math.ceil((low - hw) / grid_mid)


def maturity(dfd: pd.DataFrame, bias: str, grid_mid: float, zone_hw: float, mcfg: dict, window: int, min_values: int) -> dict:
    """[Dyn S6] stretch = (Close - EMA50) / ATR14 with its percentile rank; EMA8 - EMA14 spread; body ratio; the
    four warning flags relative to the Daily bias."""
    c = dfd["close"].values.astype(float)
    o, h, l = (dfd[x].values.astype(float) for x in ("open", "high", "low"))
    a = atr(dfd, 14)
    e_long, e_fast, e_slow = ema(c, int(mcfg["ema_long"])), ema(c, int(mcfg["ema_fast"])), ema(c, int(mcfg["ema_slow"]))
    with np.errstate(invalid="ignore", divide="ignore"):
        stretch = (c - e_long) / a
        spread = (e_fast - e_slow) / a
    rng = h - l
    body = np.where(rng > 0, np.abs(c - o) / np.where(rng > 0, rng, 1.0), np.nan)
    t = len(c) - 1
    out: dict[str, Any] = {"dyn_stretch": None, "dyn_stretch_rank": None, "dyn_stretch_band": UNAVAILABLE,
                           "dyn_ema_spread": None, "dyn_body_ratio": None, "flags": []}
    if t < 1 or not np.isfinite(stretch[t]):
        return out
    out["dyn_stretch"] = float(stretch[t])
    out["dyn_ema_spread"] = float(spread[t]) if np.isfinite(spread[t]) else None
    out["dyn_body_ratio"] = float(body[t]) if np.isfinite(body[t]) else None
    hi_rank = float(mcfg["stretched_rank"])

    def stretched_at(i: int) -> Optional[bool]:
        pr = pr_rank(stretch[: i + 1], window, min_values)
        if pr is None:
            return None
        return (bias == LONG and pr >= hi_rank) or (bias == SHORT and pr <= 1 - hi_rank)

    def exhaustion_at(i: int) -> bool:
        st = stretched_at(i)
        return bool(st and np.isfinite(body[i]) and body[i] <= float(mcfg["exhaustion_body"])
                    and _touches_grid(l[i], h[i], grid_mid, zone_hw))

    pr_now = pr_rank(stretch[: t + 1], window, min_values)
    out["dyn_stretch_rank"] = pr_now
    out["dyn_stretch_band"] = band_of(pr_now, {"core": [0.25, 0.75], "stretched": [0.10, 0.90]})
    if bias not in (LONG, SHORT):
        return out
    if stretched_at(t):
        out["flags"].append("stretched")
    if np.isfinite(spread[t]) and np.isfinite(spread[t - 1]):
        if (bias == LONG and spread[t - 1] > 0 >= spread[t]) or (bias == SHORT and spread[t - 1] < 0 <= spread[t]):
            out["flags"].append("momentum fading")
    if exhaustion_at(t):
        out["flags"].append("exhaustion candle")
    if t >= 2 and exhaustion_at(t - 1):
        far = l[t - 1] if bias == LONG else h[t - 1]
        if (bias == LONG and c[t] < far) or (bias == SHORT and c[t] > far):
            out["flags"].append("exhaustion confirmed")
    return out


def coil(dfd: pd.DataFrame, ccfg: dict) -> dict:
    """[Dyn S7] ATR5 / ATR50 (Wilder); coiled at or under 0.70; release on the first Daily bar after a coil with
    range > 2 x ATR14 and |CLV| > 0.6. [IMPL] ATR14 of the bar before the release bar sizes the range test."""
    tr = true_range(dfd)
    a_s, a_l, a14 = wilder(tr, int(ccfg["atr_short"])), wilder(tr, int(ccfg["atr_long"])), wilder(tr, 14)
    t = len(dfd) - 1
    out: dict[str, Any] = {"dyn_coil_ratio": None, "flags": []}
    if t < 1 or not np.isfinite(a_s[t]) or not np.isfinite(a_l[t]) or a_l[t] <= 0:
        return out
    ratio = float(a_s[t] / a_l[t])
    out["dyn_coil_ratio"] = ratio
    if ratio <= float(ccfg["ratio"]):
        out["flags"].append("coiled")
    prev_coiled = (np.isfinite(a_s[t - 1]) and np.isfinite(a_l[t - 1]) and a_l[t - 1] > 0
                   and a_s[t - 1] / a_l[t - 1] <= float(ccfg["ratio"]))
    h, l, c = (float(dfd[x].values[t]) for x in ("high", "low", "close"))
    rng = h - l
    if prev_coiled and rng > 0 and np.isfinite(a14[t - 1]) and rng > float(ccfg["release_range_atr"]) * a14[t - 1]:
        clv = ((c - l) - (h - c)) / rng
        if abs(clv) > float(ccfg["release_clv"]):
            out["flags"].append("release")
    return out


def impulse_passes(seq, close, atr_arr, direction: str, icfg: dict, min_mult: float, min_eff: float, T: int) -> Optional[bool]:
    """[Dyn T1, T2] Would the latest candidate A -> B pair (B confirmed within max_age_4h_bars) pass with the given
    size and efficiency gates? Same gates as v1.0 select_impulse, including void beyond origin. None = no
    candidate pair in the window."""
    c, a = np.asarray(close, float), np.asarray(atr_arr, float)
    a_kind, b_kind = ("L", "H") if direction == LONG else ("H", "L")
    seen = False
    for i in range(len(seq) - 1, 0, -1):
        A, B = seq[i - 1], seq[i]
        if A.kind != a_kind or B.kind != b_kind:
            continue
        if B.confirm < T - (int(icfg["max_age_4h_bars"]) - 1):
            break
        seen = True
        span = B.k - A.k
        if not (int(icfg["min_span_bars"]) <= span <= int(icfg["max_span_bars"])):
            continue
        ab = a[B.k]
        if not np.isfinite(ab) or ab <= 0 or abs(B.price - A.price) < min_mult * ab:
            continue
        path = float(np.abs(np.diff(c[A.k:B.k + 1])).sum())
        if path <= 0 or abs(c[B.k] - c[A.k]) / path < min_eff:
            continue
        prev_same = [p for p in seq[:i - 1] if p.kind == b_kind]
        if not prev_same:
            continue
        if direction == LONG and not B.price > prev_same[-1].price:
            continue
        if direction == SHORT and not B.price < prev_same[-1].price:
            continue
        if icfg.get("void_beyond_origin", True):
            after = c[B.k + 1:T + 1]
            if (direction == LONG and (after < A.price).any()) or (direction == SHORT and (after > A.price).any()):
                continue
        return True
    return False if seen else None


# =============================================================================
# Section 6: cross market context  [Dyn X1 to X5]
# =============================================================================

def fisher_z(rho_short: float, rho_long: float, n_short: int = 20, n_long: int = 250) -> float:
    cl = lambda r: max(min(r, 0.999999), -0.999999)  # noqa: E731
    return (math.atanh(cl(rho_short)) - math.atanh(cl(rho_long))) / math.sqrt(1 / (n_short - 3) + 1 / (n_long - 3))


def corr_break(r1: pd.Series, r2: pd.Series, short: int = 20, long: int = 250, z_min: float = 2.0, rho_min: float = 0.30) -> dict:
    """[Dyn X4] Z = (atanh(rho_20) - atanh(rho_250)) / sqrt(1/17 + 1/247); break if |Z| >= 2 or the signs differ
    while |rho_250| >= 0.30. Check: rho_250 0.80, rho_20 0.30 -> Z -3.1."""
    rho_l = correlation(r1, r2, long)
    rho_s = correlation(r1, r2, short, min_frac=1.0)
    out = {"rho_short": rho_s, "rho_long": rho_l, "z": None, "broken": None}
    if rho_s is None or rho_l is None:
        return out
    z = fisher_z(rho_s, rho_l, short, long)
    out["z"] = z
    out["broken"] = bool(abs(z) >= z_min or (np.sign(rho_s) != np.sign(rho_l) and abs(rho_l) >= rho_min))
    return out


def context_points(score: Optional[float], cuts: dict, conflict: bool = False) -> Optional[int]:
    """[Dyn X5] >= 0.30 +2 | >= 0.10 +1 | > -0.10 0 | > -0.30 -1 | else -2; a context conflict caps at 0."""
    if score is None:
        return None
    p = (2 if score >= cuts["plus2"] else 1 if score >= cuts["plus1"] else 0 if score > cuts["minus1"]
         else -1 if score > cuts["minus2"] else -2)
    return min(p, 0) if conflict else p


def context_score(d: int, usd_corr_60: Optional[float], usd_score: Optional[float], kappa: float, m_peer: float,
                  w_usd: float = 0.6, w_peer: float = 0.4) -> tuple[Optional[float], Optional[float]]:
    """[Dyn X2, X5] m_usd = d usd_corr_60 usd_score; score = 0.6 kappa m_usd + 0.4 m_peer. Returns (m_usd, score).
    Check: long GBPUSD, corr -0.75, usd_score -0.50, confirmed, one peer EURUSD (0.80, 0.60) -> 0.375, 0.465."""
    if usd_corr_60 is None or usd_score is None:
        return None, None
    m_usd = d * usd_corr_60 * usd_score
    return m_usd, w_usd * kappa * m_usd + w_peer * m_peer


def peer_momentum(d: int, corrs: dict[str, Optional[float]], scores: dict[str, Optional[float]], min_corr: float = 0.5) -> tuple[float, list[str]]:
    """[Dyn X5] m_peer = sum corr d score / sum |corr| over peers with |corr| >= 0.50; no peers -> 0."""
    num = den = 0.0
    peers = []
    for sym, rho in corrs.items():
        s = scores.get(sym)
        if rho is None or s is None or abs(rho) < min_corr:
            continue
        num += rho * d * s
        den += abs(rho)
        peers.append(sym)
    return (num / den if den > 0 else 0.0), peers


def series_structure(values, noise_ref_d: Optional[float], dcfg: dict, high=None, low=None) -> dict:
    """[Dyn X1] S1 and S2 on a Daily series. Close only (the basket U): Close as High and Low and the Wilder
    average of |dU| as ATR. With High and Low (DXY): the ordinary ATR14."""
    v = np.asarray(values, float)
    ncfg, scfg = dcfg["noise"], dcfg["swing"]
    theta, flag = theta_for(noise_of(v, ncfg["er_bars"], ncfg["window"]), noise_ref_d, scfg)
    if high is None:
        a = close_only_atr(v, 14)
        high = low = v
    else:
        a = atr(pd.DataFrame({"high": high, "low": low, "close": v}), 14)
    sw = vol_swings(high, low, v, a, theta, int(scfg["min_gap_bars"]))
    s2 = structure_score(sw, a[-1] if len(a) else None, float(dcfg["structure_score"]["atr_scale"]))
    return {"theta": theta, "flag": flag, "n": len(v), **s2}


# =============================================================================
# Section 7: plug in  [Dyn 7]
# =============================================================================

@dataclass
class Hooks:
    """v1.0 helpers the layer borrows from the scanner (never redefined here)."""
    find_pivots: Callable
    alternating_upto: Callable
    weekly_from_daily: Callable
    in_roll_window: Callable
    nearest_level: Callable


@dataclass
class InstrumentData:
    inst: Any                       # scanner.Instrument
    bars: dict                      # D (scoring window), D_full, 4H, 2H
    rows: list                      # scanner.Row for long and short; [] when the instrument errored
    v10_structure: dict = field(default_factory=dict)   # {"4H": ..., "D": ..., "W": ...} as the scanner read them


DYN_CSV_KEYS = [
    "dyn_theta_4h", "dyn_theta_d", "dyn_structure_score_4h", "dyn_structure_state_4h", "dyn_structure_agrees_4h",
    "dyn_structure_score_d", "dyn_structure_state_d", "dyn_structure_agrees_d", "dyn_structure_score_w",
    "dyn_structure_state_w", "dyn_structure_agrees_w", "dyn_clarity_4h", "dyn_tf_mode", "dyn_c1_noisy",
    "dyn_pullback_depth", "dyn_pullback_rank", "dyn_pullback_zone", "dyn_nearest_level", "dyn_reach", "dyn_hold",
    "dyn_break_depth", "dyn_break_depth_min", "dyn_psych_cross", "dyn_stretch", "dyn_stretch_rank", "dyn_stretch_band",
    "dyn_coil_ratio", "dyn_condition", "dyn_impulse_min_atr", "dyn_impulse_min_eff", "dyn_zone_frac", "dyn_stop_buffer",
    "dyn_ladder_expiry", "dyn_impulse_pass_v10", "dyn_impulse_would_pass", "dyn_hw_fixed", "dyn_c2_fixed",
    "dyn_hw_v10", "dyn_c2_v10", "dyn_hw_alt", "dyn_c2_alt", "dyn_hw_dyn", "dyn_c2_dyn", "dyn_level_distance",
    "dyn_usd_score", "dyn_dxy_score", "dyn_breadth", "dyn_m_usd", "dyn_m_peer", "dyn_context_score",
    "dyn_context_points", "dyn_vol_class", "dyn_energy_rel", "dyn_usd_corr_60", "dyn_thin", "dyn_flags",
]


def group_key(inst) -> str:
    if inst.asset in ("gold", "spx", "oil"):
        return inst.asset
    return "fx_cross" if inst.group == "FX cross" else "fx_major"


def week_anchor(asof: pd.Timestamp) -> str:
    """The Saturday on or before the decision date: traits refresh on the first run after the Saturday COT pull."""
    d = asof.tz_convert("UTC").date() if asof.tzinfo else asof.date()
    sat = d - pd.Timedelta(days=(d.weekday() - 5) % 7).to_pytimedelta()
    return str(sat)


def find_trait_file(state_dir: Path, anchor: str, config_version: str) -> Optional[tuple[Path, dict]]:
    for p in sorted(state_dir.glob("personality_*.json"), reverse=True) if state_dir.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("week_anchor") == anchor and d.get("config_version") == config_version and d.get("dyn_version") == DYN_VERSION:
            return p, d
    return None


def save_trait_file(state_dir: Path, asof: pd.Timestamp, payload: dict) -> Path:
    """data/personality/personality_<YYYY-MM-DD>.json, never overwritten [Dyn 7.3]."""
    state_dir.mkdir(parents=True, exist_ok=True)
    stem = f"personality_{asof.strftime('%Y-%m-%d')}"
    p = state_dir / f"{stem}.json"
    if p.exists():
        p = state_dir / f"{stem}_{asof.strftime('%H%M')}.json"
    p.write_text(json.dumps(clean(payload), indent=1, default=str), encoding="utf-8")
    return p


def load_tf_modes(state_dir: Path) -> dict[str, str]:
    p = state_dir / "dyn_tf_mode.json"
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")).get("modes", {})) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_tf_modes(state_dir: Path, modes: dict[str, str], asof: pd.Timestamp) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dyn_tf_mode.json").write_text(json.dumps({"updated_utc": str(asof), "modes": modes}, indent=1), encoding="utf-8")


def build_traits(cfg: dict, dcfg: dict, data: list[InstrumentData], hooks: Hooks, basket: Optional[pd.DataFrame],
                 basket_missing: list[str], asof: pd.Timestamp) -> dict:
    """[Dyn 3] The weekly trait file payload for every instrument that has bars."""
    majors = list(cfg["fx"]["majors"])
    w = int(cfg["features"]["pivot_width"])
    icfg = cfg["features"]["impulse"]
    bars: dict[str, SymBars] = {}
    counts = {}
    for d in data:
        if not d.rows or TF_D_FULL not in d.bars:
            continue
        dfd, df4 = d.bars[TF_D_FULL], d.bars[TF_4H]
        seq4 = hooks.alternating_upto(hooks.find_pivots(df4, w), len(df4) - 1)
        bars[d.inst.symbol] = SymBars(d.inst.symbol, group_key(d.inst), d.inst.underlying, d.inst.roll, dfd, df4, seq4)
        counts[d.inst.symbol] = {"daily": len(dfd), "h4": len(df4), "weekly": len(hooks.weekly_from_daily(dfd))}
        LOG.info("dynamics: %s returned %d Daily bars (%d Weekly, %d 4H)", d.inst.symbol, len(dfd),
                 counts[d.inst.symbol]["weekly"], len(df4))
    raw, refs = raw_traits(bars, basket, majors, icfg, dcfg)
    traits = summarize_traits(raw, bars, refs, dcfg, cfg.get("roll_rules", {}), hooks.in_roll_window, majors)
    pull, win = int(dcfg["daily_pull_bars"]), int(dcfg["percentile_window"])
    for sym, c in counts.items():
        fl = traits[sym]["flags"]
        if c["daily"] < pull:
            fl.append(f"{sym}: daily history short ({c['daily']} of {pull} bars)")
        if c["daily"] < win:
            fl.append(f"{sym}: percentile window not filled ({c['daily']} of {win} Daily bars)")
        if c["weekly"] < int(dcfg["weekly_min_bars"]):
            fl.append(f"{sym}: noisy mode unavailable ({c['weekly']} of {dcfg['weekly_min_bars']} Weekly bars)")
    try:
        stab = stability(bars, basket, majors, icfg, dcfg)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("dynamics: stability check failed: %s", exc)
        stab = {"error": str(exc)}
    return {"dyn_version": DYN_VERSION, "config_version": cfg["config_version"], "asof_utc": str(asof),
            "week_anchor": week_anchor(asof), "refs": refs, "basket_missing": basket_missing,
            "bar_counts": counts, "instruments": traits, "stability": stab}


def instrument_readings(d: InstrumentData, tr: Optional[dict], hooks: Hooks, cfg: dict, dcfg: dict,
                        modes: dict[str, str]) -> tuple[dict, dict[str, dict]]:
    """[Dyn 4, 5] Readings on one instrument's own bars: (per instrument fields, per direction fields)."""
    inst, bars = d.inst, d.bars
    dfd, df4 = bars[TF_D_FULL], bars[TF_4H]
    scfg, ccfg, rcfg = dcfg["swing"], dcfg["clarity"], dcfg["retracement"]
    levels = [float(x) for x in rcfg["levels"]]
    row0 = d.rows[0]
    flags: list[str] = []
    out: dict[str, Any] = {}
    if tr is None:
        flags.append("no weekly traits for this instrument: theta base used")
        theta4 = theta_d = float(scfg["theta_base"])
        tr = {}
    else:
        theta4 = tr.get("theta_4h") or float(scfg["theta_base"])
        theta_d = tr.get("theta_d") or float(scfg["theta_base"])
    out["dyn_theta_4h"], out["dyn_theta_d"] = theta4, theta_d
    # S1 and S2 on 4H, Daily, Weekly
    wk = hooks.weekly_from_daily(dfd)
    swings: dict[str, list[Swing]] = {}
    scale = float(dcfg["structure_score"]["atr_scale"])
    for tf, df, th in ((TF_4H, df4, theta4), (TF_D, dfd, theta_d), (TF_W, wk, theta_d)):
        key = tf.lower()
        if len(df) < 20:
            swings[tf] = []
            out[f"dyn_structure_score_{key}"], out[f"dyn_structure_state_{key}"] = None, UNAVAILABLE
            out[f"dyn_structure_agrees_{key}"] = None
            flags.append(f"{tf}: too few bars for swings")
            continue
        a = atr(df, 14)
        sw = vol_swings(df["high"].values, df["low"].values, df["close"].values, a, th, int(scfg["min_gap_bars"]))
        swings[tf] = sw
        s2 = structure_score(sw, a[-1], scale)
        out[f"dyn_structure_score_{key}"], out[f"dyn_structure_state_{key}"] = s2["score"], s2["state"]
        v10 = d.v10_structure.get(tf)
        out[f"dyn_structure_agrees_{key}"] = None if (v10 is None or s2["state"] == UNAVAILABLE) else bool(s2["state"] == v10)
        out[f"dyn_swings_{key}"] = [{"kind": s.kind, "price": s.price, "time": str(df.index[s.k]),
                                     "confirm_time": str(df.index[s.confirm])} for s in sw[-6:]]
    # S3 timeframe ladder
    a4 = atr(df4, 14)
    legs = swing_legs(swings.get(TF_4H, []), df4["close"].values, a4, df4.index, int(ccfg["legs"]))
    clarity = med([x["eff"] for x in legs]) if len(legs) >= int(ccfg["legs"]) else None
    out["dyn_clarity_4h"] = clarity
    weekly_ok = len(wk) >= int(dcfg["weekly_min_bars"])
    prev = modes.get(inst.symbol)
    mode = tf_mode_next(prev, clarity, float(ccfg["noisy_below"]), float(ccfg["normal_above"]))
    modes[inst.symbol] = mode
    out["dyn_tf_mode"] = mode if weekly_ok else "noisy mode unavailable"
    if not weekly_ok:
        flags.append(f"noisy mode unavailable ({len(wk)} of {dcfg['weekly_min_bars']} Weekly bars)")
    # S5 break significance, S6 maturity, S7 coil (per instrument, bias based)
    ad = atr(dfd, 14)
    cd = dfd["close"].values.astype(float)
    brk_min = (tr.get("breaks") or {}).get("break_depth_min")
    out.update(break_significance(row0.daily_bias, row0.bias_invalidation, float(cd[-1]), float(cd[-2]) if len(cd) > 1 else None,
                                  float(ad[-1]) if len(ad) else float("nan"), float(inst.grid_major), brk_min))
    if out["dyn_psych_cross"]:
        flags.append("psych level close against bias")
    zone_hw = row0.zone_half_width if row0.zone_half_width is not None else 0.0
    m = maturity(dfd, row0.daily_bias, float(inst.grid_mid), float(zone_hw), dcfg["maturity"],
                 int(dcfg["percentile_window"]), int(dcfg["percentile_min_values"]))
    mflags = m.pop("flags")
    out.update(m)
    c = coil(dfd, dcfg["coil"])
    cflags = c.pop("flags")
    out.update(c)
    cond = mflags + cflags
    out["dyn_condition"] = ", ".join(cond) if cond else "clear"
    flags += cond
    # Section 5: scaled parameters and the zone widths
    sp = tr.get("scaled") or {}
    for k in ("dyn_impulse_min_atr", "dyn_impulse_min_eff", "dyn_zone_frac", "dyn_stop_buffer", "dyn_ladder_expiry"):
        out[k] = sp.get(k)
    fcfg = cfg["features"]
    two_ticks = int(fcfg["zone_min_ticks"]) * float(inst.tick)
    a4_now = float(a4[-1]) if len(a4) and np.isfinite(a4[-1]) else 0.0
    ad20 = wilder(true_range(dfd), int(dcfg["zone_alt"]["atr_period_daily"]))
    ad20_now = float(ad20[-1]) if len(ad20) and np.isfinite(ad20[-1]) else 0.0
    P = float(row0.ref_price)
    lvl, _kind = hooks.nearest_level(inst, P)
    dist = abs(P - lvl)
    widths = {"fixed": row0.zone_half_width, "v10": max(two_ticks, float(fcfg["zone_atr_fraction"]) * a4_now),
              "alt": max(two_ticks, float(dcfg["zone_alt"]["fraction"]) * ad20_now),
              "dyn": None if sp.get("dyn_zone_frac") is None else max(two_ticks, float(sp["dyn_zone_frac"]) * a4_now)}
    out["dyn_level_distance"] = dist
    for name, hw in widths.items():
        out[f"dyn_hw_{name}"] = hw
        out[f"dyn_c2_{name}"] = (bool(row0.c2) if name == "fixed" else None if hw is None else bool(dist <= hw + 1e-12))
        out[f"dyn_pass_est_{name}"] = None if hw is None else min(1.0, 2 * hw / float(inst.grid_mid))
    # traits carried for the row
    out["dyn_vol_class"] = tr.get("vol_class", UNAVAILABLE)
    out["dyn_energy_rel"] = tr.get("energy_rel")
    out["dyn_usd_corr_60"] = tr.get("usd_corr_60")
    out["dyn_thin"] = bool(tr.get("thin", True))
    # per direction: S3 noisy C1, S4 pullback, T1/T2 would pass
    w = int(fcfg["pivot_width"])
    seq4 = hooks.alternating_upto(hooks.find_pivots(df4, w), len(df4) - 1)
    icfg = fcfg["impulse"]
    ret = tr.get("retracement") or {}
    reach = {k[len("reach_"):]: v for k, v in ret.items() if k.startswith("reach_")}
    hold = {k[len("hold_"):]: v for k, v in ret.items() if k.startswith("hold_")}
    by_dir: dict[str, dict] = {}
    for r in d.rows:
        dd: dict[str, Any] = {}
        dd["dyn_c1_noisy"] = (bool(out.get("dyn_structure_state_w") == r.direction and out.get("dyn_structure_state_d") == r.direction)
                              if mode == "noisy" and weekly_ok else None)
        if r.impulse:
            dd.update(pullback_location(float(r.impulse["A"]), float(r.impulse["B"]), P, ret.get("rho_continued") or [], levels, reach, hold))
        else:
            dd.update({"dyn_pullback_depth": None, "dyn_pullback_rank": None, "dyn_pullback_zone": "no impulse",
                       "dyn_nearest_level": None, "dyn_reach": None, "dyn_hold": None})
        dd["dyn_impulse_pass_v10"] = impulse_passes(seq4, df4["close"].values, a4, r.direction, icfg,
                                                    float(icfg["min_atr_mult"]), float(icfg["min_efficiency"]), len(df4) - 1)
        t1, t2 = sp.get("dyn_impulse_min_atr"), sp.get("dyn_impulse_min_eff")
        dd["dyn_impulse_would_pass"] = (None if (t1 is None or t2 is None) else
                                        impulse_passes(seq4, df4["close"].values, a4, r.direction, icfg, float(t1), float(t2), len(df4) - 1))
        by_dir[r.direction] = dd
    out["flags"] = flags
    return out, by_dir


def context_all(data: list[InstrumentData], inst_fields: dict[str, dict], traits: dict[str, dict], basket: Optional[pd.DataFrame],
                basket_missing: list[str], dxy_daily: Optional[pd.DataFrame], cfg: dict, dcfg: dict) -> tuple[dict, dict[str, dict[str, dict]]]:
    """[Dyn 6] Dollar read, breadth, correlation breaks and context points. Returns (global fields, per symbol per
    direction fields). Any missing major makes the whole section unavailable."""
    ccfg = dcfg["context"]
    majors = list(cfg["fx"]["majors"])
    glob: dict[str, Any] = {"dyn_usd_score": None, "dyn_dxy_score": None, "dyn_breadth": None, "dyn_breadth_2": None,
                            "dyn_kappa": None, "flags": [], "links": {}}
    per: dict[str, dict[str, dict]] = {}
    scored = {d.inst.symbol: d for d in data if d.rows}
    missing = list(basket_missing) + [m for m in majors if m not in inst_fields or inst_fields[m].get("dyn_structure_score_d") is None]
    unavailable = {"dyn_m_usd": None, "dyn_m_peer": None, "dyn_context_score": None, "dyn_context_points": None, "dyn_peers": []}
    if basket is None or missing:
        glob["flags"].append(f"context unavailable: majors missing ({', '.join(dict.fromkeys(missing))})")
        for sym, d in scored.items():
            per[sym] = {r.direction: dict(unavailable) for r in d.rows}
        return glob, per
    noise_ref_d = (traits.get(majors[0]) or {}).get("_refs", {}).get("noise_d")
    u = series_structure(basket["U"].values, noise_ref_d, dcfg)
    glob["dyn_usd_score"], glob["dyn_usd_theta"] = u["score"], u["theta"]
    if dxy_daily is not None and len(dxy_daily) >= 60:
        x = series_structure(dxy_daily["close"].values, noise_ref_d, dcfg, dxy_daily["high"].values, dxy_daily["low"].values)
        glob["dyn_dxy_score"] = x["score"]
        if (u["score"] is not None and x["score"] is not None and np.sign(u["score"]) != np.sign(x["score"])
                and abs(u["score"]) > float(ccfg["disagree_min"]) and abs(x["score"]) > float(ccfg["disagree_min"])):
            glob["flags"].append("dollar reads disagree")
    else:
        glob["flags"].append("DXY unavailable")
    q = {m: (1.0 if m.startswith("USD") else -1.0) for m in majors}
    s2d = {sym: f.get("dyn_structure_score_d") for sym, f in inst_fields.items()}
    glob["dyn_breadth"] = sum(q[m] * s2d[m] for m in majors) / len(majors)
    if s2d.get("EURUSD") is not None and s2d.get("GBPUSD") is not None:
        glob["dyn_breadth_2"] = -(s2d["EURUSD"] + s2d["GBPUSD"]) / 2
    usd = glob["dyn_usd_score"]
    confirmed = (usd is not None and np.sign(glob["dyn_breadth"]) == np.sign(usd) and abs(glob["dyn_breadth"]) >= float(ccfg["breadth_min"]))
    glob["dyn_kappa"] = 1.0 if confirmed else 0.5
    # Daily log returns by trade date for every scored instrument; "USD" = the basket
    rets = {}
    for sym, d in scored.items():
        cbd = close_by_date(d.bars[TF_D_FULL])
        rets[sym] = pd.Series(log_returns(cbd.values), index=cbd.index)
    rets["USD"] = basket["r_usd"]
    conflict: set[str] = set()
    for a, b in ccfg["links"]:
        if a in rets and b in rets:
            res = corr_break(rets[a], rets[b], int(ccfg["corr_short"]), int(ccfg["corr_long"]), float(ccfg["corr_break_z"]), float(ccfg["corr_break_rho"]))
            glob["links"][f"{a}-{b}"] = res
            if res["broken"]:
                conflict.update(x for x in (a, b) if x != "USD")
    frame = pd.DataFrame({s: r for s, r in rets.items() if s != "USD"}).iloc[-int(ccfg["corr_long"]):]
    corr = frame.corr(min_periods=int(0.8 * int(ccfg["corr_long"])))
    for sym, d in scored.items():
        und = d.inst.underlying
        corrs = {k: (None if pd.isna(v) else float(v)) for k, v in corr[sym].items()
                 if k != sym and not (und and scored[k].inst.underlying == und)}
        tr = traits.get(sym) or {}
        per[sym] = {}
        for r in d.rows:
            dsign = 1 if r.direction == LONG else -1
            m_peer, peers = peer_momentum(dsign, corrs, s2d, float(ccfg["peer_corr_min"]))
            m_usd, score = context_score(dsign, tr.get("usd_corr_60"), usd, glob["dyn_kappa"], m_peer,
                                         float(ccfg["usd_weight"]), float(ccfg["peer_weight"]))
            per[sym][r.direction] = {"dyn_m_usd": m_usd, "dyn_m_peer": m_peer, "dyn_context_score": score,
                                     "dyn_context_points": context_points(score, ccfg["points"], sym in conflict),
                                     "dyn_peers": peers, "dyn_context_conflict": sym in conflict}
    return glob, per


def apply_layer(cfg: dict, base: Path, out_dir: Path, asof: pd.Timestamp, run_type: str, data: list[InstrumentData],
                dxy_daily: Optional[pd.DataFrame], demo: bool, hooks: Hooks, meta: dict) -> dict:
    """[Dyn 7.1] Called once per scan after the v1.0 scoring, ranking and footer are complete. Attaches the dyn_
    fields to every row, refreshes or reuses the weekly trait file, appends the comparison log, and returns a
    summary for the dashboard header. Never changes a pre existing field."""
    dcfg = dyn_cfg(cfg)
    state_dir = (out_dir / "personality") if demo else (base / cfg["paths"]["data_dir"] / "personality")
    majors = list(cfg["fx"]["majors"])
    daily = {d.inst.symbol: d.bars.get(TF_D_FULL) for d in data if d.rows}
    basket, basket_missing = usd_basket(daily, majors)
    # weekly traits [Dyn 2 cadence]
    anchor = week_anchor(asof)
    found = None if (demo or dcfg.get("trait_refresh") == "always") else find_trait_file(state_dir, anchor, cfg["config_version"])
    if found:
        trait_path, payload = found
        refreshed = False
    else:
        payload = build_traits(cfg, dcfg, data, hooks, basket, basket_missing, asof)
        trait_path = save_trait_file(state_dir, asof, payload)
        refreshed = True
    traits: dict[str, dict] = dict(payload.get("instruments", {}))
    for t in traits.values():
        t["_refs"] = payload.get("refs", {})
    # per instrument readings [Dyn 4, 5]
    modes = load_tf_modes(state_dir)
    inst_fields: dict[str, dict] = {}
    dir_fields: dict[str, dict[str, dict]] = {}
    for d in data:
        if not d.rows or TF_D_FULL not in d.bars:
            continue
        try:
            f, by_dir = instrument_readings(d, traits.get(d.inst.symbol), hooks, cfg, dcfg, modes)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("dynamics: readings failed for %s", d.inst.symbol)
            f, by_dir = {"flags": [f"readings failed: {exc}"]}, {r.direction: {} for r in d.rows}
        inst_fields[d.inst.symbol], dir_fields[d.inst.symbol] = f, by_dir
    save_tf_modes(state_dir, modes, asof)
    # context [Dyn 6]
    try:
        glob, ctx = context_all(data, inst_fields, traits, basket, basket_missing, dxy_daily, cfg, dcfg)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("dynamics: context failed")
        glob, ctx = {"flags": [f"context failed: {exc}"], "dyn_usd_score": None, "dyn_dxy_score": None, "dyn_breadth": None}, {}
    # attach [Dyn 7.3]
    for d in data:
        f = inst_fields.get(d.inst.symbol)
        if f is None:
            continue
        for r in d.rows:
            fields = {k: v for k, v in f.items() if k != "flags"}
            fields.update(dir_fields[d.inst.symbol].get(r.direction, {}))
            fields.update(ctx.get(d.inst.symbol, {}).get(r.direction, {}))
            fields["dyn_usd_score"] = glob.get("dyn_usd_score")
            fields["dyn_dxy_score"] = glob.get("dyn_dxy_score")
            fields["dyn_breadth"] = glob.get("dyn_breadth")
            fl = list(f.get("flags", [])) + list(glob.get("flags", []))
            if fields.get("dyn_context_conflict"):
                fl.append("context conflict")
            fields["dyn_flags"] = "; ".join(dict.fromkeys(fl))
            r.dynamics = clean(fields)
    append_compare_log(base / cfg["paths"]["log_dir"] if not demo else out_dir, meta, data)
    thin = sum(1 for t in {id(t): t for t in traits.values()}.values() if t.get("thin"))
    summary = {"enabled": True, "dyn_version": DYN_VERSION, "trait_file": trait_path.name, "trait_asof": payload.get("asof_utc"),
               "traits_refreshed": refreshed, "instruments_with_traits": len(traits), "thin_trait_sets": thin,
               "context": "unavailable" if glob.get("dyn_usd_score") is None else "ok",
               "usd_score": glob.get("dyn_usd_score"), "dxy_score": glob.get("dyn_dxy_score"), "breadth": glob.get("dyn_breadth"),
               "flags": glob.get("flags", []), "links": glob.get("links", {})}
    meta["dynamics"] = clean(summary)
    meta["freshness"]["Dynamics"] = (f"v{DYN_VERSION} logged only; traits {trait_path.name} ({'refreshed' if refreshed else 'reused'}), "
                                     f"{thin} thin; context {summary['context']}"
                                     + (f"; {'; '.join(summary['flags'])}" if summary["flags"] else ""))
    return summary


COMPARE_COLUMNS = ["run_utc", "run_type", "config_version", "symbol", "v10_structure_4h", "dyn_state_4h", "agrees_4h",
                   "v10_structure_d", "dyn_state_d", "agrees_d", "v10_structure_w", "dyn_state_w", "agrees_w", "tf_mode",
                   "clarity_4h", "long_impulse_v10", "long_impulse_dyn", "short_impulse_v10", "short_impulse_dyn",
                   "hw_fixed", "c2_fixed", "hw_v10", "c2_v10", "hw_alt", "c2_alt", "hw_dyn", "c2_dyn", "level_distance",
                   "context_points_long", "context_points_short", "condition", "flags"]


def append_compare_log(log_dir: Path, meta: dict, data: list[InstrumentData]) -> Optional[Path]:
    """logs/dynamics_compare.csv: one line per instrument per scan [Dyn 7.3]."""
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "dynamics_compare.csv"
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        if new:
            wr.writerow(COMPARE_COLUMNS)
        for d in data:
            if not d.rows or not d.rows[0].dynamics:
                continue
            by = {r.direction: r.dynamics for r in d.rows}
            f = by.get(LONG) or next(iter(by.values()))
            s = by.get(SHORT, {})
            wr.writerow([meta["asof_utc"], meta["run_type"], meta["config_version"], d.inst.symbol,
                         d.v10_structure.get(TF_4H), f.get("dyn_structure_state_4h"), f.get("dyn_structure_agrees_4h"),
                         d.v10_structure.get(TF_D), f.get("dyn_structure_state_d"), f.get("dyn_structure_agrees_d"),
                         d.v10_structure.get(TF_W), f.get("dyn_structure_state_w"), f.get("dyn_structure_agrees_w"),
                         f.get("dyn_tf_mode"), f.get("dyn_clarity_4h"), f.get("dyn_impulse_pass_v10"), f.get("dyn_impulse_would_pass"),
                         s.get("dyn_impulse_pass_v10"), s.get("dyn_impulse_would_pass"),
                         f.get("dyn_hw_fixed"), f.get("dyn_c2_fixed"), f.get("dyn_hw_v10"), f.get("dyn_c2_v10"),
                         f.get("dyn_hw_alt"), f.get("dyn_c2_alt"), f.get("dyn_hw_dyn"), f.get("dyn_c2_dyn"), f.get("dyn_level_distance"),
                         f.get("dyn_context_points"), s.get("dyn_context_points"), f.get("dyn_condition"), f.get("dyn_flags")])
    return p


C2_EXTRA_COLUMNS = ["hw_fixed", "pass_est_fixed", "hw_v10", "c2_v10", "pass_est_v10", "hw_alt", "c2_alt", "pass_est_alt",
                    "hw_dyn", "c2_dyn", "pass_est_dyn"]


def c2_extra_values(dyn: dict) -> list:
    """The T4 columns appended to logs/c2_rejections.csv for one instrument [Dyn T4]."""
    if not dyn:
        return [None] * len(C2_EXTRA_COLUMNS)
    return [dyn.get("dyn_hw_fixed"), dyn.get("dyn_pass_est_fixed"), dyn.get("dyn_hw_v10"), dyn.get("dyn_c2_v10"), dyn.get("dyn_pass_est_v10"),
            dyn.get("dyn_hw_alt"), dyn.get("dyn_c2_alt"), dyn.get("dyn_pass_est_alt"), dyn.get("dyn_hw_dyn"), dyn.get("dyn_c2_dyn"),
            dyn.get("dyn_pass_est_dyn")]


def csv_values(dyn: dict) -> list:
    return [dyn.get(k) for k in DYN_CSV_KEYS] if dyn else [None] * len(DYN_CSV_KEYS)


def _f(v, nd: int = 2) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:+.{nd}f}"
    return str(v)


def html_block(dyn: dict, esc: Callable[[str], str]) -> str:
    """[Dyn 7.3] Four short lines for one top setup. The scanner wraps this in a cell."""
    if not dyn:
        return "<span class='muted'>–</span>"
    agrees = dyn.get("dyn_structure_agrees_4h")
    trend = (f"4H {_f(dyn.get('dyn_structure_score_4h'))} {esc(str(dyn.get('dyn_structure_state_4h', '–')))} · "
             f"D {_f(dyn.get('dyn_structure_score_d'))} {esc(str(dyn.get('dyn_structure_state_d', '–')))} · "
             f"W {_f(dyn.get('dyn_structure_score_w'))} · mode {esc(str(dyn.get('dyn_tf_mode', '–')))} · "
             f"agrees with v1.0 (4H): {_f(agrees)}")
    loc = esc(str(dyn.get("dyn_pullback_zone", "–")))
    if dyn.get("dyn_pullback_depth") is not None:
        loc += f" (depth {_f(dyn.get('dyn_pullback_depth'))}, rank {_f(dyn.get('dyn_pullback_rank'))})"
    if dyn.get("dyn_nearest_level") is not None:
        loc += f" · nearest {dyn['dyn_nearest_level']:.3f}: reach {_f(dyn.get('dyn_reach'))} hold {_f(dyn.get('dyn_hold'))}"
    cond = (f"{esc(str(dyn.get('dyn_condition', '–')))} · stretch rank {_f(dyn.get('dyn_stretch_rank'))} · "
            f"coil {_f(dyn.get('dyn_coil_ratio'))}")
    pts = dyn.get("dyn_context_points")
    cflags = [x for x in str(dyn.get("dyn_flags", "")).split("; ")
              if x in ("context conflict", "dollar reads disagree") or x.startswith("context unavailable")]
    ctx = (f"points {'–' if pts is None else f'{pts:+d}'} (score {_f(dyn.get('dyn_context_score'))}, "
           f"dollar {_f(dyn.get('dyn_usd_score'))})" + (f" · {esc('; '.join(cflags))}" if cflags else ""))
    return ("<span class='muted'>Dynamic layer: logged only, does not affect rank</span>"
            f"<br><b>Trend</b> {trend}<br><b>Location</b> {loc}<br><b>Condition</b> {cond}<br><b>Context</b> {ctx}")
