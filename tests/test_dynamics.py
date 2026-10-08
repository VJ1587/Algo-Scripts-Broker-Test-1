"""Tests for dynamics.py: the ten checks in Build Instructions v0.1 Section 8 plus the check values.
Run: python -m pytest -q tests/test_dynamics.py"""
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dynamics as dy  # noqa: E402
import scanner as sc  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = sc.load_config(ROOT / "scanner_config.yaml")
DCFG = dy.dyn_cfg(CFG)
UNIVERSE = {i.symbol: i for i in sc.build_universe(CFG)}
ASOF = pd.Timestamp("2026-10-02T12:05Z")


def walk(n, start=1.2, vol=0.004, seed=1, freq="4h", start_time="2024-01-01"):
    rng = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(rng.normal(0, vol, n)))
    o = np.r_[start, c[:-1]]
    sp = np.abs(rng.normal(0, vol * 0.5, n)) * c
    idx = pd.date_range(start_time, periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + sp, "low": np.minimum(o, c) - sp, "close": c}, index=idx)


def unit_range_bars(closes, highs=None, lows=None, freq="1D"):
    """Bars whose true range is exactly 1 unless highs/lows are given, so ATR14 is 1.0 from bar 14 on."""
    c = np.asarray(closes, float)
    h = c + 0.5 if highs is None else np.asarray(highs, float)
    l = c - 0.5 if lows is None else np.asarray(lows, float)
    idx = pd.date_range("2025-01-01", periods=len(c), freq=freq, tz="UTC")
    return pd.DataFrame({"open": c, "high": h, "low": l, "close": c}, index=idx)


# --------------------------------------------------------------- Section 2: shared definitions
def test_atr_and_ema_match_scanner():
    df = walk(300)
    assert np.allclose(dy.atr(df, 14), sc.atr(df, 14), equal_nan=True)
    assert np.allclose(dy.ema(df["close"].values, 20), sc.ema(df["close"].values, 20), equal_nan=True)


def test_smoothed_rate_three_of_three_is_080():      # Section 8 test 6
    assert dy.smoothed_rate(3, 3) == pytest.approx(0.80)
    assert dy.smoothed_rate(0, 0) == pytest.approx(0.50)


def test_shrink_thin_and_full():                     # Section 8 test 7 (the formula)
    used, thin = dy.shrink(1.0, 10, 2.0, k=20, min_n=30)
    assert thin and used == pytest.approx((10 * 1.0 + 20 * 2.0) / 30)
    used, thin = dy.shrink(1.0, 30, 2.0, k=20, min_n=30)
    assert not thin and used == 1.0
    assert dy.shrink(None, 0, 2.0) == (None, True)


def test_pr_rank_and_bands():
    v = np.arange(1, 201, dtype=float)
    assert dy.pr_rank(v, 756, 100) == pytest.approx(1.0)
    assert dy.pr_rank(np.r_[v, 100.5], 756, 100) == pytest.approx(101 / 201)
    assert dy.pr_rank(v[:50], 756, 100) is None
    bands = DCFG["bands"]
    assert dy.band_of(0.5, bands) == "in character" and dy.band_of(0.85, bands) == "stretched"
    assert dy.band_of(0.95, bands) == "out of character" and dy.band_of(None, bands) == "unavailable"


# --------------------------------------------------------------- S1 swings
def test_s1_constructed_series_one_swing_high_at_right_bar():   # Section 8 test 3
    rise = [100 + 0.2 * i for i in range(21)]              # bar 20 is the peak close (high = peak + 0.5)
    peak = rise[-1]
    closes = rise + [peak - 0.8, peak - 1.6] + [peak - 1.6] * 10
    highs = [c + 0.5 for c in rise] + [peak, peak - 0.8] + [peak - 1.1] * 10
    lows = [c - 0.5 for c in rise] + [peak - 1.0, peak - 1.8] + [peak - 2.1] * 10
    df = unit_range_bars(closes, highs, lows)
    a = dy.atr(df, 14)
    assert np.allclose(a[14:], 1.0)
    sw = dy.vol_swings(df["high"].values, df["low"].values, df["close"].values, a, theta=1.5)
    highs_found = [s for s in sw if s.kind == "H"]
    assert len(highs_found) == 1
    s = highs_found[0]
    assert s.k == 20 and s.confirm == 22 and s.price == pytest.approx(peak + 0.5)
    assert [x.kind for x in sw] == ["L", "H"]            # the opening rise confirms the first swing low


