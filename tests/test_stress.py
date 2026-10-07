"""StressLab Phase 1 tests: the harness works AND still catches planted bugs."""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.demo_components import (DemoPersonality, DonchianScanner, FixedFractionalExecutor,  # noqa: E402
                                      LeakyScanner, SimpleJournal, SloppyPersonality)
from stresslab import faults as F  # noqa: E402
from stresslab import generators as g  # noqa: E402
from stresslab import invariants as inv  # noqa: E402
from stresslab import trial_log  # noqa: E402
from stresslab.adapters import validate_bars, validate_signals  # noqa: E402
from stresslab.execution_sim import ExecConditions, SimBroker, run_execution  # noqa: E402
from stresslab.loaders import load_bars_csv  # noqa: E402
from stresslab.montecarlo import parameter_sensitivity, trade_sequence_mc  # noqa: E402
from stresslab.overfit import deflated_sharpe, pbo_cscv  # noqa: E402
from stresslab.regression import golden_check, parity_check  # noqa: E402
from stresslab.suite import StressSuite  # noqa: E402

PARAMS = {"lookback": 55, "atr_len": 14, "stop_atr": 2.0, "target_atr": 3.0}


def _suite(buggy, golden_dir):
    sc = LeakyScanner if buggy else DonchianScanner
    return StressSuite(SloppyPersonality() if buggy else DemoPersonality(), sc(**PARAMS),
                       lambda: FixedFractionalExecutor(), SimpleJournal, scanner_factory=sc,
                       scanner_params=PARAMS, golden_dir=golden_dir, sensitivity_runs=6, verbose=False)


@pytest.fixture(scope="module")
def good_run(tmp_path_factory):
    return _suite(False, tmp_path_factory.mktemp("golden")).run_all()


@pytest.fixture(scope="module")
def buggy_run(tmp_path_factory):
    return _suite(True, tmp_path_factory.mktemp("golden")).run_all()


def _status(scorecard, layer, check):
    row = scorecard[(scorecard["layer"] == layer) & (scorecard["check"] == check)]
    assert len(row) == 1, f"missing {layer}/{check}"
    return row["status"].iloc[0]


def _rw(seed, n=3000):
    return g.to_bars(g.gbm_returns(n, seed=seed), seed=seed + 1)


# ------------------------------------------------------------- suite level
def test_good_stack_has_no_fail(good_run):
    scorecard = good_run[0]
    fails = scorecard[scorecard["status"] == "FAIL"]
    assert fails.empty, fails.to_string()


def test_buggy_stack_fails_lookahead_and_personality_faults(buggy_run):
    sc = buggy_run[0]
    assert _status(sc, "invariants", "scanner_no_lookahead") == "FAIL"
    assert _status(sc, "invariants", "personality_no_lookahead") == "FAIL"
    for name in F.MUST_DETECT:
        assert _status(sc, "data_faults", f"personality:detect:{name}") == "FAIL", name


def test_suite_returns_documented_shapes(good_run):
    scorecard, scenarios, mc, sens = good_run
    assert list(scorecard.columns) == ["layer", "check", "status", "detail"]
    assert set(scorecard["status"]) <= {"PASS", "WARN", "FAIL"}
    assert len(scenarios) == 10
    assert mc["n_trades"] == 200
    assert 0 <= sens["cliff_ratio"] <= 1


# ------------------------------------------------------------- generators and broker
def test_random_walk_null_mean_is_spread_cost_only():
    rs = []
    for k in range(10):
        b = _rw(300 + k)
        t, _ = run_execution(DonchianScanner().scan(b), b, FixedFractionalExecutor(), ExecConditions(), seed=k)
        rs += t["r_multiple"].tolist()
    r = np.array(rs)
    assert len(r) > 500
    t_stat = r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))
    assert -0.15 < r.mean() < 0.05, r.mean()
    assert t_stat < 2


def test_bracket_hits_target_about_40pct_on_zero_spread_random_walk():
    hits = []
    for k in range(20):
        b = _rw(100 + k)
        t, _ = run_execution(DonchianScanner().scan(b), b, FixedFractionalExecutor(),
                             ExecConditions(spread_mult=0.0), seed=k)
        t = t[t["exit_reason"] != "end_of_data"]
        hits += (t["exit_reason"] == "target").tolist()
    rate = float(np.mean(hits))
    assert len(hits) > 800
    assert abs(rate - 0.40) <= 0.05, rate


def test_to_bars_envelope_and_continuity():
    b = _rw(1, 500)
    assert validate_bars(b) == []
    assert np.allclose(b["open"].to_numpy()[1:], b["close"].to_numpy()[:-1])


def test_regime_switching_single_regime():
    r, labels = g.regime_switching_returns(200, regimes=[g.regime_by_name("range")], seed=1)
    assert len(r) == 200 and set(labels) == {"range"}
    r2, labels2 = g.regime_switching_returns(2000, seed=1)
    assert len(set(labels2)) > 1


