"""Monte Carlo over trade sequences, parameters and synthetic price paths."""
from __future__ import annotations

import numpy as np


def _max_losing_streak(r_row: np.ndarray) -> int:
    best = cur = 0
    for x in r_row:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def trade_sequence_mc(r_multiples, n_sims: int = 2000, n_trades: int | None = 200, risk_frac: float = 0.01,
                      ruin_dd: float = 0.25, method: str = "bootstrap", seed: int = 0) -> dict:
    """Resample a trade list into many equity paths.

    ``method="bootstrap"`` draws ``n_trades`` trades with replacement.
    ``method="shuffle"`` permutes the observed trades (``n_trades`` is then
    the number of observed trades); it keeps the exact trade set, so
    expectancy and final compounded equity are preserved and only path
    dependent statistics (drawdown, streaks) vary.

    Equity compounds as ``equity *= 1 + risk_frac * R``.
    """
    r = np.asarray(r_multiples, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return {"n_input_trades": 0, "error": "no finite R multiples"}
    rng = np.random.default_rng(seed)
    if method == "bootstrap":
        n_t = int(n_trades or len(r))
        paths = rng.choice(r, size=(n_sims, n_t), replace=True)
    elif method == "shuffle":
        n_t = len(r)
        paths = np.stack([rng.permutation(r) for _ in range(n_sims)])
    else:
        raise ValueError("method must be 'bootstrap' or 'shuffle'")
    growth = np.clip(1.0 + risk_frac * paths, 1e-12, None)
    eq = np.cumprod(growth, axis=1)
    eq = np.concatenate([np.ones((n_sims, 1)), eq], axis=1)
    peak = np.maximum.accumulate(eq, axis=1)
    max_dd = (1 - eq / peak).max(axis=1)
    final = eq[:, -1]
    streaks = np.array([_max_losing_streak(row) for row in paths])
    return {
        "method": method,
        "n_input_trades": int(len(r)),
        "n_sims": int(n_sims),
        "n_trades": int(n_t),
        "risk_frac": float(risk_frac),
        "mean_r": float(r.mean()),
        "path_mean_r": float(paths.mean()),
        "max_dd_p50": float(np.percentile(max_dd, 50)),
        "max_dd_p95": float(np.percentile(max_dd, 95)),
        "max_dd_p99": float(np.percentile(max_dd, 99)),
        "final_equity_p5": float(np.percentile(final, 5)),
        "final_equity_p50": float(np.percentile(final, 50)),
        "losing_streak_p95": float(np.percentile(streaks, 95)),
        "prob_ruin": float((max_dd >= ruin_dd).mean()),
        "ruin_dd": float(ruin_dd),
        "prob_loss": float((final < 1.0).mean()),
    }


def parameter_sensitivity(metric_fn, base_params: dict, jitter: float = 0.2, n: int = 30,
                          int_params=None, seed: int = 0) -> dict:
    """Perturb every numeric parameter by up to +/- ``jitter`` (relative).

    ``cliff_ratio`` is the share of perturbed runs whose metric falls by more
    than half of the base metric's magnitude: a high value means the result
    sits on a narrow peak and is probably fitted to noise.
    """
    rng = np.random.default_rng(seed)
    if int_params is None:
        int_params = [k for k, v in base_params.items() if isinstance(v, int) and not isinstance(v, bool)]
    int_params = set(int_params)
    base = float(metric_fn(dict(base_params)))
    vals, trials = [], []
    for _ in range(n):
        p = dict(base_params)
        for k, v in base_params.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            x = v * (1 + rng.uniform(-jitter, jitter))
            p[k] = max(1, int(round(x))) if k in int_params else float(x)
        m = float(metric_fn(p))
        vals.append(m)
        trials.append((p, m))
    vals_a = np.asarray(vals, dtype=float)
    finite = vals_a[np.isfinite(vals_a)]
    drop = base - 0.5 * abs(base)
    cliff = float(np.mean(~np.isfinite(vals_a) | (vals_a < drop))) if len(vals_a) else float("nan")
    pct = (lambda q: float(np.percentile(finite, q))) if len(finite) else (lambda q: float("nan"))
    return {"base": base, "n": n, "jitter": jitter, "p5": pct(5), "p50": pct(50), "p95": pct(95),
            "cliff_ratio": cliff, "trials": trials}


def synthetic_path_mc(evaluate, make_bars, n_paths: int = 20, seed: int = 0) -> dict:
    """Evaluate a strategy metric on many synthetic paths; ``make_bars(seed)``."""
    vals = np.array([float(evaluate(make_bars(seed + k))) for k in range(n_paths)])
    finite = vals[np.isfinite(vals)]
    if len(finite) == 0:
        return {"n_paths": n_paths, "values": vals.tolist(), "error": "no finite values"}
    return {"n_paths": n_paths, "values": vals.tolist(), "mean": float(finite.mean()),
            "p5": float(np.percentile(finite, 5)), "p50": float(np.percentile(finite, 50)),
            "p95": float(np.percentile(finite, 95)), "share_positive": float((finite > 0).mean())}