def test_s1_never_early_and_never_moves():                       # Section 8 test 2
    df = walk(600, seed=3)
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    a = dy.atr(df, 14)
    full = dy.vol_swings(h, l, c, a, 1.5)
    assert len(full) > 10 and all(s.confirm >= s.k + 2 for s in full)
    for s in full[:8]:
        before = dy.vol_swings(h[:s.confirm], l[:s.confirm], c[:s.confirm], a[:s.confirm], 1.5)
        assert (s.kind, s.k) not in [(x.kind, x.k) for x in before]          # not emitted before its confirming close
        at = dy.vol_swings(h[:s.confirm + 1], l[:s.confirm + 1], c[:s.confirm + 1], a[:s.confirm + 1], 1.5)
        assert (s.kind, s.k, s.price, s.confirm) == (at[-1].kind, at[-1].k, at[-1].price, at[-1].confirm)
    n = 400
    part = dy.vol_swings(h[:n], l[:n], c[:n], a[:n], 1.5)
    assert part == [s for s in full if s.confirm < n]                         # appending bars never moves a swing
    kinds = [s.kind for s in full]
    assert all(x != y for x, y in zip(kinds, kinds[1:]))                       # alternate by construction


def test_theta_clip_and_fallback():
    scfg = DCFG["swing"]
    assert dy.theta_for(0.6, 0.5, scfg) == (pytest.approx(1.8), "")
    assert dy.theta_for(0.1, 0.5, scfg)[0] == 1.0 and dy.theta_for(2.0, 0.5, scfg)[0] == 3.0
    th, flag = dy.theta_for(None, 0.5, scfg)
    assert th == 1.5 and "base used" in flag


# --------------------------------------------------------------- S2, X4, X5 check values  (Section 8 test 4)
def test_s2_check_value():
    sw = [dy.Swing("H", 100.0, 0, 2), dy.Swing("L", 99.0, 4, 6), dy.Swing("H", 101.2, 8, 10), dy.Swing("L", 99.3, 12, 14)]
    s2 = dy.structure_score(sw, 1.0)
    assert round(s2["score"], 2) == 0.56 and s2["state"] == "long"
    assert s2["uH"] == pytest.approx(0.834, abs=0.001) and s2["uL"] == pytest.approx(0.291, abs=0.001)
    assert dy.structure_score(sw[:3], 1.0)["state"] == "unavailable"
    down = [dy.Swing("H", 100.0, 0, 2), dy.Swing("L", 99.0, 4, 6), dy.Swing("H", 99.5, 8, 10), dy.Swing("L", 98.0, 12, 14)]
    assert dy.structure_score(down, 1.0)["state"] == "short"


def test_x4_check_value_and_break():
    assert round(dy.fisher_z(0.30, 0.80), 1) == -3.1
    rng = np.random.default_rng(5)
    base = rng.normal(0, 1, 260)
    idx = pd.date_range("2025-01-01", periods=260, freq="B")
    r1 = pd.Series(base, index=idx)
    r2 = pd.Series(np.r_[base[:240], -base[240:]] + rng.normal(0, 0.2, 260), index=idx)
    res = dy.corr_break(r1, r2)
    assert res["rho_long"] > 0.5 and res["rho_short"] < 0 and res["broken"]
    same = dy.corr_break(r1, r1 + rng.normal(0, 0.2, 260))
    assert not same["broken"]