def _flat_bars(prices_ohlc):
    idx = pd.date_range("2024-01-01", periods=len(prices_ohlc), freq="1h", tz="UTC")
    df = pd.DataFrame(prices_ohlc, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    df["spread"] = 0.0
    return df


def test_gap_through_stop_fills_at_open_and_r_uses_risk_budget():
    bars = _flat_bars([[100, 100, 100, 100], [100, 100.5, 99.5, 100], [100, 100.5, 99.5, 100],
                       [92, 92.5, 91.5, 92], [92, 92, 92, 92]])
    br = SimBroker(bars, ExecConditions(spread_mult=0.0))
    br.step(0)
    br.market_order(1, units=100, stop=99.0, target=103.0, signal_time=bars.index[0], risk_budget=100.0)
    for i in range(1, len(bars)):
        br.step(i)
    t = br.trades().iloc[0]
    assert t["exit_reason"] == "gap_stop"
    assert t["exit"] == pytest.approx(92.0)
    assert t["r_multiple"] == pytest.approx(t["pnl"] / 100.0)
    assert t["r_multiple"] == pytest.approx(-8.0)


def test_fill_beyond_stop_does_not_produce_absurd_r():
    # signal at 100 with stop 99; the market gaps to 98.9 before the fill
    bars = _flat_bars([[100, 100, 100, 100], [98.9, 99.0, 98.8, 98.9], [98.9, 99, 98.8, 98.9]])
    bars["spread"] = 0.02
    br = SimBroker(bars, ExecConditions())
    br.step(0)
    br.market_order(1, units=100, stop=99.0, target=103.0, signal_time=bars.index[0], risk_budget=100.0)
    for i in range(1, len(bars)):
        br.step(i)
    t = br.trades().iloc[0]
    assert "filled_beyond_stop" in br.event_counts()
    assert -1.0 < t["r_multiple"] < 0
    assert t["r_multiple"] == pytest.approx(t["pnl"] / t["risk_budget"])


def test_intrabar_stop_checked_before_target():
    bars = _flat_bars([[100, 100, 100, 100], [100, 104, 98, 100], [100, 100, 100, 100]])
    br = SimBroker(bars, ExecConditions(spread_mult=0.0))
    br.step(0)
    br.market_order(1, units=1, stop=99.0, target=103.0, signal_time=bars.index[0], risk_budget=1.0)
    for i in range(1, len(bars)):
        br.step(i)
    assert br.trades().iloc[0]["exit_reason"] == "stop"


def test_naked_order_logged_and_journal_reconciles():
    bars = _rw(5, 300)
    br = SimBroker(bars)
    br.market_order(1, units=10, stop=float("nan"), target=float("nan"), signal_time=bars.index[0], risk_budget=1)
    assert br.event_counts()["naked_order"] == 1
    j = SimpleJournal()
    t, broker = run_execution(DonchianScanner().scan(bars), bars, FixedFractionalExecutor(), seed=1, journal=j)
    assert j.summary()["n_trades"] == len(t)
    assert j.summary()["net_pnl"] == pytest.approx(broker.equity - broker.start_equity, abs=0.01)


# ------------------------------------------------------------- adapters and faults
@pytest.mark.parametrize("name", F.MUST_DETECT)
def test_must_detect_faults_raise_in_good_components(name):
    bad = F.apply_fault(name, _rw(7, 600), seed=1)
    with pytest.raises(ValueError):
        DemoPersonality().assess(bad)
    with pytest.raises(ValueError):
        DonchianScanner().scan(bad)


@pytest.mark.parametrize("name", F.MUST_SURVIVE)
def test_must_survive_faults_run(name):
    bad = F.apply_fault(name, _rw(7, 600), seed=1)
    out = DemoPersonality().assess(bad)
    assert all(np.isfinite(v) for v in out.values())
    assert validate_signals(DonchianScanner().scan(bad)) == []


def test_validate_signals_catches_bad_rows():
    t = pd.Timestamp("2024-01-01", tz="UTC")
    df = pd.DataFrame({"time": [t] * 4, "side": [1, -1, 0, 1], "entry": [1.0, 1.0, 1.0, np.nan],
                       "stop": [1.1, 0.9, 0.9, 0.9], "target": [1.2, 0.8, 1.1, 1.1]})
    msg = " ".join(validate_signals(df))
    for part in ("stop at or above", "stop at or below", "side not", "NaN"):
        assert part in msg


# ------------------------------------------------------------- invariants on planted bugs
def test_lookahead_checks_catch_planted_bugs_and_pass_good():
    b = _rw(11)
    assert inv.check_scanner_no_lookahead(DonchianScanner(), b).status == "PASS"
    assert inv.check_scanner_no_lookahead(LeakyScanner(), b).status == "FAIL"
    assert inv.check_personality_no_lookahead(DemoPersonality(), b).status == "PASS"
    assert inv.check_personality_no_lookahead(SloppyPersonality(), b).status == "FAIL"


# ------------------------------------------------------------- overfitting stats
def test_pbo_noise_is_high_and_real_edge_is_low():
    rng = np.random.default_rng(1)
    noise = rng.standard_normal((1000, 40))
    assert pbo_cscv(noise)["pbo"] > 0.3
    edge = noise.copy()
    edge[:, 0] += 0.2
    assert pbo_cscv(edge)["pbo"] < 0.1


def test_deflated_sharpe_penalises_many_trials():
    rng = np.random.default_rng(2)
    r = 0.08 + rng.standard_normal(500)
    few = deflated_sharpe(r, n_trials=1)["dsr"]
    many = deflated_sharpe(r, n_trials=500)["dsr"]
    assert many < few
    assert many < 0.5 < few


# ------------------------------------------------------------- monte carlo
def test_shuffle_mc_preserves_expectancy_and_final_equity():
    rng = np.random.default_rng(3)
    r = rng.choice([-1.0, 1.5], size=150, p=[0.6, 0.4])
    mc = trade_sequence_mc(r, n_sims=300, method="shuffle", seed=1)
    assert mc["path_mean_r"] == pytest.approx(r.mean())
    assert mc["final_equity_p5"] == pytest.approx(mc["final_equity_p50"])
    assert mc["max_dd_p95"] >= mc["max_dd_p50"]
    boot = trade_sequence_mc(r, n_sims=300, n_trades=200, method="bootstrap", seed=1)
    assert boot["n_trades"] == 200 and 0 <= boot["prob_ruin"] <= 1


def test_parameter_sensitivity_flags_a_cliff():
    smooth = parameter_sensitivity(lambda p: 10.0 + p["x"] * 0.01, {"x": 50}, n=30, seed=1)
    spike = parameter_sensitivity(lambda p: 10.0 if p["x"] == 50 else -5.0, {"x": 50}, n=30, seed=1)
    assert smooth["cliff_ratio"] == 0
    assert spike["cliff_ratio"] > 0.3


# ------------------------------------------------------------- regression and parity
def test_parity_detects_price_disagreement_and_missing_rows():
    t = pd.date_range("2024-01-01", periods=4, freq="1h", tz="UTC")
    ref = pd.DataFrame({"time": t, "side": [1, -1, 1, 1], "entry": [1.1, 1.2, 1.3, 1.4],
                        "stop": [1.0, 1.3, 1.2, 1.3], "target": [1.3, 1.0, 1.5, 1.6]})
    other = ref.iloc[:3].copy()
    other.loc[1, "entry"] = 1.2005
    res = parity_check(ref, other, price_tol=1e-5)
    assert res["status"] == "FAIL"
    assert len(res["price_mismatch"]) == 1 and res["price_mismatch"][0][1] == "entry"
    assert len(res["only_in_reference"]) == 1
    assert parity_check(ref, ref.copy())["status"] == "PASS"
    shifted = ref.copy()
    shifted["time"] = shifted["time"] + pd.Timedelta(minutes=1)
    assert parity_check(ref, shifted, time_tol=pd.Timedelta(minutes=2))["status"] == "PASS"


def test_golden_check_detects_change(tmp_path):
    assert golden_check("x", {"a": 1.0}, tmp_path).status == "WARN"
    assert golden_check("x", {"a": 1.0 + 1e-9}, tmp_path).status == "PASS"
    assert golden_check("x", {"a": 1.1}, tmp_path).status == "FAIL"
    df = DonchianScanner().scan(_rw(3, 800))
    golden_check("sig", df, tmp_path)
    assert golden_check("sig", df, tmp_path).status == "PASS"
    assert golden_check("sig", df.iloc[1:], tmp_path).status == "FAIL"


def test_trial_log_counts_and_variance(tmp_path):
    p = tmp_path / "trials.csv"
    for k, s in enumerate([0.1, 0.3, 0.2]):
        trial_log.log_trial(p, "donchian", {"lookback": 50 + k}, s, 1000)
    trial_log.log_trial(p, "donchian", {"lookback": 50}, 0.1, 1000, note="rerun")
    assert trial_log.trial_count(p) == 3
    assert trial_log.trial_count(p, distinct=False) == 4
    assert trial_log.sharpe_variance(p) > 0


def test_loader_converts_server_time_and_requires_offset(tmp_path):
    p = tmp_path / "mt5.csv"
    p.write_text("<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\n"
                 "2024.01.02\t02:00:00\t1.1\t1.2\t1.0\t1.15\t10\n"
                 "2024.01.02\t03:00:00\t1.15\t1.2\t1.1\t1.12\t12\n")
    cmap = {"time": ["<DATE>", "<TIME>"], "open": "<OPEN>", "high": "<HIGH>", "low": "<LOW>",
            "close": "<CLOSE>", "volume": "<TICKVOL>"}
    bars = load_bars_csv(p, cmap, server_utc_offset_hours=2, time_format="%Y.%m.%d %H:%M:%S", sep="\t")
    assert bars.index[0] == pd.Timestamp("2024-01-02 00:00", tz="UTC")
    with pytest.raises(ValueError):
        load_bars_csv(p, cmap, server_utc_offset_hours=None, time_format="%Y.%m.%d %H:%M:%S", sep="\t")
