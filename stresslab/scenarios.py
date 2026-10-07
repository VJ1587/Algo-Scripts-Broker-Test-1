"""Stress scenarios (synthetic) and historical replay windows (real data).

All synthetic magnitudes below (volatility, drift, gap and crash sizes,
spread multiples, latency, reject rates) are ILLUSTRATIVE. Calibrate them per
instrument from its own history before treating a scenario result as a
statement about that market.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

from stresslab import generators as g
from stresslab.execution_sim import ExecConditions

N_BARS = 3000


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    make_bars: Callable[[int], pd.DataFrame]
    cond: ExecConditions = field(default_factory=ExecConditions)
    baseline: bool = False


def _random_walk(seed):
    return g.to_bars(g.gbm_returns(N_BARS, seed=seed), seed=seed + 1)


def _garch(seed):
    return g.to_bars(g.garch_returns(N_BARS, seed=seed), seed=seed + 1)


def _trend(seed):
    return g.to_bars(g.gbm_returns(N_BARS, drift=0.00015, seed=seed), seed=seed + 1)


def _range(seed):
    r, _ = g.regime_switching_returns(N_BARS, regimes=[g.regime_by_name("range")], seed=seed)
    return g.to_bars(r, seed=seed + 1)


def _regimes(seed):
    r, _ = g.regime_switching_returns(N_BARS, seed=seed)
    return g.to_bars(r, seed=seed + 1)


def _flash(seed):
    bars = _random_walk(seed)
    bars = g.apply_flash_crash(bars, at=N_BARS // 2, depth=0.06, recover_bars=30)
    return g.widen_spread(bars, N_BARS // 2 - 2, N_BARS // 2 + 30, 10.0)


def _peg_break(seed):
    bars = g.to_bars(g.gbm_returns(N_BARS, sigma=0.0004, seed=seed), seed=seed + 1)
    return g.apply_gap(bars, at=N_BARS // 2, pct=-0.08)


def _jumpy(seed):
    return g.to_bars(g.jump_returns(N_BARS, seed=seed), seed=seed + 1)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("random_walk_null", "GBM, no drift: any edge here is leakage or fill bias", _random_walk),
    Scenario("vol_clustering_fat_tails", "GARCH(1,1) with Student t shocks", _garch, baseline=True),
    Scenario("steady_trend", "GBM with persistent positive drift", _trend),
    Scenario("choppy_range", "Mean reverting AR(1) range", _range, baseline=True),
    Scenario("regime_switching", "Markov switching trend/range/panic", _regimes, baseline=True),
    Scenario("flash_crash", "6% intrabar crash, 30 bar recovery, spread x10 during it", _flash),
    Scenario("peg_break_gap", "Quiet pegged market then an 8% gap", _peg_break,
             cond=ExecConditions(slippage=0.0001)),
    Scenario("liquidity_drought", "Spread x6, 2 bar latency, 10% rejects, 20% partial fills", _random_walk,
             cond=ExecConditions(spread_mult=6.0, latency_bars=2, reject_prob=0.10, partial_fill_prob=0.20)),
    Scenario("jumpy_news_market", "Diffusion plus news jumps", _jumpy, baseline=True),
    Scenario("slow_execution", "3 bar latency plus slippage", _garch,
             cond=ExecConditions(latency_bars=3, slippage=0.0002)),
)

SCENARIOS_BY_NAME = {s.name: s for s in SCENARIOS}

# Windows to replay with REAL data. Dates are approximate and must be
# verified against primary sources (central bank statements, exchange
# notices) before use. No move sizes are listed on purpose: measure them from
# your own data.
HISTORICAL_REPLAYS = (
    {"name": "snb_floor_removal", "instruments": "EURCHF, USDCHF, CHF crosses", "start": "2015-01-12",
     "end": "2015-01-23", "note": "SNB removes the EUR/CHF minimum rate. Verify dates against primary sources."},
    {"name": "uk_eu_referendum", "instruments": "GBP pairs, UK indices", "start": "2016-06-20",
     "end": "2016-07-01", "note": "UK EU membership referendum result. Verify dates against primary sources."},
    {"name": "covid_liquidity_shock", "instruments": "all majors, indices, gold, oil", "start": "2020-02-17",
     "end": "2020-04-30", "note": "COVID liquidity shock. Verify dates against primary sources."},
    {"name": "uk_gilt_ldi_crisis", "instruments": "GBP pairs, gilts", "start": "2022-09-21",
     "end": "2022-10-17", "note": "UK gilt / LDI crisis. Verify dates against primary sources."},
    {"name": "yen_carry_unwind", "instruments": "JPY crosses, indices", "start": "2024-07-29",
     "end": "2024-08-09", "note": "Yen carry trade unwind. Verify dates against primary sources."},
)
