"""Unit tests for the Empire Game Market Personality engine (empire/ and personality.py)."""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from empire import events as ev  # noqa: E402
from empire import journal as jr  # noqa: E402
from empire import levels as lv  # noqa: E402
from empire import pairs as pr  # noqa: E402
from empire import players as pl  # noqa: E402
from empire import stories as st  # noqa: E402
from empire import traits as tr  # noqa: E402
from empire.config import load_config, load_yaml  # noqa: E402
from empire.data import trade_dates  # noqa: E402


def rw(n=3000, seed=0, sd=0.01):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2010-01-01", periods=n)
    return pd.Series(rng.normal(0, sd, n), idx)


# ---------------------------------------------------------------- traits

def test_rolling_ols_matches_lstsq():
    rng = np.random.default_rng(1)
    n = 400
    X = pd.DataFrame({"a": rng.normal(0, 1, n), "b": rng.normal(0, 0.05, n)})
    y = pd.Series(0.3 + 2 * X["a"] - 5 * X["b"] + rng.normal(0, 0.1, n))
    out = tr.rolling_ols(y, X, 250, full=True)
    Xm = np.column_stack([np.ones(250), X.values[-250:]])
    b, *_ = np.linalg.lstsq(Xm, y.values[-250:], rcond=None)
    assert out["a"].iloc[-1] == pytest.approx(b[1], rel=1e-8)
    assert out["b"].iloc[-1] == pytest.approx(b[2], rel=1e-8)
    assert out["const"].iloc[-1] == pytest.approx(b[0], rel=1e-8)
    resid = y.values[-250:] - Xm @ b
    assert out["resid_sd"].iloc[-1] == pytest.approx(math.sqrt(resid @ resid / (250 - 3)), rel=1e-6)


def test_variance_ratio_near_one_for_random_walk_and_above_for_trend():
    r = rw(3000)
    assert tr.variance_ratio(r, 20, 2000).iloc[-1] == pytest.approx(1.0, abs=0.35)
    trend = r.rolling(10).mean().fillna(0) + r * 0.1     # persistent returns
    assert tr.variance_ratio(trend, 20, 2000).iloc[-1] > 2


def test_half_life_of_ar1_gap():
    rng = np.random.default_rng(3)
    n, phi = 3000, 0.95                                 # true half life ln2 / -ln(0.95) ~ 13.5 days
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + rng.normal(0, 0.01)
    level = pd.Series(np.exp(x), pd.bdate_range("2010-01-01", periods=n))
    hl = 10 ** tr.half_life(level, 200, 2000, additive=False).iloc[-1]
    assert 8 < hl < 25


def test_asymmetry_above_one_when_falls_are_sharper():
    r = pd.Series([0.01, 0.01, 0.01, -0.03] * 100)
    assert tr.asymmetry(r, 200).iloc[-1] > 1.5


def test_bands_use_percentiles_of_own_history():
    s = pd.DataFrame({"energy": np.r_[np.linspace(0, 1, 500), [0.99]]})
    b = tr.trait_bands(s, 5040, {"core": [25, 75], "stretched": [10, 90]})
    assert b["energy"]["band"] == "out of character"
    s2 = pd.DataFrame({"energy": np.r_[np.linspace(0, 1, 500), [0.5]]})
    assert tr.trait_bands(s2, 5040, {})["energy"]["band"] == "core"


def test_solo_index_identity_pair_equals_scaled_gap():
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2015-01-01", periods=300)
    majors = ("USD", "EUR", "JPY", "GBP", "CHF", "AUD", "NZD", "CAD")
    pairs = {}
    for c in majors[1:]:
        p = np.exp(np.cumsum(rng.normal(0, 0.005, 300)))
        pairs[f"{c}USD" if c in ("EUR", "GBP", "AUD", "NZD") else f"USD{c}"] = pd.Series(p, idx)
    logv = tr.currency_log_values(pairs, majors)
    c = tr.solo_indices(logv)
    r_audjpy = np.log(tr.cross_from_legs(logv, "AUD", "JPY")).diff()
    N = len(majors)
    assert np.allclose(r_audjpy.dropna(), ((N - 1) / N * (c["AUD"] - c["JPY"])).dropna())