def test_x5_check_value():
    m_usd, score = dy.context_score(1, -0.75, -0.50, 1.0, 0.60)
    assert m_usd == pytest.approx(0.375) and round(score, 3) == 0.465
    m_peer, peers = dy.peer_momentum(1, {"EURUSD": 0.80, "USDJPY": 0.3}, {"EURUSD": 0.60, "USDJPY": -0.9})
    assert m_peer == pytest.approx(0.60) and peers == ["EURUSD"]
    cuts = DCFG["context"]["points"]
    assert dy.context_points(0.465, cuts) == 2 and dy.context_points(0.465, cuts, conflict=True) == 0
    assert [dy.context_points(x, cuts) for x in (0.2, 0.0, -0.2, -0.5)] == [1, 0, -1, -2]
    assert dy.context_points(None, cuts) is None


# --------------------------------------------------------------- P5 retracement profile  (Section 8 test 5)
def _ohlc(closes, freq="4h"):
    c = np.asarray(closes, float)
    idx = pd.date_range("2025-01-01", periods=len(c), freq=freq, tz="UTC")
    return pd.DataFrame({"open": c, "high": c + 0.1, "low": c - 0.1, "close": c}, index=idx)


def _seq(df):
    return sc.alternating_upto(sc.find_pivots(df, 2), len(df) - 1)


def test_p5_undecided_excluded_then_continued():
    path = [10.0] * 16 + [11, 12, 13, 12, 11, 10, 11, 12, 13, 14, 15, 14.8, 14.6, 14.7]
    df = _ohlc(path)
    icfg = CFG["features"]["impulse"]
    obs = dy.enumerate_impulses(_seq(df), df["close"].values, df["high"].values, df["low"].values, dy.atr(df), df.index, icfg)
    assert obs == []                                                  # still undecided at the last bar
    df2 = _ohlc(path + [15.5, 16.0])
    obs = dy.enumerate_impulses(_seq(df2), df2["close"].values, df2["high"].values, df2["low"].values, dy.atr(df2), df2.index, icfg)
    assert len(obs) == 1 and obs[0]["continued"] and obs[0]["direction"] == "long"
    assert obs[0]["rho_max"] == pytest.approx((15.1 - 14.5) / (15.1 - 9.9))
    prof = dy.retracement_profile(obs, [0.382, 0.5, 0.618], [50])
    assert prof["reach"]["0.382"] == pytest.approx(1 / 3) and prof["n_continued"] == 1


def test_p5_reach_monotone_on_random_walk():
    df = walk(3000, seed=11)
    icfg = CFG["features"]["impulse"]
    obs = dy.enumerate_impulses(_seq(df), df["close"].values, df["high"].values, df["low"].values, dy.atr(df), df.index, icfg)
    assert len(obs) >= 10
    prof = dy.retracement_profile(obs, [0.382, 0.5, 0.618], [25, 50, 75, 90])
    r = prof["reach"]
    assert r["0.382"] >= r["0.500"] >= r["0.618"]
    assert all(0 < v < 1 for v in list(r.values()) + list(prof["hold"].values()))


# --------------------------------------------------------------- P6 breaks
def test_p6_break_event_follow_through():
    closes = [100.0] * 30
    df = unit_range_bars(closes)
    a = dy.atr(df)
    sw = [dy.Swing("H", 103.0, 10, 12)]
    c = df["close"].values.copy()
    h, l = df["high"].values.copy(), df["low"].values.copy()
    c[20] = 103.5                                     # close beyond the swing high -> break
    h[20], l[20] = 104.0, 100.0
    c[21:25], h[21:25], l[21:25] = 103.4, 103.9, 102.9  # stays beyond the level afterwards
    h[23] = 103.5 + 1.0 * a[20] + 0.1                  # travels a further ATR before any close back inside
    ev = dy.break_events(c, h, l, a, sw, df.index)
    assert len(ev) == 1 and ev[0]["followed"] and ev[0]["depth"] == pytest.approx(0.5 / a[20])
    c2 = c.copy()
    c2[21] = 102.0                                     # closes back inside first -> not followed
    ev2 = dy.break_events(c2, h, l, a, sw, df.index)
    assert len(ev2) == 1 and not ev2[0]["followed"]
    prof = dy.break_profile(ev + ev2, [0.0, 0.1, 0.25, 0.5], 0.60, 0.5)
    assert prof["phi"]["0.00"] == pytest.approx(2 / 4) and prof["break_depth_min"] == 0.5 and prof["break_depth_min_default"]


