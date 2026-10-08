"""Pair math: combining two personalities.

r_{A/B} = (N-1)/N (c_A - c_B), so sensitivities subtract, energy depends on how alike the two are, and
one side usually runs the pair (dominance share). Conviction, patience and level respect are measured on
the pair itself.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from .traits import rolling_ols

ADF_CRIT_5PCT = -2.86   # Dickey-Fuller with a constant, large sample (approximate; no lag terms)


def dominance(cA: pd.Series, r_pair: pd.Series, N: int, window: int) -> Optional[float]:
    j = pd.concat([cA, r_pair], axis=1, join="inner").dropna().iloc[-window:]
    if len(j) < window * 0.8:
        return None
    v = j.iloc[:, 1].var()
    if v <= 0:
        return None
    return float((N - 1) / N * j.cov().iloc[0, 1] / v)


def predicted_pair_beta(beta_a: Optional[float], beta_b: Optional[float], N: int) -> Optional[float]:
    if beta_a is None or beta_b is None:
        return None
    return (N - 1) / N * (beta_a - beta_b)


def predicted_pair_energy(sa: float, sb: float, rho: float, N: int) -> float:
    return (N - 1) / N * math.sqrt(max(sa * sa + sb * sb - 2 * rho * sa * sb, 0.0))


def carry_appeal(rate_a: Optional[float], rate_b: Optional[float], sigma_pair: Optional[float]) -> Optional[float]:
    """CA = (i_A - i_B) / sigma_{A/B}; rates in percent, sigma annualized."""
    if rate_a is None or rate_b is None or not sigma_pair:
        return None
    return float((rate_a - rate_b) / 100.0 / sigma_pair)


def mean_reversion_test(u: pd.Series) -> dict:
    """AR(1) on the residual: du = a + b u_{t-1}. Dickey-Fuller style t on b and the implied half life."""
    u = u.dropna()
    if len(u) < 100:
        return {"t": None, "half_life": None, "mean_reverting": False}
    du, ul = u.diff().iloc[1:], u.shift(1).iloc[1:]
    X = np.column_stack([np.ones(len(ul)), ul.values])
    beta, *_ = np.linalg.lstsq(X, du.values, rcond=None)
    resid = du.values - X @ beta
    s2 = resid @ resid / (len(du) - 2)
    se = math.sqrt(s2 * np.linalg.inv(X.T @ X)[1, 1])
    t = float(beta[1] / se) if se > 0 else None
    hl = float(-math.log(2) / math.log1p(beta[1])) if -1 < beta[1] < 0 else None
    return {"t": None if t is None else round(t, 2), "half_life": None if hl is None else round(hl, 1),
            "mean_reverting": bool(t is not None and t < ADF_CRIT_5PCT)}


def incentive_gap(log_price: pd.Series, drivers: pd.DataFrame, window: int, history_days: int = 0) -> dict:
    """s_t = a + b1 (y_A - y_B) + b2 ToT + b3 Risk + u_t on a rolling window; G_t = u_t / sigma_u.
    The gap is only trusted if the window's residual is mean reverting."""
    X = drivers.reindex(log_price.index)
    cols = [c for c in X.columns if X[c].notna().sum() >= window * 0.8]
    if not cols:
        return {"G": None, "status": "no drivers", "drivers": []}
    X = X[cols]
    fit = rolling_ols(log_price, X, window, full=True)
    fitted = fit["const"] + sum(fit[c] * X[c] for c in cols)
    G = (log_price - fitted) / fit["resid_sd"]
    G = G.dropna()
    if G.empty:
        return {"G": None, "status": "insufficient history", "drivers": cols}
    last = fit.dropna().iloc[-1]
    in_win = log_price.iloc[-window:]
    u = in_win - (last["const"] + sum(last[c] * X[c].reindex(in_win.index) for c in cols))
    mr = mean_reversion_test(u)
    g_now = float(G.iloc[-1])
    g_20 = float(G.iloc[-21]) if len(G) > 21 else None
    trend = None if g_20 is None else ("widening" if abs(g_now) > abs(g_20) + 0.25 else
                                       "closing" if abs(g_now) < abs(g_20) - 0.25 else "steady")
    days_beyond = 0
    for g in G.iloc[::-1]:
        if abs(g) > 2:
            days_beyond += 1
        else:
            break
    out = {"G": round(g_now, 2), "trend": trend, "days_beyond_2": days_beyond, "drivers": cols,
           "betas": {c: round(float(last[c]), 4) for c in cols}, "mean_reversion": mr,
           "status": "trusted" if mr["mean_reverting"] else "not trusted: residual not mean reverting"}
    if history_days:
        out["history"] = G
    return out


def gap_closure_rate(G: pd.Series, threshold: float = 2.0, close_to: float = 0.5, horizon: int = 60) -> dict:
    """Validation: does a gap beyond 2 close (back inside +/-0.5) within 60 days more often than not?
    Counted once per excursion."""
    g = G.dropna()
    n = hits = 0
    i = 0
    vals = g.values
    while i < len(vals) - horizon:
        if abs(vals[i]) > threshold:
            n += 1
            fut = np.abs(vals[i + 1:i + 1 + horizon])
            if (fut < close_to).any():
                hits += 1
            # skip to the end of this excursion
            while i < len(vals) and abs(vals[i]) > close_to:
                i += 1
        i += 1
    return {"excursions": n, "closed": hits, "rate": None if n == 0 else round(hits / n, 3)}