def test_drift_streak_counts_trailing_run():
    d = pd.Series([0, 3, 3, -3, 2.5, 2.6, 2.7])
    assert tr.drift_streak(d, 2.0) == (3, 1)


# ---------------------------------------------------------------- levels

def bars_from(closes, spread=0.0005):
    c = np.asarray(closes, float)
    idx = pd.bdate_range("2020-01-01", periods=len(c))
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread, "close": c}, index=idx)


def test_zone_touch_reaction_and_break():
    # outside 6 bars above, touch 1.1000, react up 2 ATR; then leave, come back, break below for 2 closes
    path = [1.1060] * 6 + [1.1002, 1.1030, 1.1060, 1.1080] + [1.1080] * 6 + [1.1001, 1.0980, 1.0975, 1.0970] + [1.0970] * 12
    b = bars_from(path)
    n = len(b)
    atr = np.full(n, 0.0020)
    w = np.full(n, 0.0005)
    z = lv.score_zone(lv.Zone(1.1000, "round_mid", 0.0005), b["high"].values, b["low"].values, b["close"].values, atr, w,
                      b.index, None, {"touch_outside_bars": 5, "reaction_bars": 10, "reaction_atr": 1.0, "break_closes": 2})
    assert [t.outcome for t in z.touches] == ["reaction", "break"]
    assert z.respect == pytest.approx((1 + 1) / (2 + 2))       # (R + 1) / (N + 2)


def test_zone_ignores_touches_before_it_was_known():
    path = [1.1060] * 6 + [1.1002, 1.1030, 1.1060, 1.1080] + [1.1080] * 12
    b = bars_from(path)
    atr, w = np.full(len(b), 0.002), np.full(len(b), 0.0005)
    z = lv.score_zone(lv.Zone(1.1000, "market", 0.0005, born_i=10), b["high"].values, b["low"].values, b["close"].values,
                      atr, w, b.index, None, {})
    assert z.n == 0


def test_round_levels_and_families():
    lvls = lv.round_levels(1.04, 1.16, 0.05, 0.025)
    assert (1.05, "round_major") in lvls and (1.075, "round_mid") in lvls and (1.10, "round_major") in lvls


def test_edge_z_formula():
    z = lv.Zone(1.0, "round_major", 0.001)
    z.touches = [lv.Touch(i, pd.Timestamp("2020-01-01"), "above", "reaction", 1.0) for i in range(18)] + \
                [lv.Touch(i, pd.Timestamp("2020-01-01"), "above", "break", 0.0) for i in range(2)]
    p = (18 + 1) / (20 + 2)
    assert z.edge_z(0.5) == pytest.approx((p - 0.5) / math.sqrt(0.25 / 20))


def test_base_rates_sum_to_one_and_touch_probability_is_monotone():
    r = rw(2000, 7, 0.006)
    c = 1.1 * np.exp(r.cumsum())
    b = pd.DataFrame({"open": c, "high": c * 1.002, "low": c * 0.998, "close": c})
    sigma = r.rolling(20).std() * math.sqrt(252)
    exc = lv.forward_excursions(b, sigma, 20, False)
    pi = lv.outcome_base_rates(exc, 1.0, 1.0)
    assert pi["up"] + pi["down"] + pi["range"] == pytest.approx(1.0)
    assert lv.touch_probability(exc, 0.5) > lv.touch_probability(exc, 1.5) > lv.touch_probability(exc, 3.0)
    assert lv.touch_probability_formula(0.0) == pytest.approx(1.0)


# ---------------------------------------------------------------- likelihood, EV, grades (doc worked example)

def test_likelihood_equals_base_rates_without_incentives_and_tilts_with_them():
    pi = {"up": 0.3, "down": 0.3, "range": 0.4}
    out = st.likelihood(pi, {"up": 0, "down": 0, "range": 0}, 0.0, 1.0, 1.0)["P"]
    assert out == pytest.approx(pi)
    tilted = st.likelihood(pi, {"up": 0.5, "down": -0.5, "range": 0}, 0.0, 1.0, 1.0)["P"]
    assert tilted["up"] > 0.3 > tilted["down"]
    hot = st.likelihood(pi, {"up": 0.5, "down": -0.5, "range": 0}, 0.0, 1.0, 1000.0)["P"]
    assert hot["up"] == pytest.approx(0.3, abs=1e-3)              # high T stays near the base rates


