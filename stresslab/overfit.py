"""Overfitting statistics: PBO via CSCV and the Deflated Sharpe Ratio.

Credit: the Probability of Backtest Overfitting (PBO) estimated by
combinatorially symmetric cross validation (CSCV) is due to David H. Bailey,
Jonathan M. Borwein, Marcos Lopez de Prado and Qiji Jim Zhu. The Deflated
Sharpe Ratio is due to David H. Bailey and Marcos Lopez de Prado.

These are independent implementations written from the published method
descriptions. They have NOT been checked line by line against the original
papers; verify them against the original papers before relying on the
numbers for a capital decision.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np
from scipy import stats

EULER_GAMMA = 0.5772156649015329


def pbo_cscv(perf_matrix, n_splits: int = 16) -> dict:
    """Probability of Backtest Overfitting via CSCV (Bailey, Borwein, Lopez de Prado, Zhu).

    ``perf_matrix`` is T x N: T periods (rows) of returns for N strategy
    configurations (columns). Rows are split into ``n_splits`` contiguous
    blocks; every combination of half the blocks is an in sample (IS) set and
    its complement the out of sample (OOS) set. For each split the best IS
    configuration (by Sharpe) is located in the OOS ranking; PBO is the share
    of splits where it lands at or below the OOS median (logit <= 0).

    Verify against the original paper before relying on the result.
    """
    m = np.asarray(perf_matrix, dtype=float)
    if m.ndim != 2 or m.shape[1] < 2:
        raise ValueError("perf_matrix must be T x N with N >= 2")
    if n_splits % 2 or n_splits < 2:
        raise ValueError("n_splits must be an even number >= 2")
    t, n = m.shape
    t_use = (t // n_splits) * n_splits
    if t_use < n_splits * 2:
        raise ValueError("too few rows for the requested number of splits")
    blocks = m[:t_use].reshape(n_splits, t_use // n_splits, n)
    s1 = blocks.sum(axis=1)          # n_splits x N
    s2 = (blocks**2).sum(axis=1)
    cnt = blocks.shape[1]
    combos = np.array(list(combinations(range(n_splits), n_splits // 2)))
    mask = np.zeros((len(combos), n_splits))
    np.put_along_axis(mask, combos, 1.0, axis=1)

    def sharpe(msk):
        k = msk.sum(axis=1, keepdims=True) * cnt
        mu = (msk @ s1) / k
        var = (msk @ s2) / k - mu**2
        return mu / np.sqrt(np.clip(var, 1e-300, None))

    sr_is = sharpe(mask)
    sr_oos = sharpe(1.0 - mask)
    best = sr_is.argmax(axis=1)
    oos_best = sr_oos[np.arange(len(best)), best]
    # rank 1..N of the IS winner within OOS performance
    rank = (sr_oos < oos_best[:, None]).sum(axis=1) + 1
    omega = rank / (n + 1.0)
    logits = np.log(omega / (1 - omega))
    return {"pbo": float((logits <= 0).mean()), "n_combinations": int(len(combos)),
            "logit_median": float(np.median(logits)), "n_strategies": int(n), "n_periods_used": int(t_use)}


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum Sharpe of ``n_trials`` zero skill trials (Bailey and Lopez de Prado)."""
    if n_trials <= 1:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(sr_variance) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(returns, n_trials: int, sr_variance_across_trials: float | None = None) -> dict:
    """Deflated Sharpe Ratio (Bailey and Lopez de Prado).

    Probability that the true per period Sharpe exceeds the Sharpe one would
    expect from the best of ``n_trials`` zero skill strategies. Sharpe ratios
    are per period (not annualised). Skewness and (non excess) kurtosis of the
    returns enter the standard error.

    ``sr_variance_across_trials`` should be the variance of the Sharpe ratios
    of all trials tried (see ``trial_log.sharpe_variance``). If it is not
    given, the asymptotic variance of the Sharpe estimator itself is used as
    a stand in, which is an assumption, not part of the published method.

    Verify against the original paper before relying on the result.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    t = len(r)
    if t < 3:
        raise ValueError("need at least 3 finite returns")
    sd = r.std(ddof=1)
    if sd == 0:
        raise ValueError("returns have zero variance")
    sr = r.mean() / sd
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    se_term = np.sqrt(max(denom, 1e-12))
    var = sr_variance_across_trials
    if var is None:
        var = denom / (t - 1)
    sr0 = expected_max_sharpe(int(n_trials), float(var))
    z = (sr - sr0) * np.sqrt(t - 1) / se_term
    return {"sharpe": float(sr), "sr_benchmark": sr0, "dsr": float(stats.norm.cdf(z)),
            "n_trials": int(n_trials), "n_obs": t, "skew": skew, "kurtosis": kurt}
