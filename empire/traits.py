"""Personality math for a single instrument: eight traits, three added measures, bands, drift.

All rolling measures use only data up to each date, so every reading is point in time.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

TRAIT_NAMES = ["energy", "energy_long", "atr_pct", "conviction", "vr5", "vr60", "patience", "fear_beta",
               "rate_beta", "resource_beta", "event_reactivity", "asymmetry"]
# the vector used for the single "breaking character" distance (level respect has no daily history)
DISTANCE_TRAITS = ["energy", "conviction", "patience", "fear_beta", "rate_beta", "resource_beta", "asymmetry"]
TRAIT_TEXT = {
    "energy": "Energy: annualized volatility, 20 days", "energy_long": "Energy: annualized volatility, 250 days",
    "atr_pct": "Energy: ATR as a share of price, 20 days", "conviction": "Conviction: variance ratio VR(20)",
    "vr5": "Conviction: VR(5)", "vr60": "Conviction: VR(60)", "patience": "Patience: mean reversion half life (days, log)",
    "fear_beta": "Fear response: beta to VIX change", "rate_beta": "Rate sensitivity: beta to 2y yield gap change",
    "resource_beta": "Resource sensitivity: beta to commodity return", "event_reactivity": "Event reactivity: event day / normal day |move|",
    "asymmetry": "Downside asymmetry: down-day size / up-day size",
}


# =============================================================================
# Rolling building blocks
# =============================================================================

def rolling_ols(y: pd.Series, X: pd.DataFrame, window: int, min_frac: float = 0.8, full: bool = False) -> pd.DataFrame:
    """Rolling OLS with an intercept via cumulative cross products. Rows with any NaN are left out of each
    window. Returns one column per regressor; with full=True also 'const' and 'resid_sd' (window residual sd)."""
    df = pd.concat([y.rename("_y"), X], axis=1)
    valid = df.notna().all(axis=1).values
    yv = np.where(valid, np.nan_to_num(df["_y"].values.astype(float)), 0.0)
    Xv = np.column_stack([np.ones(len(df)), X.values.astype(float)])
    Xv = np.where(valid[:, None], np.nan_to_num(Xv), 0.0)
    k = Xv.shape[1]
    cxx = np.cumsum(np.einsum("ti,tj->tij", Xv, Xv), axis=0)
    cxy = np.cumsum(Xv * yv[:, None], axis=0)
    cyy = np.cumsum(yv * yv)
    cn = np.cumsum(valid.astype(float))

    def win(c):
        lag = np.concatenate([np.zeros((window,) + c.shape[1:]), c], axis=0)[:len(df)]
        return c - lag

    A, b, yy, n = win(cxx), win(cxy), win(cyy), win(cn)
    coef = np.full((len(df), k), np.nan)
    ok = (np.arange(len(df)) >= window - 1) & (n >= min_frac * window)
    if ok.any():
        Ak, bk = A[ok], b[ok]
        diag = np.sqrt(np.clip(np.einsum("tii->ti", Ak), 1e-300, None))
        As = Ak / (diag[:, :, None] * diag[:, None, :])       # scale to a correlation-like matrix
        good = np.abs(np.linalg.det(As)) > 1e-10
        sol = np.full((int(ok.sum()), k), np.nan)
        if good.any():
            z = np.linalg.solve(As[good], (bk[good] / diag[good])[..., None])[..., 0]
            sol[good] = z / diag[good]
        coef[ok] = sol
    out = pd.DataFrame(coef[:, 1:], index=df.index, columns=list(X.columns))
    if full:
        out["const"] = coef[:, 0]
        ssr = yy - np.einsum("ti,ti->t", np.nan_to_num(coef), b)
        dof = np.maximum(n - k, 1)
        out["resid_sd"] = np.where(np.isnan(coef[:, 0]), np.nan, np.sqrt(np.clip(ssr, 0, None) / dof))
    return out


def variance_ratio(r: pd.Series, q: int, window: int) -> pd.Series:
    """VR(q) = Var(q-day sum) / (q Var(1-day)), on overlapping sums within a rolling window."""
    s = r.rolling(q).sum()
    num = s.rolling(window, min_periods=int(window * 0.8)).var()
    den = q * r.rolling(window, min_periods=int(window * 0.8)).var()
    return num / den


def half_life(level: pd.Series, anchor_days: int, window: int, additive: bool) -> pd.Series:
    """Regress the change in the gap to the anchor on the prior gap: t_half = -ln2 / ln(1 + b), b < 0.
    Returned on a log10 scale of days so its distribution is usable; inf (no reversion) maps to 4 (10,000 days)."""
    x = level - level.rolling(anchor_days, min_periods=int(anchor_days * 0.8)).mean() if additive else \
        np.log(level) - np.log(level.rolling(anchor_days, min_periods=int(anchor_days * 0.8)).mean())
    dx = x.diff()
    xl = x.shift(1)
    cov = dx.rolling(window, min_periods=int(window * 0.8)).cov(xl)
    var = xl.rolling(window, min_periods=int(window * 0.8)).var()
    b = cov / var
    hl = pd.Series(np.nan, index=level.index)
    ok = (b < 0) & (b > -1)
    hl[ok] = -math.log(2) / np.log1p(b[ok])
    hl[(b >= 0)] = 1e4
    return np.log10(hl.clip(lower=1.0, upper=1e4))


def atr_series(bars: pd.DataFrame, n: int) -> pd.Series:
    c_prev = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - c_prev).abs(), (bars["low"] - c_prev).abs()], axis=1).max(axis=1)
    if "close_only" in bars and bars["close_only"].any():
        tr = tr.where(~bars["close_only"].astype(bool), (bars["close"] - c_prev).abs())
    return tr.rolling(n, min_periods=max(2, int(n * 0.8))).mean()


def asymmetry(r: pd.Series, window: int) -> pd.Series:
    """A = sqrt(mean(min(r,0)^2) / mean(max(r,0)^2)); above 1 means down days are larger."""
    dn = (r.clip(upper=0) ** 2).rolling(window, min_periods=int(window * 0.8)).mean()
    up = (r.clip(lower=0) ** 2).rolling(window, min_periods=int(window * 0.8)).mean()
    return np.sqrt(dn / up)


def event_reactivity(r: pd.Series, event_days: Optional[pd.DatetimeIndex], window: int, min_events: int = 8) -> pd.Series:
    if event_days is None or len(event_days) == 0:
        return pd.Series(np.nan, index=r.index)
    is_ev = pd.Series(r.index.isin(event_days), index=r.index)
    a = r.abs()
    ev_sum = (a.where(is_ev, 0.0)).rolling(window, min_periods=int(window * 0.8)).sum()
    ev_n = is_ev.astype(float).rolling(window, min_periods=int(window * 0.8)).sum()
    no_sum = (a.where(~is_ev, 0.0)).rolling(window, min_periods=int(window * 0.8)).sum()
    no_n = (~is_ev).astype(float).rolling(window, min_periods=int(window * 0.8)).sum()
    e = (ev_sum / ev_n) / (no_sum / no_n)
    return e.where(ev_n >= min_events)


def compute_traits(r: pd.Series, bars: Optional[pd.DataFrame], level: pd.Series, additive: bool,
                   drivers: pd.DataFrame, event_days: Optional[pd.DatetimeIndex], tcfg: dict) -> pd.DataFrame:
    """Daily trait history for one instrument. drivers has columns fear, rate, resource (any may be NaN)."""
    ws, wl = int(tcfg.get("short_window", 20)), int(tcfg.get("long_window", 250))
    out = pd.DataFrame(index=r.index)
    scale = math.sqrt(252)
    out["energy"] = r.rolling(ws, min_periods=int(ws * 0.8)).std() * scale
    out["energy_long"] = r.rolling(wl, min_periods=int(wl * 0.8)).std() * scale
    if bars is not None and not additive:
        out["atr_pct"] = (atr_series(bars, ws) / bars["close"]).reindex(r.index)
    else:
        out["atr_pct"] = np.nan
    out["vr5"] = variance_ratio(r, 5, wl)
    out["conviction"] = variance_ratio(r, 20, wl)
    out["vr60"] = variance_ratio(r, 60, max(wl, 500))
    out["patience"] = half_life(level.reindex(r.index), int(tcfg.get("anchor_days", 200)), wl, additive)
    X = drivers.reindex(r.index)
    cols = [c for c in ("fear", "rate", "resource") if c in X and X[c].notna().sum() > wl]
    if cols:
        b = rolling_ols(r, X[cols], wl)
        for c in ("fear", "rate", "resource"):
            out[f"{c}_beta"] = b[c] if c in b else np.nan
    else:
        out["fear_beta"] = out["rate_beta"] = out["resource_beta"] = np.nan
    out["event_reactivity"] = event_reactivity(r, event_days, wl)
    out["asymmetry"] = asymmetry(r, wl)
    return out[TRAIT_NAMES]


# =============================================================================
# Bands, distance, range bands, drift
# =============================================================================

def robust_z(x: pd.Series, value: float) -> Optional[float]:
    h = x.dropna()
    if len(h) < 50 or value is None or not np.isfinite(value):
        return None
    med = h.median()
    mad = (h - med).abs().median()
    if mad <= 0:
        return None
    return float((value - med) / (1.4826 * mad))


def band_of(pct: Optional[float], bcfg: dict) -> str:
    if pct is None:
        return "unknown"
    core, stretch = bcfg.get("core", [25, 75]), bcfg.get("stretched", [10, 90])
    if core[0] <= pct <= core[1]:
        return "core"
    if stretch[0] <= pct <= stretch[1]:
        return "stretched"
    return "out of character"


def trait_bands(traits: pd.DataFrame, hist_days: int, bcfg: dict) -> dict[str, dict]:
    """Current reading of each trait against its own history (up to hist_days back)."""
    out = {}
    for name in traits.columns:
        s = traits[name].dropna()
        if len(s) < 100:
            out[name] = {"value": None if s.empty else float(s.iloc[-1]), "pct": None, "z": None, "band": "unknown",
                         "median": None, "n": int(len(s))}
            continue
        hist = s.iloc[-hist_days:]
        v = float(s.iloc[-1])
        pct = float((hist < v).mean() * 100 + (hist == v).mean() * 50)
        out[name] = {"value": v, "pct": round(pct, 1), "z": robust_z(hist, v), "band": band_of(pct, bcfg),
                     "median": float(hist.median()), "n": int(len(hist))}
    return out


def character_distance(traits: pd.DataFrame, names: list[str], hist_days: int, flag_pct: float = 95.0,
                       sample_every: int = 5) -> dict:
    """D_t = sqrt((x - mu)' Sigma^-1 (x - mu)), with traits standardized first. Flag the top 5 percent."""
    cols = [c for c in names if c in traits and traits[c].notna().sum() > 250]
    if len(cols) < 2:
        return {"D": None, "pct": None, "flag": False, "traits": cols}
    X = traits[cols].iloc[-hist_days:].dropna()
    if len(X) < 250:
        return {"D": None, "pct": None, "flag": False, "traits": cols}
    mu = X.mean()
    sd = X.std().replace(0, np.nan)
    Z = ((X - mu) / sd).dropna(axis=1)
    if Z.shape[1] < 2:
        return {"D": None, "pct": None, "flag": False, "traits": cols}
    S = np.cov(Z.iloc[::sample_every].values, rowvar=False)
    Si = np.linalg.pinv(S)
    zz = Z.values
    D = np.sqrt(np.einsum("ti,ij,tj->t", zz, Si, zz))
    d_now = float(D[-1])
    pct = float((D < d_now).mean() * 100)
    return {"D": round(d_now, 3), "pct": round(pct, 1), "flag": pct >= flag_pct, "traits": list(Z.columns),
            "history": pd.Series(D, index=Z.index)}


def vol_range_band(price: float, sigma: float, h: int, k: float, additive: bool) -> tuple[float, float]:
    if additive:
        d = k * sigma * math.sqrt(h / 252)
        return price - d, price + d
    f = k * sigma * math.sqrt(h / 252)
    return price * math.exp(-f), price * math.exp(f)


def measured_range_band(r: pd.Series, regimes: Optional[pd.Series], regime_now: Optional[str], price: float, h: int,
                        additive: bool, q=(10, 90), min_obs: int = 120) -> dict:
    """10th and 90th percentile of h-day returns, within the current regime when it has enough history."""
    R = r.rolling(h).sum().dropna()                   # h-day returns that have already happened
    scope = "all history"
    if regimes is not None and regime_now:
        start_regime = regimes.shift(h).reindex(R.index)  # regime in force when each window began
        rr = R[start_regime == regime_now]
        if len(rr) >= min_obs:
            R, scope = rr, f"regime {regime_now}"
    if len(R) < min_obs:
        return {"lower": None, "upper": None, "scope": "insufficient history"}
    lo, hi = np.percentile(R, q)
    if additive:
        return {"lower": price + lo, "upper": price + hi, "scope": scope}
    return {"lower": price * math.exp(lo), "upper": price * math.exp(hi), "scope": scope}


def ewma_drift(traits: pd.DataFrame, half_life_days: int, hist_days: int) -> pd.DataFrame:
    """Drift_t = (EWMA_t - median_20y) / (1.4826 MAD_20y) for every trait."""
    alpha = 1 - 2 ** (-1 / half_life_days)
    sm = traits.ewm(alpha=alpha, adjust=False, ignore_na=True).mean()
    out = pd.DataFrame(index=traits.index)
    for c in traits.columns:
        h = traits[c].dropna().iloc[-hist_days:]
        if len(h) < 250:
            out[c] = np.nan
            continue
        med = h.median()
        mad = (h - med).abs().median()
        out[c] = (sm[c] - med) / (1.4826 * mad) if mad > 0 else np.nan
    return out


def drift_streak(d: pd.Series, threshold: float) -> tuple[int, int]:
    """Consecutive trailing days with |drift| beyond the threshold, and the sign of that run."""
    v = d.dropna()
    if v.empty:
        return 0, 0
    sign = int(np.sign(v.iloc[-1])) if abs(v.iloc[-1]) > threshold else 0
    if sign == 0:
        return 0, 0
    n = 0
    for x in v.iloc[::-1]:
        if abs(x) > threshold and int(np.sign(x)) == sign:
            n += 1
        else:
            break
    return n, sign


# =============================================================================
# Solo currency indices  c_i = 1/(N-1) sum_{j != i} r_{i/j}
# =============================================================================

def currency_log_values(usd_pairs: dict[str, pd.Series], majors: tuple) -> pd.DataFrame:
    """Log value of each major in dollars (USD = 0) from the seven dollar pairs' closes."""
    L = {}
    for c in majors:
        if c == "USD":
            continue
        if f"{c}USD" in usd_pairs:
            L[c] = np.log(usd_pairs[f"{c}USD"])
        elif f"USD{c}" in usd_pairs:
            L[c] = -np.log(usd_pairs[f"USD{c}"])
    df = pd.DataFrame(L).dropna()
    df["USD"] = 0.0
    return df[[c for c in majors if c in df]]


def solo_indices(logv: pd.DataFrame) -> pd.DataFrame:
    """Basket returns c_i for each currency against the other N-1 majors."""
    dL = logv.diff()
    N = dL.shape[1]
    return (N / (N - 1)) * (dL.sub(dL.mean(axis=1), axis=0))


def cross_from_legs(logv: pd.DataFrame, base: str, quote: str) -> pd.Series:
    return np.exp(logv[base] - logv[quote])