def test_doc_worked_example_grades_c():
    # Doc: P_dir 0.62, reach 0.55, hold 0.60 -> P_opp ~0.20; EV at entry ~0.44 < 0.5 -> grade C
    p_opp = 0.62 * 0.55 * 0.60
    assert p_opp == pytest.approx(0.2046, abs=1e-3)
    evx = st.ev_entry(0.62, 0.60, 3.0, 1.0, 0.05)
    assert evx == pytest.approx(0.44, abs=0.01)
    card = {"pressure_pct": 95, "p_dir": 0.62, "ev_entry": evx, "reward_r": 3.0, "trade_type": "alignment",
            "story_push_agree": True, "needle_pass": True}
    assert st.grade(card, {})["grade"] == "C"
    card["ev_entry"] = 0.6
    assert st.grade(card, {})["grade"] == "A"
    card["p_dir"] = 0.57
    assert st.grade(card, {})["grade"] == "B"
    card["needle_pass"] = False
    assert st.grade(card, {})["grade"] == "none"


def test_doc_worked_example_event_weight_and_pressure():
    # ECB decision: 0.18 x 1 x 0.8 x 0.9 ~ 0.13; exposure +0.7 -> ~0.09; half every 10 days
    E = 0.18 * 1 * 0.8 * 0.9
    dates = pd.bdate_range("2026-10-01", periods=21)
    p = ev.pressure_series([{"date": dates[0], "E": E, "x": 0.7, "h": 10}], dates)
    assert p.iloc[0] == pytest.approx(0.0907, abs=1e-3)
    assert p.iloc[10] == pytest.approx(p.iloc[0] / 2, rel=1e-6)


def test_gates_and_alignment_cells():
    card = {"favored": "up", "top_incentive": {"used": True, "gains_from": "up", "player": "US", "id": "x"},
            "character": {"band": "core"}, "at_zone": True, "location": {"in_proven_zone": True},
            "window": {"open": True, "event_inside_stop": False}}
    g = st.gate_results(card, {})
    assert g["_count"] == 4 and g["_status"] == "candidate"
    sc = {"rows": [{"symbol": "EURUSD", "dir": 1, "status": "qualified"}]}
    assert st.alignment_cell(1, sc, "EURUSD")["cell"] == "aligned"
    assert st.alignment_cell(0, sc, "EURUSD")["cell"] == "tactical"
    assert st.alignment_cell(-1, sc, "EURUSD")["cell"] == "conflict"
    assert st.alignment_cell(1, sc, "GBPUSD")["cell"] == "watch"
    assert st.alignment_cell(0, sc, "GBPUSD")["cell"] == "no trade"


def test_generic_scanner_adapter(tmp_path):
    p = tmp_path / "out" / "scan_x.csv"
    p.parent.mkdir()
    p.write_text("symbol,direction,status\nEURUSD,long,qualified\nUSDJPY,short,developing\n", encoding="utf-8")
    sc = st.load_scanner({"name": "x", "path": "out/scan_*.csv", "adapter": "generic_csv"}, tmp_path, pd.Timestamp.now())
    assert sc["status"] == "ok" and [(r["symbol"], r["dir"]) for r in sc["rows"]] == [("EURUSD", 1)]


def test_exposure_check_flags_one_dollar_bet_placed_twice():
    cards = [{"symbol": "EURUSD", "grade": "A", "favored": "down", "usd_sign": -1, "top_incentive": {"player": "US"}},
             {"symbol": "USDJPY", "grade": "A", "favored": "up", "usd_sign": 1, "top_incentive": {"player": "US"}},
             {"symbol": "AUDUSD", "grade": "B", "favored": "down", "usd_sign": -1, "top_incentive": {"player": "US"}}]
    out = st.exposure_check(cards, cap=2.0, single_player_cap=2.0)
    assert out["net_usd"] == pytest.approx(2.5) and len(out["flags"]) == 2