# --------------------------------------------------------------- S3 to S7 pieces
def test_tf_mode_hysteresis():
    m = dy.tf_mode_next(None, 0.45, 0.40, 0.50)
    assert m == "normal"
    m = dy.tf_mode_next(m, 0.39, 0.40, 0.50)
    assert m == "noisy"
    assert dy.tf_mode_next(m, 0.45, 0.40, 0.50) == "noisy"           # stays noisy between the thresholds
    assert dy.tf_mode_next(m, 0.50, 0.40, 0.50) == "normal"
    assert dy.tf_mode_next("noisy", None, 0.40, 0.50) == "noisy"


def test_pullback_location_and_nearest_level():
    reach, hold = {"0.382": 0.7, "0.500": 0.5, "0.618": 0.3}, {"0.382": 0.8, "0.500": 0.7, "0.618": 0.6}
    out = dy.pullback_location(1.0, 2.0, 1.5, [0.2, 0.3, 0.4, 0.6, 0.7], [0.382, 0.5, 0.618], reach, hold)
    assert out["dyn_pullback_depth"] == pytest.approx(0.5) and out["dyn_pullback_rank"] == pytest.approx(0.6)
    assert out["dyn_pullback_zone"] == "normal" and out["dyn_nearest_level"] == 0.5
    assert out["dyn_reach"] == 0.5 and out["dyn_hold"] == 0.7
    deep = dy.pullback_location(1.0, 2.0, 1.2, [0.2, 0.3, 0.4, 0.6, 0.7], [0.382, 0.5, 0.618], reach, hold)
    assert deep["dyn_pullback_zone"] == "out of character"


def test_break_significance_and_psych_cross():
    out = dy.break_significance("long", 1.1000, 1.0950, 1.1030, 0.0100, 0.05, 0.25)
    assert out["dyn_break_depth"] == pytest.approx(0.5) and out["dyn_psych_cross"]         # crossed 1.1000 going down
    out = dy.break_significance("short", 1.1000, 1.0950, 1.0930, 0.0100, 0.05, 0.25)
    assert out["dyn_break_depth"] == pytest.approx(-0.5) and not out["dyn_psych_cross"]
    assert dy.break_significance("neutral", None, 1.0, 1.0, 0.01, 0.05, 0.25)["dyn_break_depth"] is None


def test_coil_and_release():
    quiet = unit_range_bars([100.0] * 80)
    out = dy.coil(quiet, DCFG["coil"])
    assert out["dyn_coil_ratio"] == pytest.approx(1.0) and out["flags"] == []
    c = [100.0] * 60 + [100.0] * 19 + [104.0]
    h = [100.5] * 60 + [100.1] * 19 + [104.2]
    l = [99.5] * 60 + [99.9] * 19 + [100.0]
    df = unit_range_bars(c, h, l)
    out = dy.coil(df, DCFG["coil"])
    assert "release" in out["flags"]


