"""Synthetic markets with controlled properties.

All return generators produce per bar LOG returns. ``to_bars`` turns them into
OHLC bars. All magnitudes here (default sigma, spread, jump sizes) are
illustrative and must be calibrated per instrument before drawing
conclusions about a specific market.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_SIGMA = 0.001  # per hourly bar, illustrative (roughly a major FX pair)
DEFAULT_SPREAD_REL = 8e-5  # spread as a fraction of price, illustrative


@dataclass(frozen=True)
class Regime:
    name: str
    drift: float
    sigma: float
    ar: float = 0.0


DEFAULT_REGIMES: tuple[Regime, ...] = (
    Regime("trend_up", drift=0.0003, sigma=0.0009, ar=0.05),
    Regime("trend_down", drift=-0.0003, sigma=0.0010, ar=0.05),
    Regime("range", drift=0.0, sigma=0.0007, ar=-0.35),
    Regime("panic", drift=-0.0002, sigma=0.0035, ar=0.0),
)


def regime_by_name(name: str) -> Regime:
    for r in DEFAULT_REGIMES:
        if r.name == name:
            return r
    raise KeyError(f"unknown regime {name!r}; known: {[r.name for r in DEFAULT_REGIMES]}")


def gbm_returns(n: int, sigma: float = DEFAULT_SIGMA, drift: float = 0.0, seed: int = 0) -> np.ndarray:
    """IID normal log returns (geometric Brownian motion in price)."""
    rng = np.random.default_rng(seed)
    return drift + sigma * rng.standard_normal(n)


def garch_returns(n: int, sigma: float = DEFAULT_SIGMA, alpha: float = 0.08, beta: float = 0.90,
                  nu: float = 5.0, seed: int = 0) -> np.ndarray:
    """GARCH(1,1) log returns with unit variance Student t shocks.

    ``sigma`` is the unconditional per bar volatility; omega is set so the
    long run variance equals ``sigma**2``.
    """
    if alpha + beta >= 1:
        raise ValueError("alpha + beta must be < 1 for a stationary GARCH(1,1)")
    if nu <= 2:
        raise ValueError("nu must be > 2 so the shocks have finite variance")
    rng = np.random.default_rng(seed)
    omega = sigma**2 * (1 - alpha - beta)
    z = rng.standard_t(nu, n) * np.sqrt((nu - 2) / nu)
    r = np.empty(n)
    var = sigma**2
    for t in range(n):
        r[t] = np.sqrt(var) * z[t]
        var = omega + alpha * r[t] ** 2 + beta * var
    return r


def jump_returns(n: int, sigma: float = DEFAULT_SIGMA, jump_prob: float = 0.004,
                 jump_scale: float = 0.008, seed: int = 0) -> np.ndarray:
    """Normal diffusion plus Poisson style jumps (news shocks)."""
    rng = np.random.default_rng(seed)
    base = sigma * rng.standard_normal(n)
    jumps = rng.random(n) < jump_prob
    size = rng.normal(0.0, jump_scale, n)
    return base + np.where(jumps, size, 0.0)


def regime_switching_returns(n: int, regimes=None, stay_prob: float = 0.985,
                             transition: np.ndarray | None = None, seed: int = 0):
    """Markov switching AR(1) returns.

    Returns ``(returns, labels)``. Works with a single regime (then every
    label is that regime). Within a regime:
    ``r_t = drift + ar * (r_{t-1} - drift) + sigma * z_t``.
    """
    regimes = tuple(DEFAULT_REGIMES if regimes is None else regimes)
    k = len(regimes)
    if k == 0:
        raise ValueError("need at least one regime")
    if transition is None:
        if k == 1:
            transition = np.ones((1, 1))
        else:
            transition = np.full((k, k), (1 - stay_prob) / (k - 1))
            np.fill_diagonal(transition, stay_prob)
    transition = np.asarray(transition, dtype=float)
    if transition.shape != (k, k) or not np.allclose(transition.sum(axis=1), 1.0):
        raise ValueError("transition must be k x k with rows summing to 1")
    rng = np.random.default_rng(seed)
    z = rng.standard_normal(n)
    u = rng.random(n)
    cum = np.cumsum(transition, axis=1)
    state = int(rng.integers(k))
    r = np.empty(n)
    labels = np.empty(n, dtype=object)
    prev = 0.0
    for t in range(n):
        if t > 0:
            state = min(int(np.searchsorted(cum[state], u[t], side="right")), k - 1)
        g = regimes[state]
        prev = g.drift + g.ar * (prev - g.drift) + g.sigma * z[t]
        r[t] = prev
        labels[t] = g.name
    return r, labels


def block_bootstrap_returns(real_returns, n: int, block: int = 24, seed: int = 0) -> np.ndarray:
    """Moving block bootstrap of real returns (keeps short range dependence)."""
    x = np.asarray(real_returns, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < block:
        raise ValueError(f"need at least block={block} finite returns, got {len(x)}")
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, len(x) - block + 1, n_blocks)
    out = np.concatenate([x[s:s + block] for s in starts])
    return out[:n]


def to_bars(returns, start: str = "2020-01-06", freq: str = "1h", start_price: float = 1.10,
            spread_rel: float = DEFAULT_SPREAD_REL, substeps: int = 12, vol_window: int = 24,
            seed: int = 0) -> pd.DataFrame:
    """Build OHLC bars from per bar log returns.

    Highs and lows come from a Brownian bridge between each bar's open and
    close with ``substeps`` steps, scaled by local (trailing ``vol_window``
    bar) volatility. Independent random wicks are deliberately NOT used: they
    bias stop versus target hit rates on a pure random walk.

    open[t] equals close[t-1], so there are no gaps unless a shock adds one.
    """
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if n == 0:
        raise ValueError("returns is empty")
    if not np.isfinite(r).all():
        raise ValueError("returns contain non finite values")
    rng = np.random.default_rng(seed)
    log_close = np.log(start_price) + np.cumsum(r)
    log_open = np.concatenate([[np.log(start_price)], log_close[:-1]])

    # trailing local volatility (current bar included; no future data)
    local = pd.Series(r).rolling(vol_window, min_periods=2).std().to_numpy()
    first_ok = np.flatnonzero(np.isfinite(local))
    fallback = local[first_ok[0]] if len(first_ok) else (np.abs(r).mean() or 1e-6)
    local = np.where(np.isfinite(local) & (local > 0), local, fallback)

    # Brownian bridge from 0 to r[t] over `substeps` steps
    step_sd = (local / np.sqrt(substeps))[:, None]
    inc = rng.standard_normal((n, substeps)) * step_sd
    walk = np.concatenate([np.zeros((n, 1)), np.cumsum(inc, axis=1)], axis=1)
    frac = np.linspace(0.0, 1.0, substeps + 1)[None, :]
    bridge = walk - frac * walk[:, -1:] + frac * r[:, None]
    hi = log_open + bridge.max(axis=1)
    lo = log_open + bridge.min(axis=1)

    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=n, freq=freq)
    close = np.exp(log_close)
    bars = pd.DataFrame({
        "open": np.exp(log_open),
        "high": np.exp(hi),
        "low": np.exp(lo),
        "close": close,
        "volume": rng.integers(500, 5000, n).astype(float),
        "spread": close * spread_rel,
    }, index=idx)
    # guard against float rounding at the envelope edges
    bars["high"] = bars[["open", "high", "close"]].max(axis=1)
    bars["low"] = bars[["open", "low", "close"]].min(axis=1)
    return bars


def regime_bars(name: str, n: int = 600, seed: int = 0, **to_bars_kw) -> pd.DataFrame:
    """Bars from a single named default regime (used for discrimination tests)."""
    r, _ = regime_switching_returns(n, regimes=[regime_by_name(name)], seed=seed)
    return to_bars(r, seed=seed + 7919, **to_bars_kw)


# ---------------------------------------------------------------- shocks
def apply_gap(bars: pd.DataFrame, at: int, pct: float) -> pd.DataFrame:
    """Gap the market by ``pct`` at the open of bar ``at``; all later prices shift."""
    out = bars.copy()
    if not 0 < at < len(out):
        raise ValueError("gap position must be inside the series and not the first bar")
    f = 1.0 + pct
    if f <= 0:
        raise ValueError("pct must be > -1")
    cols = ["open", "high", "low", "close"]
    out.iloc[at:, [out.columns.get_loc(c) for c in cols]] *= f
    if "spread" in out.columns:
        out.iloc[at:, out.columns.get_loc("spread")] *= f
    return out


def apply_flash_crash(bars: pd.DataFrame, at: int, depth: float, recover_bars: int) -> pd.DataFrame:
    """Price collapses by ``depth`` within bar ``at`` then recovers linearly."""
    out = bars.copy()
    n = len(out)
    if not 0 < at < n:
        raise ValueError("crash position must be inside the series")
    if not 0 < depth < 1:
        raise ValueError("depth must be between 0 and 1")
    f = np.ones(n)
    end = min(n, at + recover_bars + 1)
    f[at:end] = 1 - depth * (1 - np.arange(end - at) / max(recover_bars, 1))
    f_prev = np.concatenate([[1.0], f[:-1]])
    o = out["open"].to_numpy() * f_prev
    c = out["close"].to_numpy() * f
    h = np.maximum.reduce([out["high"].to_numpy() * np.maximum(f, f_prev), o, c])
    lo = np.minimum.reduce([out["low"].to_numpy() * np.minimum(f, f_prev), o, c])
    lo[at] = lo[at] * (1 - depth * 0.25)  # overshoot wick on the crash bar
    out["open"], out["high"], out["low"], out["close"] = o, h, lo, c
    return out


def widen_spread(bars: pd.DataFrame, start: int, end: int, mult: float) -> pd.DataFrame:
    """Multiply the spread column by ``mult`` on rows [start, end)."""
    out = bars.copy()
    if "spread" not in out.columns:
        out["spread"] = out["close"] * DEFAULT_SPREAD_REL
    col = out.columns.get_loc("spread")
    out.iloc[start:end, col] = out.iloc[start:end, col] * mult
    return out