# ---------------------------------------------------------------- players, events, pairs

def test_incentive_score_and_evidence_exclusion():
    raw = {"players": {"JP": {"weight": 0.08}, "US": {"weight": 0.30}},
           "incentives": [
               {"id": "a", "player": "JP", "instrument": "USDJPY", "gains_from": "down", "B": 4, "K": 4, "U": 3, "C": 3,
                "evidence": "derived intelligence"},
               {"id": "b", "player": "US", "instrument": "USDJPY", "gains_from": "up", "B": 5, "K": 5, "U": 5, "C": 1,
                "evidence": "unverified"}]}
    led = pl.parse_ledger(raw)
    assert led.incentives[0].score == pytest.approx(16.0)        # 4 x 4 x 3 / 3
    W = pl.incentive_weights(led, "USDJPY")
    c = 16 / 125 * (0.08 / 0.30)
    assert W["W"]["down"] == pytest.approx(c) and W["W"]["up"] == pytest.approx(-c)   # unverified US entry excluded
    with pytest.raises(ValueError):
        pl.parse_ledger({"players": {"JP": {}}, "incentives": [{"player": "JP", "instrument": "X", "gains_from": "up",
                                                                 "B": 7, "evidence": "unknown"}]})


def test_plan_credibility_and_alignment_score():
    raw = {"players": {"EU": {"weight": 0.18}},
           "plans": [{"id": "P", "player": "EU", "funding_ratio": 0.8, "delivery_ratio": 0.5,
                      "goals": [{"id": "G1", "goal": "x"}, {"id": "G2", "goal": "y", "delivery_ratio": 1.0}]}]}
    led = pl.parse_ledger(raw)
    assert led.goals["G1"].credibility == pytest.approx(0.4) and led.goals["G2"].credibility == pytest.approx(0.8)
    evs = pd.DataFrame({"date": [pd.Timestamp("2026-09-01"), pd.Timestamp("2026-09-15"), pd.Timestamp("2026-01-01")],
                        "plan_effects": [{"G1": 1}, {"G1": 1}, {"G1": -1}]})
    a = pl.alignment_scores(led, evs, pd.Timestamp("2026-10-01"))
    assert a["G1"]["sum"] == 2 and a["G1"]["A"] == pytest.approx(0.4 * 0.18 * 2)   # the January event is outside 90 days


def test_leverage_score_unknown_input_not_computed():
    assert pl.leverage_score({"dependence": 0.5, "supply_share": 0.6, "substitution_years": 9, "credibility": 1}) == pytest.approx(1.5)
    assert pl.leverage_score({"dependence": 0.5, "supply_share": None, "substitution_years": 3, "credibility": 1}) is None


def test_hypothesis_statuses():
    h = {"id": "H", "expect": ">0"}
    assert pl.evaluate_hypothesis(h, 0.1, 0.2, {})["status"] == "validated"
    assert pl.evaluate_hypothesis(h, -0.1, 0.2, {})["status"] == "regime-dependent"
    assert pl.evaluate_hypothesis(h, -0.1, -0.2, {})["status"] == "invalidated"
    assert pl.evaluate_hypothesis(h, None, None, {})["status"] == "inconclusive"
    assert pl.check_expect(0.5, "between 0.2, 0.7")


def test_event_study_measures_a_jump():
    rng = np.random.default_rng(9)
    idx = pd.bdate_range("2024-01-01", periods=400)
    f = pd.Series(rng.normal(0, 0.01, 400), idx)
    r = 0.5 * f + pd.Series(rng.normal(0, 0.002, 400), idx)
    day0 = idx[300]
    r.loc[day0] += 0.02                                          # ~10 abnormal sd
    out = ev.event_study(r, pd.DataFrame({"f": f}), day0, {})
    assert out["shock"] > 5 and out["complete"]