# --------------------------------------------------------------- traits: thin, shrinkage, shared underlying
def _raw(n_legs, seed=0, size=2.0):
    rng = np.random.default_rng(seed)
    legs = [{"size": size + rng.normal(0, 0.1), "bars": 5, "eff": 0.6, "date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i)}
            for i in range(n_legs)]
    return {"n_bars_d": 1300, "n_bars_4h": 1000, "flags": [], "natr": 0.01, "noise_4h": 0.6, "noise_d": 0.55,
            "wick_4h": 0.4, "wick_d": 0.4, "theta_4h": 1.5, "theta_d": 1.5, "legs_4h": legs, "legs_d": legs,
            "impulses": [{"direction": "long", "rho_max": 0.5, "continued": True, "bars_to_rho": 4, "date": legs[0]["date"]}] * n_legs,
            "breaks": [{"direction": "long", "depth": 0.2, "followed": True, "date": legs[0]["date"]}] * n_legs,
            "usd_corr_60": -0.5, "usd_corr_250": -0.4}


def _bars(sym, group="fx_major", underlying=None, roll=None):
    df = walk(50, freq="1D")
    return dy.SymBars(sym, group, underlying, roll, df, df, [])


def test_thin_sample_shrinks_and_flags():            # Section 8 test 7
    raw = {"A": _raw(5, size=1.0), "B": _raw(40, size=3.0)}
    bars = {"A": _bars("A"), "B": _bars("B")}
    refs = {"natr": 0.01, "noise_4h": 0.6, "noise_d": 0.55, "wick_4h": 0.4, "wick_d": 0.4}
    tr = dy.summarize_traits(raw, bars, refs, DCFG, {}, None, ["A", "B"])
    assert tr["A"]["thin"] and any(f.startswith("thin: legs 4h (5 observations)") for f in tr["A"]["flags"])
    assert not tr["B"]["thin"]
    own_a = dy.leg_stats(raw["A"]["legs_4h"])["size_p60"]
    grp = dy.leg_stats(raw["A"]["legs_4h"] + raw["B"]["legs_4h"])["size_p60"]
    assert tr["A"]["legs_4h"]["size_p60"] == pytest.approx((5 * own_a + 20 * grp) / 25)
    assert tr["B"]["legs_4h"]["size_p60"] == pytest.approx(dy.leg_stats(raw["B"]["legs_4h"])["size_p60"])
    assert tr["B"]["scaled"]["dyn_impulse_min_atr"] == 3.0            # clipped to the T1 ceiling
    assert tr["A"]["scaled"]["dyn_zone_frac"] == pytest.approx(0.10)  # noise equal to the reference


def test_shared_underlying_pools_and_excludes_roll_windows():   # [Dyn 3.1]
    raw = {"XAU": _raw(20, seed=1), "GC": _raw(25, seed=2)}
    bars = {"XAU": _bars("XAU", "gold", "GOLD"), "GC": _bars("GC", "gold", "GOLD", roll="gc")}
    refs = {"natr": 0.01, "noise_4h": 0.6, "noise_d": 0.55, "wick_4h": 0.4, "wick_d": 0.4}
    roll_days = {raw["GC"]["legs_4h"][i]["date"].date() for i in range(5)}
    tr = dy.summarize_traits(raw, bars, refs, DCFG, {"gc": {}}, lambda day, key, rules: day in roll_days, [])
    assert tr["XAU"] is tr["GC"] and tr["XAU"]["members"] == ["XAU", "GC"]
    assert tr["XAU"]["legs_4h"]["n"] == 20 + 25 - 5 and tr["XAU"]["legs_4h"]["n_single"] == 25
    assert tr["XAU"]["thin"]                                           # 25 < 30 on the larger single series


# --------------------------------------------------------------- readings on a built instrument
def _instrument_data(sym="EURUSD", n_daily=1300):
    inst = UNIVERSE[sym]
    dfd = walk(n_daily, freq="B", seed=7, start_time="2021-01-04")
    df4 = walk(1000, freq="4h", seed=8, start_time="2026-04-01")
    df2 = walk(1200, freq="2h", seed=9, start_time="2026-06-01")
    rows = []
    for direction in ("long", "short"):
        r = sc.Row(sym, inst.group, direction)
        r.ref_price, r.daily_bias, r.bias_invalidation = float(df2["close"].values[-1]), "long", float(dfd["low"].values[-30])
        r.zone_half_width, r.c2, r.structure_4h = 0.0015, False, "mixed"
        rows.append(r)
    bars = {sc.TF_D: dfd.iloc[-400:], dy.TF_D_FULL: dfd, sc.TF_4H: df4, sc.TF_2H: df2}
    return dy.InstrumentData(inst, bars, rows, sc._dynamics_v10_structure(bars, 2))


def test_weekly_under_150_bars_reports_noisy_mode_unavailable():   # Section 8 test 9
    d = _instrument_data(n_daily=500)                    # about 100 weeks
    out, by_dir = dy.instrument_readings(d, None, sc._dynamics_hooks(), CFG, DCFG, {})
    assert out["dyn_tf_mode"] == "noisy mode unavailable"
    assert any("noisy mode unavailable" in f for f in out["flags"])
    assert by_dir["long"]["dyn_c1_noisy"] is None
    d = _instrument_data(n_daily=1300)                   # about 260 weeks
    out, _ = dy.instrument_readings(d, None, sc._dynamics_hooks(), CFG, DCFG, {})
    assert out["dyn_tf_mode"] in ("normal", "noisy")
    assert out["dyn_structure_state_w"] in ("long", "short", "mixed")
    assert out["dyn_hw_v10"] > 0 and out["dyn_hw_alt"] > 0 and out["dyn_hw_dyn"] is None   # no traits -> no T3
    assert set(dy.DYN_CSV_KEYS) - set(out) - set(by_dir["long"]) <= {"dyn_m_usd", "dyn_m_peer", "dyn_context_score",
                                                                      "dyn_context_points", "dyn_usd_score", "dyn_dxy_score",
                                                                      "dyn_breadth", "dyn_flags"}


def test_traits_use_only_bars_completed_at_decision_time():        # Section 8 test 10
    d = _instrument_data()
    hooks = sc._dynamics_hooks()
    icfg = CFG["features"]["impulse"]

    def raw_at(t_daily, t_4h):
        dfd, df4 = d.bars[dy.TF_D_FULL].iloc[:t_daily], d.bars[sc.TF_4H].iloc[:t_4h]
        seq = hooks.alternating_upto(hooks.find_pivots(df4, 2), len(df4) - 1)
        sb = {"EURUSD": dy.SymBars("EURUSD", "fx_major", None, None, dfd, df4, seq)}
        return dy.raw_traits(sb, None, ["EURUSD"], icfg, DCFG)[0]["EURUSD"]

    early, late = raw_at(1000, 800), raw_at(1300, 1000)
    t_d, t_4 = d.bars[dy.TF_D_FULL].index[999], d.bars[sc.TF_4H].index[799]
    assert all(o["date"] <= t_4 for o in early["impulses"]) and all(e["date"] <= t_d for e in early["breaks"])
    # every impulse decided by the early cut is in the later run with the same outcome
    late_by_date = {(o["date"], o["direction"]): o for o in late["impulses"]}
    for o in early["impulses"]:
        m = late_by_date[(o["date"], o["direction"])]
        assert m["rho_max"] == pytest.approx(o["rho_max"]) and m["continued"] == o["continued"]


# --------------------------------------------------------------- end to end
def _run(cfg, tmp_path, name):
    out = tmp_path / name
    paths = sc.run_scan(cfg, ROOT, "preny", ASOF, "tradingview", demo=True, out_dir=out)
    return paths, json.loads(paths["json"].read_text(encoding="utf-8"))


def test_layer_off_vs_on_identical_pre_existing_outputs(tmp_path):   # Section 8 test 1
    off_cfg = copy.deepcopy(CFG)
    off_cfg["dynamics"]["enabled"] = False
    p_off, j_off = _run(off_cfg, tmp_path, "off")
    p_on, j_on = _run(CFG, tmp_path, "on")

    def strip(rows):
        return [{k: v for k, v in r.items() if k != "dynamics"} for r in rows]

    assert strip(j_off["rows"]) == strip(j_on["rows"]) and strip(j_off["top"]) == strip(j_on["top"])
    assert j_off["instruments"] == j_on["instruments"]
    assert all(r["dynamics"] == {} for r in j_off["rows"])
    assert all(r["dynamics"] for r in j_on["rows"])
    # CSV: the pre existing columns are identical, dyn_ columns come after them
    import csv
    off_rows = list(csv.reader(p_off["csv"].read_text(encoding="utf-8").splitlines()))
    on_rows = list(csv.reader(p_on["csv"].read_text(encoding="utf-8").splitlines()))
    n_cols = len(off_rows[0])
    assert len(off_rows) == len(on_rows) and len(off_rows) == len(j_off["rows"]) + 1
    for a, b in zip(off_rows, on_rows):
        assert a == b[:n_cols]
    assert on_rows[0][n_cols:] == dy.DYN_CSV_KEYS
    html = p_on["html"].read_text(encoding="utf-8")
    assert "Dynamic layer: logged only, does not affect rank" in html
    assert "Dynamic layer" not in p_off["html"].read_text(encoding="utf-8")


def test_demo_dynamics_outputs(tmp_path):
    paths, j = _run(CFG, tmp_path, "on")
    rows = j["rows"]
    d = rows[0]["dynamics"]
    assert d["dyn_structure_state_4h"] in ("long", "short", "mixed") and d["dyn_tf_mode"] in ("normal", "noisy")
    assert d["dyn_usd_score"] is not None and d["dyn_dxy_score"] is not None       # demo basket and demo DXY
    assert d["dyn_context_points"] in (-2, -1, 0, 1, 2)
    assert all(isinstance(r["dynamics"]["dyn_c2_fixed"], bool) for r in rows)
    assert all(r["dynamics"]["dyn_c2_fixed"] == r["c2"] for r in rows)             # the deciding width is the fixed one
    gold = {r["symbol"]: r["dynamics"] for r in rows if r["symbol"] in ("XAUUSD", "GC") and r["direction"] == "long"}
    assert gold["XAUUSD"]["dyn_impulse_min_atr"] == gold["GC"]["dyn_impulse_min_atr"]   # shared traits per underlying
    meta = j["meta"]
    assert meta["dynamics"]["traits_refreshed"] and "Dynamics" in meta["freshness"]
    trait_files = list((tmp_path / "on" / "personality").glob("personality_*.json"))
    assert len(trait_files) == 1
    tf = json.loads(trait_files[0].read_text(encoding="utf-8"))
    assert tf["config_version"] == CFG["config_version"] and len(tf["instruments"]) == 34
    assert "natr" in tf["stability"] and tf["bar_counts"]["EURUSD"]["daily"] >= 1250
    assert (tmp_path / "on" / "dynamics_compare.csv").exists()
    cmp_lines = (tmp_path / "on" / "dynamics_compare.csv").read_text(encoding="utf-8").splitlines()
    assert cmp_lines[0].split(",") == dy.COMPARE_COLUMNS and len(cmp_lines) == 35


def test_missing_major_makes_context_unavailable_and_scan_completes(tmp_path, monkeypatch):   # Section 8 test 8
    real = sc.load_instrument_bars

    def failing(src, inst, cfg, asof, daily_cutoff):
        if inst.symbol == "NZDUSD":
            raise RuntimeError("feed down")
        return real(src, inst, cfg, asof, daily_cutoff)

    monkeypatch.setattr(sc, "load_instrument_bars", failing)
    paths, j = _run(CFG, tmp_path, "missing")
    assert paths["html"].exists() and j["meta"]["instruments_with_errors"] == 1
    scored = [r for r in j["rows"] if r["dynamics"]]
    assert scored and all(r["dynamics"]["dyn_context_points"] is None and r["dynamics"]["dyn_usd_score"] is None for r in scored)
    assert all("context unavailable" in r["dynamics"]["dyn_flags"] for r in scored)
    assert all(r["dynamics"]["dyn_structure_state_4h"] in ("long", "short", "mixed") for r in scored)   # Section 4 still runs


def test_html_block_and_csv_values_shapes():
    assert dy.html_block({}, lambda s: s).startswith("<span")
    block = dy.html_block({"dyn_structure_score_4h": 0.5, "dyn_structure_state_4h": "long", "dyn_tf_mode": "normal",
                           "dyn_pullback_zone": "normal", "dyn_pullback_depth": 0.5, "dyn_nearest_level": 0.5,
                           "dyn_condition": "clear", "dyn_context_points": 2, "dyn_flags": "context conflict"}, lambda s: s)
    assert "Trend" in block and "Location" in block and "Condition" in block and "Context" in block and "+2" in block
    assert len(dy.csv_values({})) == len(dy.DYN_CSV_KEYS) and len(dy.c2_extra_values({})) == len(dy.C2_EXTRA_COLUMNS)
