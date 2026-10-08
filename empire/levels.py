"""Level engine: the 20 year price map.

Two families of levels are scored side by side from day one: round number zones and levels the market
made itself (yearly and quarterly extremes, major swing points). Each is tested against randomly placed
levels under identical rules, and a zone is trusted only when it beats chance.

Rules (doc defaults, all in personality_config.yaml levels:)
    Touch     price enters the zone after at least 5 daily bars outside it
    Reaction  after a touch, price exits on the side it came from and travels at least 1 ATR within 10 bars
    Break     price closes beyond the far side of the zone for 2 bars
    Respect   p = (R + 1) / (N + 2);  belief p ~ Beta(1 + R, 1 + N - R)
    Edge      Z = (p - p0) / sqrt(p0 (1 - p0) / N), p0 from random non-round levels
    Strength  S = sum |dP_k| / ATR_k * exp(-(t - t_k) / tau)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class Touch:
    i: int
    date: pd.Timestamp
    side: str          # above | below (where price came from)
    outcome: str       # reaction | break | none
    size_atr: float
    regime: Optional[str] = None


@dataclass
class Zone:
    level: float
    family: str                     # round_major | round_mid | market | pain | random
    half_width: float
    touches: list = field(default_factory=list)
    label: str = ""
    player: Optional[str] = None
    evidence: Optional[str] = None
    source: Optional[str] = None
    born_i: int = 0                 # first bar on which the level was known (point in time)

    @property
    def n(self) -> int:
        return len(self.touches)

    @property
    def r(self) -> int:
        return sum(t.outcome == "reaction" for t in self.touches)

    @property
    def breaks(self) -> int:
        return sum(t.outcome == "break" for t in self.touches)

    @property
    def respect(self) -> float:
        return (self.r + 1) / (self.n + 2)

    def respect_in(self, regime: Optional[str], min_touches: int) -> tuple[float, str]:
        if regime:
            ts = [t for t in self.touches if t.regime == regime]
            if len(ts) >= min_touches:
                r = sum(t.outcome == "reaction" for t in ts)
                return (r + 1) / (len(ts) + 2), f"regime {regime} ({len(ts)} touches)"
        return self.respect, f"all regimes ({self.n} touches)"

    def beta_interval(self, z: float = 1.645) -> tuple[float, float]:
        a, b = 1 + self.r, 1 + self.n - self.r
        m = a / (a + b)
        sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        return max(0.0, m - z * sd), min(1.0, m + z * sd)

    def edge_z(self, p0: Optional[float]) -> Optional[float]:
        if p0 is None or self.n == 0 or not 0 < p0 < 1:
            return None
        return (self.respect - p0) / math.sqrt(p0 * (1 - p0) / self.n)

    def strength(self, now_i: int, tau_days: float) -> float:
        return float(sum(t.size_atr * math.exp(-(now_i - t.i) / tau_days) for t in self.touches if t.outcome == "reaction"))


# =============================================================================
# Building levels
# =============================================================================

def half_width(atr_now: float, w_min: float, lam: float) -> float:
    return max(float(w_min), lam * float(atr_now)) if np.isfinite(atr_now) else float(w_min)


def round_levels(lo: float, hi: float, major: float, mid: float) -> list[tuple[float, str]]:
    out = []
    start = math.floor(lo / mid) * mid
    k = 0
    while True:
        lvl = round(start + k * mid, 10)
        if lvl > hi + mid:
            break
        if lvl > 0 or lo <= 0:
            is_major = abs(lvl / major - round(lvl / major)) < 1e-6
            out.append((lvl, "round_major" if is_major else "round_mid"))
        k += 1
    return out


def market_levels(bars: pd.DataFrame, pivot_width: int, merge_within: float) -> list[tuple[float, str, pd.Timestamp]]:
    """Completed yearly and quarterly highs and lows, plus major swing points, each with the date it became
    known: the day after its period ended, or pivot_width bars after a swing. Close levels are merged and
    the merged level is known from its earliest member."""
    cand: list[tuple[float, str, pd.Timestamp]] = []
    idx = bars.index
    last = idx[-1]
    for freq, lab in (("YE", "yearly"), ("QE", "quarterly")):
        try:
            g = bars.resample(freq)
        except ValueError:  # pandas < 2.2 aliases
            g = bars.resample(freq[0])
        hi, lo = g["high"].max(), g["low"].min()
        for t in hi.index:
            if t >= last:      # the period still running is not complete
                continue
            known = idx[idx > t]
            if known.empty:
                continue
            per = f"{t.year}" + (f"Q{t.quarter}" if lab == "quarterly" else "")
            if pd.notna(hi[t]):
                cand.append((float(hi[t]), f"{lab} high {per}", known[0]))
            if pd.notna(lo[t]):
                cand.append((float(lo[t]), f"{lab} low {per}", known[0]))
    h, l = bars["high"].values, bars["low"].values
    w = pivot_width
    for i in range(w, len(bars) - w):
        if h[i] == h[i - w:i + w + 1].max():
            cand.append((float(h[i]), f"swing high {idx[i].date()}", idx[i + w]))
        if l[i] == l[i - w:i + w + 1].min():
            cand.append((float(l[i]), f"swing low {idx[i].date()}", idx[i + w]))
    cand.sort(key=lambda x: x[0])
    merged: list[tuple[float, str, pd.Timestamp]] = []
    group: list[tuple[float, str, pd.Timestamp]] = []
    for lv in cand:
        if group and lv[0] - group[0][0] > merge_within:
            merged.append(_merge(group))
            group = []
        group.append(lv)
    if group:
        merged.append(_merge(group))
    return merged


def _merge(group: list[tuple[float, str, pd.Timestamp]]) -> tuple[float, str, pd.Timestamp]:
    lvl = float(np.mean([g[0] for g in group]))
    first = min(group, key=lambda g: g[2])
    lab = first[1] if len(group) == 1 else f"{first[1]} (+{len(group) - 1} more)"
    return lvl, lab, first[2]


def random_levels(lo: float, hi: float, n: int, avoid: list[float], min_gap: float, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    out: list[float] = []
    av = np.array(sorted(avoid)) if avoid else np.array([])
    tries = 0
    while len(out) < n and tries < n * 50:
        tries += 1
        x = float(rng.uniform(lo, hi))
        if av.size and np.min(np.abs(av - x)) < min_gap:
            continue
        out.append(x)
    return out


# =============================================================================
# Scoring
# =============================================================================

def score_zone(z: Zone, H: np.ndarray, L: np.ndarray, C: np.ndarray, atr: np.ndarray, w: np.ndarray,
               dates: pd.DatetimeIndex, regimes: Optional[np.ndarray], lcfg: dict) -> Zone:
    """Find touches and classify each. Only touches whose outcome window has completed are kept."""
    gap_bars = int(lcfg.get("touch_outside_bars", 5))
    react_bars = int(lcfg.get("reaction_bars", 10))
    react_atr = float(lcfg.get("reaction_atr", 1.0))
    break_closes = int(lcfg.get("break_closes", 2))
    lvl = z.level
    top, bot = lvl + w, lvl - w
    inside = (L <= top) & (H >= bot)
    idx = np.flatnonzero(inside)
    if idx.size == 0:
        return z
    prev = np.concatenate([[-10 ** 9], idx[:-1]])
    entries = idx[((idx - prev - 1) >= gap_bars) & (idx > z.born_i)]
    n = len(C)
    for t in entries:
        if t < 1 or not np.isfinite(atr[t]):
            continue
        if C[t - 1] > top[t - 1]:
            side = "above"
        elif C[t - 1] < bot[t - 1]:
            side = "below"
        else:
            continue
        end = min(n - 1, t + react_bars)
        outcome, size = "none", 0.0
        beyond = 0
        for k in range(t, end + 1):
            if side == "above":
                if H[k] >= top[t] + react_atr * atr[t] and C[k] > top[t]:
                    outcome, size = "reaction", (H[t:k + 1].max() - top[t]) / atr[t]
                    break
                beyond = beyond + 1 if C[k] < bot[t] else 0
            else:
                if L[k] <= bot[t] - react_atr * atr[t] and C[k] < bot[t]:
                    outcome, size = "reaction", (bot[t] - L[t:k + 1].min()) / atr[t]
                    break
                beyond = beyond + 1 if C[k] > top[t] else 0
            if beyond >= break_closes:
                outcome = "break"
                break
        if outcome == "none" and t + react_bars > n - 1:
            continue        # still pending: the outcome window has not finished
        z.touches.append(Touch(int(t), dates[t], side, outcome, float(size),
                               None if regimes is None else (regimes[t] if isinstance(regimes[t], str) else None)))
    return z


@dataclass
class LevelMap:
    symbol: str
    zones: list            # scored real zones (round, market, pain)
    p0: Optional[float]    # random-level baseline respect rate
    random_n: int
    family_edge: dict      # family -> {n, r, rate, z}
    half_width_now: float
    atr_now: float
    vacuum: dict
    price: float
    now_i: int


def family_stats(zones: list[Zone], p0: Optional[float]) -> dict:
    out = {}
    fams = sorted({z.family for z in zones})
    for f in fams:
        zs = [z for z in zones if z.family == f]
        n = sum(z.n for z in zs)
        r = sum(z.r for z in zs)
        rate = r / n if n else None
        zz = None
        if p0 and n and 0 < p0 < 1:
            zz = (rate - p0) / math.sqrt(p0 * (1 - p0) / n)
        out[f] = {"levels": len(zs), "touches": n, "reactions": r, "rate": None if rate is None else round(rate, 3),
                  "z": None if zz is None else round(zz, 2)}
    return out


def build_map(symbol: str, bars: pd.DataFrame, atr: pd.Series, grid: dict, lcfg: dict,
              regimes: Optional[pd.Series], pain: list[dict], seed: int) -> Optional[LevelMap]:
    b = bars.dropna(subset=["close"])
    if len(b) < 300:
        return None
    H, L, C = b["high"].values.astype(float), b["low"].values.astype(float), b["close"].values.astype(float)
    a = atr.reindex(b.index).values.astype(float)
    lam, w_min = float(lcfg.get("atr_multiplier", 0.25)), float(grid.get("w_min", 0.0))
    w = np.where(np.isfinite(a), np.maximum(w_min, lam * a), w_min)
    reg = None if regimes is None else regimes.reindex(b.index).values
    lo, hi = float(np.nanmin(L)), float(np.nanmax(H))
    major, mid = float(grid["major"]), float(grid["mid"])
    zones: list[Zone] = []
    for lvl, fam in round_levels(lo, hi, major, mid):
        zones.append(Zone(lvl, fam, float(w[-1]), label=f"{fam.replace('_', ' ')} {lvl:g}"))
    for lvl, lab, born in market_levels(b, int(lcfg.get("pivot_width", 20)), float(w[-1])):
        zones.append(Zone(lvl, "market", float(w[-1]), label=lab, born_i=int(b.index.searchsorted(born))))
    for p in pain:
        zones.append(Zone(float(p["level"]), "pain", float(w[-1]), label=p.get("label", "pain zone"), player=p.get("player"),
                          evidence=p.get("evidence"), source=p.get("source")))
    dates = b.index
    for z in zones:
        score_zone(z, H, L, C, a, w, dates, reg, lcfg)
    rnd = random_levels(lo, hi, int(lcfg.get("random_levels", 200)),
                        [lv for lv, _ in round_levels(lo, hi, major, mid)], min(2 * float(w[-1]), mid / 4), seed)
    rz = [score_zone(Zone(x, "random", float(w[-1])), H, L, C, a, w, dates, reg, lcfg) for x in rnd]
    rn = sum(z.n for z in rz)
    p0 = (sum(z.r for z in rz) / rn) if rn >= 30 else None
    fam = family_stats(zones, p0)
    fam["random"] = {"levels": len(rz), "touches": rn, "reactions": sum(z.r for z in rz),
                     "rate": None if p0 is None else round(p0, 3), "z": None}
    vac = time_spent(C, float(w[-1]))
    return LevelMap(symbol, zones, p0, len(rz), fam, float(w[-1]), float(a[-1]) if np.isfinite(a[-1]) else float("nan"),
                    vac, float(C[-1]), len(C) - 1)


def time_spent(C: np.ndarray, w: float) -> dict:
    """Share of days spent in each price bin of width 2w; used to find vacuum stretches."""
    if w <= 0:
        return {}
    lo, hi = np.nanmin(C), np.nanmax(C)
    edges = np.arange(lo - w, hi + 2 * w, 2 * w)
    if len(edges) < 3:
        return {}
    hist, _ = np.histogram(C[np.isfinite(C)], bins=edges)
    share = hist / max(hist.sum(), 1)
    nz = share[share > 0]
    return {"edges": edges, "share": share, "median_share": float(np.median(nz)) if nz.size else 0.0}


def vacuum_between(vac: dict, a: float, b: float, ratio: float = 0.5) -> Optional[bool]:
    """True when price spent little time between a and b (mean bin share under ratio x median)."""
    if not vac or a == b:
        return None
    lo, hi = min(a, b), max(a, b)
    e, s = vac["edges"], vac["share"]
    mids = (e[:-1] + e[1:]) / 2
    sel = (mids > lo) & (mids < hi)
    if not sel.any():
        return None
    return bool(s[sel].mean() < ratio * vac["median_share"])


def zone_edge_ok(z: Zone, p0: Optional[float], fam: dict, edge_z: float) -> bool:
    """Proven edge: the zone's lower credible bound beats chance, or its family beats chance by edge_z with
    the zone itself at or above the baseline."""
    if p0 is None or z.n == 0:
        return False
    if z.beta_interval()[0] > p0:
        return True
    fz = (fam.get(z.family) or {}).get("z")
    return bool(fz is not None and fz >= edge_z and z.respect >= p0)


# =============================================================================
# Excursions: touch probability and outcome base rates from the instrument's own history
# =============================================================================

def forward_excursions(bars: pd.DataFrame, sigma: pd.Series, h: int, additive: bool) -> dict:
    """For every past day s with a full h-day future: the running max up and down excursion over k=1..h,
    in units of sigma_s * sqrt(h/252). Arrays have shape (days, h)."""
    b = bars.dropna(subset=["close"])
    C = b["close"].values.astype(float)
    H = b["high"].values.astype(float)
    L = b["low"].values.astype(float)
    sg = sigma.reindex(b.index).values.astype(float)
    n = len(C)
    m = n - h
    if m <= 50:
        return {}
    idx = np.arange(m)[:, None] + np.arange(1, h + 1)[None, :]
    if additive:
        up = H[idx] - C[:m, None]
        dn = C[:m, None] - L[idx]
        unit = sg[:m] * math.sqrt(h / 252)
    else:
        up = np.log(H[idx] / C[:m, None])
        dn = np.log(C[:m, None] / L[idx])
        unit = sg[:m] * math.sqrt(h / 252)
    ok = np.isfinite(unit) & (unit > 0)
    up = np.maximum.accumulate(up[ok], axis=1) / unit[ok, None]
    dn = np.maximum.accumulate(dn[ok], axis=1) / unit[ok, None]
    return {"up": up, "dn": dn, "dates": b.index[:m][ok]}


def touch_probability(exc: dict, u: float) -> Optional[float]:
    """Empirical: share of past days whose h-day excursion (either side, pooled) reached u units."""
    if not exc:
        return None
    m = np.concatenate([exc["up"][:, -1], exc["dn"][:, -1]])
    return float((m >= u).mean())


def touch_probability_formula(u: float) -> float:
    """No-drift random walk baseline: 2 [1 - Phi(u)]."""
    return float(min(1.0, 2 * (1 - 0.5 * (1 + math.erf(u / math.sqrt(2))))))


def outcome_base_rates(exc: dict, u_up: Optional[float], u_dn: Optional[float]) -> Optional[dict]:
    """pi_k: share of past days on which price reached the up distance first, the down distance first, or
    neither within h days. A same-bar double hit counts half to each side."""
    if not exc or u_up is None or u_dn is None:
        return None
    up_hit = exc["up"] >= u_up
    dn_hit = exc["dn"] >= u_dn
    h = up_hit.shape[1]
    first_up = np.where(up_hit.any(axis=1), up_hit.argmax(axis=1), h + 1)
    first_dn = np.where(dn_hit.any(axis=1), dn_hit.argmax(axis=1), h + 1)
    n = len(first_up)
    up = ((first_up < first_dn).sum() + 0.5 * ((first_up == first_dn) & (first_up <= h)).sum()) / n
    dn = ((first_dn < first_up).sum() + 0.5 * ((first_up == first_dn) & (first_up <= h)).sum()) / n
    rng = 1 - up - dn
    eps = 1e-3   # keep every outcome possible
    v = np.clip(np.array([up, dn, rng]), eps, None)
    v = v / v.sum()
    return {"up": float(v[0]), "down": float(v[1]), "range": float(v[2]), "n": int(n)}


def distance_units(price: float, level: float, sigma: float, h: int, additive: bool) -> Optional[float]:
    if not sigma or not np.isfinite(sigma) or sigma <= 0:
        return None
    d = abs(level - price) if additive else abs(math.log(level / price)) if level > 0 and price > 0 else None
    if d is None:
        return None
    return d / (sigma * math.sqrt(h / 252))