def test_fingerprint_picks_the_matching_template():
    basket = ["WTI", "XAUUSD", "SPX", "VIX"]
    tmpl = {"war_supply": {"WTI": 1, "XAUUSD": 1, "SPX": -1, "VIX": 1}, "growth_scare": {"WTI": -1, "SPX": -1, "VIX": 1}}
    fp = ev.fingerprint({"WTI": 3.0, "XAUUSD": 2.0, "SPX": -2.5, "VIX": 2.8}, basket, tmpl)
    assert fp["best"] == "war_supply" and fp["best_sim"] > 0.9


def test_needle_test_needs_both_parts():
    led = pl.parse_ledger({"players": {"US": {"weight": 0.3}}, "lever_defaults": {"MON": {"value": 0.8, "evidence": "working assumption"}}})
    e = {"player": "US", "lever": "MON", "headline": "FOMC statement", "type": "central_bank"}
    assert ev.needle_test(e, {"shock_max": 2.5, "shock_max_on": "DXY"}, led, {}, False, False, 0.75, {})["pass"]
    assert not ev.needle_test(e, {"shock_max": 0.8, "shock_max_on": "DXY"}, led, {}, False, False, 0.75, {})["pass"]


def test_incentive_gap_and_closure_rate():
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2015-01-01", periods=1500)
    gap = pd.Series(np.cumsum(rng.normal(0, 0.02, 1500)), idx)
    u = np.zeros(1500)
    for t in range(1, 1500):
        u[t] = 0.9 * u[t - 1] + rng.normal(0, 0.003)
    lp = pd.Series(0.5 * gap.values + u, idx)
    out = pr.incentive_gap(lp, pd.DataFrame({"yield_gap": gap}), 630, history_days=1)
    assert out["status"] == "trusted" and abs(out["betas"]["yield_gap"] - 0.5) < 0.05
    G = pd.Series([0.0, 2.5, 2.2, 1.0, 0.2, 0.0] + [0.0] * 100)
    assert pr.gap_closure_rate(G)["rate"] == 1.0


# ---------------------------------------------------------------- journal

def test_forecast_resolution_and_scoring(tmp_path):
    fc = pd.DataFrame([{"forecast_id": "f1", "instrument": "EURUSD", "made_date": "2026-01-01", "expiry_date": "2026-01-30",
                        "zone_up_edge": "1.11", "zone_down_edge": "1.09", "P_up": "0.6", "P_down": "0.3", "P_range": "0.1",
                        "favored": "up", "status": "open", "outcome": "", "resolved_date": "", "brier": "", "logloss": "", "hit": ""}])
    idx = pd.bdate_range("2026-01-02", periods=5)
    b = pd.DataFrame({"high": [1.100, 1.105, 1.112, 1.10, 1.10], "low": [1.095] * 5, "close": [1.1] * 5}, index=idx)
    out, res = jr.resolve_forecasts(fc, {"EURUSD": b}, pd.Timestamp("2026-01-10"))
    assert res and out.loc[0, "outcome"] == "up" and out.loc[0, "resolved_date"] == str(idx[2].date())
    assert float(out.loc[0, "brier"]) == pytest.approx(0.4 ** 2 + 0.3 ** 2 + 0.1 ** 2)


def test_params_change_control_and_ledger_diff(tmp_path):
    j = jr.Journal(tmp_path)
    stt = j.params({"T": 2.0})
    j.change_param(stt, "T", 1.0, "calibration", "n=40", "2026-10-01")
    j.save_params(stt)
    log = j.read("params_changelog.csv")
    assert stt["version"] == "params-2" and log.iloc[0]["new"] == "1.0"
    a = {"players": {"US": {"weight": 0.3}}, "incentives": [{"id": "x", "B": 3}]}
    b = {"players": {"US": {"weight": 0.25}}, "incentives": [{"id": "x", "B": 4}]}
    d = jr.diff_paths(a, b)
    assert any("players.US.weight" in x for x in d) and any("incentives[x].B" in x for x in d)


def test_trade_dates_put_new_york_evening_bars_on_next_day():
    # a daily FX bar opening 17:00 New York (21:00 UTC in summer) belongs to the next day's session
    assert trade_dates([pd.Timestamp("2026-07-06 21:00", tz="UTC")])[0] == pd.Timestamp("2026-07-07")
    assert trade_dates([pd.Timestamp("2026-07-07 13:30", tz="UTC")])[0] == pd.Timestamp("2026-07-07")


def test_shipped_config_and_ledger_parse():
    cfg = load_config(ROOT / "personality_config.yaml")
    led = pl.parse_ledger(load_yaml(ROOT / cfg["paths"]["ledger"]))
    assert abs(sum(led.player_weight(p) for p in led.players) - 1.0) < 1e-9     # the nine weights sum to 1
    raw = load_yaml(ROOT / cfg["paths"]["events"])
    assert len(ev.load_owner_events(raw["events"])) >= 1


def test_demo_end_to_end(tmp_path):
    """Full daily + monthly + quarterly run on synthetic data in a temporary copy of the config."""
    import shutil

    import personality
    for f in ("personality_config.yaml", "personality_ledger.yaml", "personality_events.yaml"):
        shutil.copy(ROOT / f, tmp_path / f)
    paths = personality.run_once(tmp_path / "personality_config.yaml", pd.Timestamp("2026-10-06"), "quarterly", True)
    assert len(paths) == 3 and all(p.exists() for p in paths)
    rep = json.loads(paths[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert rep["regime"]["regime"] and rep["cards"] and rep["forecasts_written"] > 0


def test_last_completed_session_skips_unfinished_day_and_weekend():
    from empire.data import last_completed_session
    assert last_completed_session(pd.Timestamp("2026-10-07 14:00", tz="UTC")) == pd.Timestamp("2026-10-06")  # 10:00 New York
    assert last_completed_session(pd.Timestamp("2026-10-07 22:30", tz="UTC")) == pd.Timestamp("2026-10-07")
    assert last_completed_session(pd.Timestamp("2026-10-10 12:00", tz="UTC")) == pd.Timestamp("2026-10-09")  # Saturday


def test_personality_change_is_one_row_per_instrument_with_the_trait_mix(tmp_path):
    from types import SimpleNamespace
    from empire.monitor import personality_changes
    j = jr.Journal(tmp_path)
    # a legacy journal: gold logged once per trait
    j.append("personality_changes.csv", [
        {"date": "2026-10-07", "symbol": "XAUUSD", "trait": t, "direction": "higher", "drift": 2.5, "days": 120,
         "status": "open", "suspected_cause": "not identified: investigate", "closed": ""}
        for t in ("energy", "energy_long", "atr_pct")])
    ctx = SimpleNamespace(cfg={"traits": {"drift_threshold": 2.0, "drift_days": 60}}, journal=j,
                          asof=pd.Timestamp("2026-10-08"))
    idx = pd.bdate_range("2026-01-01", periods=200)
    high = pd.Series(2.5, idx)
    drift = pd.DataFrame({"energy": high, "energy_long": high, "atr_pct": high, "conviction": pd.Series(0.1, idx)})
    out = personality_changes(ctx, "XAUUSD", drift, None)
    log = j.read("personality_changes.csv")
    assert len(log) == 1 and len(out) == 1
    assert log.iloc[0]["trait"] == "energy, energy_long, atr_pct"
    assert log.iloc[0]["date"] == "2026-10-07" and log.iloc[0]["status"] == "open"
    assert personality_changes(ctx, "XAUUSD", drift, None) == []  # same mix: nothing new
    # one trait comes back inside the band: still one row, smaller mix
    drift.loc[idx[-1], "atr_pct"] = 0.0
    personality_changes(ctx, "XAUUSD", drift, "event X")
    log = j.read("personality_changes.csv")
    assert len(log) == 1 and log.iloc[0]["trait"] == "energy, energy_long"
    assert log.iloc[0]["suspected_cause"] == "event X"
    # all back inside: the one row closes
    drift.loc[idx[-1]] = 0.0
    out = personality_changes(ctx, "XAUUSD", drift, None)
    log = j.read("personality_changes.csv")
    assert len(log) == 1 and log.iloc[0]["status"] == "closed" and out[0]["status"] == "closed"
