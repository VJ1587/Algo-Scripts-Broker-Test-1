"""Unit tests for scanner.py. Run: python -m pytest -q"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scanner as sc  # noqa: E402

CFG = sc.load_config(Path(__file__).resolve().parents[1] / "scanner_config.yaml")
UNIVERSE = {i.symbol: i for i in sc.build_universe(CFG)}


def bars(h, l, o=None, c=None, start="2026-01-01", freq="4h"):
    n = len(h)
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    h, l = np.asarray(h, float), np.asarray(l, float)
    c = (h + l) / 2 if c is None else np.asarray(c, float)
    o = c if o is None else np.asarray(o, float)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


# --------------------------------------------------------------- universe
def test_universe_has_34_instruments_and_grids():
    assert len(UNIVERSE) == 34
    assert UNIVERSE["USDJPY"].grid_major == 5.0 and UNIVERSE["USDJPY"].tick == 0.001
    assert UNIVERSE["EURUSD"].grid_major == 0.05 and UNIVERSE["EURUSD"].grid_mid == 0.025
    assert UNIVERSE["CL"].grid_mid == 2.5 and UNIVERSE["GC"].fut_contract == 1


# --------------------------------------------------------------- indicators
def test_ema_seed_and_recursion():
    v = np.arange(1, 11, dtype=float)
    e = sc.ema(v, 3)
    assert np.isnan(e[1]) and e[2] == pytest.approx(2.0)
    assert e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)


def test_atr_seed_uses_first_n_true_ranges():
    df = bars([2] * 20, [1] * 20, c=[1.5] * 20, freq="1h")
    a = sc.atr(df, 14)
    assert np.isnan(a[13]) and a[14] == pytest.approx(1.0) and a[19] == pytest.approx(1.0)


# --------------------------------------------------------------- pivots
def test_pivot_strict_and_confirmation_lag():
    df = bars([1, 2, 5, 2, 1, 1, 1], [0.5, 1, 1, 1, 0.5, 0.4, 0.3])
    piv = sc.find_pivots(df, 2)
    hs = [p for p in piv if p.kind == "H"]
    assert len(hs) == 1 and hs[0].k == 2 and hs[0].confirm == 4


def test_pivot_tie_fails():
    df = bars([1, 2, 5, 5, 1, 1, 1], [0.9] * 7)
    assert not [p for p in sc.find_pivots(df, 2) if p.kind == "H"]


def test_pivot_both_types_discarded():
    df = bars([1, 1, 9, 1, 1], [5, 5, 0, 5, 5])
    assert sc.find_pivots(df, 2) == []


def test_alternation_keeps_more_extreme_and_earlier_on_tie():
    seq = []
    for p in [sc.Pivot(1, "H", 5, 3), sc.Pivot(4, "H", 6, 6), sc.Pivot(7, "H", 6, 9)]:
        sc.add_alternating(seq, p)
    assert len(seq) == 1 and seq[0].k == 4


def test_structure():
    P = sc.Pivot
    up = [P(0, "L", 1, 2), P(2, "H", 3, 4), P(4, "L", 2, 6), P(6, "H", 4, 8)]
    assert sc.structure(up) == sc.LONG
    dn = [P(0, "H", 4, 2), P(2, "L", 2, 4), P(4, "H", 3, 6), P(6, "L", 1, 8)]
    assert sc.structure(dn) == sc.SHORT
    assert sc.structure(up[:3]) is None


# --------------------------------------------------------------- daily bias
def _zigzag_up():
    # rising swings: lows 10, 12, 14 / highs 15, 17, 19
    seq = [10, 15, 12, 17, 14, 19]
    h, l = [], []
    for i, v in enumerate(seq):
        for j in range(5):
            mid = v if j == 2 else (v - 0.5 if i % 2 else v + 0.5)
            h.append(mid + 0.3 if i % 2 else mid + 1.0)
            l.append(mid - 1.0 if i % 2 else mid - 0.3)
    return h, l


def test_daily_bias_long_and_wick_does_not_invalidate():
    h, l = _zigzag_up()
    df = bars(h, l, freq="1D")
    b = sc.daily_bias(df, sc.find_pivots(df, 2))
    assert b.bias == sc.LONG
    inv = b.invalidation
    # wick below invalidation but close above -> still long
    h2, l2 = h + [20], l + [inv - 5]
    df2 = bars(h2, l2, c=list((np.array(h) + np.array(l)) / 2) + [inv + 1], freq="1D")
    assert sc.daily_bias(df2, sc.find_pivots(df2, 2)).bias == sc.LONG
    # close below invalidation -> not long
    df3 = bars(h + [inv], l + [inv - 6], c=list((np.array(h) + np.array(l)) / 2) + [inv - 1], freq="1D")
    b3 = sc.daily_bias(df3, sc.find_pivots(df3, 2))
    assert b3.bias != sc.LONG and b3.invalidated_on_last_bar


# --------------------------------------------------------------- zones and fib
def test_zone_and_nearest_level():
    eu = UNIVERSE["EURUSD"]
    lvl, kind = sc.nearest_level(eu, 1.1740)
    assert lvl == pytest.approx(1.175) and kind == "mid"
    lvl, kind = sc.nearest_level(eu, 1.2010)
    assert lvl == pytest.approx(1.20) and kind == "major"
    assert sc.zone_half_width(eu, 0.0001, CFG["features"]) == pytest.approx(0.00002)  # two ticks win
    assert sc.zone_half_width(eu, 0.01, CFG["features"]) == pytest.approx(0.001)
    assert sc.in_psych_zone(eu, 1.1741, 0.001)[0] and not sc.in_psych_zone(eu, 1.1600, 0.001)[0]


def test_psych_zone_fixed_15_pips_fx_and_atr_where_unset():
    eu, uj, xau = UNIVERSE["EURUSD"], UNIVERSE["USDJPY"], UNIVERSE["XAUUSD"]
    assert sc.psych_zone_half_width(eu, 0.0004) == (pytest.approx(0.0015), "fixed")
    assert sc.psych_zone_half_width(uj, 0.04) == (pytest.approx(0.15), "fixed")
    assert sc.psych_zone_half_width(xau, 1.7) == (pytest.approx(20.0), "fixed")  # gold +/- $20
    assert sc.psych_zone_half_width(UNIVERSE["GC"], 1.7) == (pytest.approx(20.0), "fixed")
    assert sc.psych_zone_half_width(UNIVERSE["ES"], 3.0) == (pytest.approx(20.0), "fixed")   # S&P follows gold
    assert sc.psych_zone_half_width(UNIVERSE["WTI"], 0.3) == (pytest.approx(1.0), "fixed")  # oil: gold ratio
    assert all(i.psych_zone_hw is not None for i in UNIVERSE.values())                       # no ATR fallback left
    # gold 3,300 major: 3,282 is inside the $3,280-$3,320 zone, 3,278 is outside
    assert sc.in_psych_zone(xau, 3282.0, 20.0)[1:] == (pytest.approx(3300.0), "major")
    assert sc.in_psych_zone(xau, 3282.0, 20.0)[0] and not sc.in_psych_zone(xau, 3278.0, 20.0)[0]
    # 1.1736 is 14 pips under the 1.1750 mid level: inside; 1.1734 is 16 pips under: outside
    assert sc.in_psych_zone(eu, 1.1736, 0.0015)[0] and not sc.in_psych_zone(eu, 1.1734, 0.0015)[0]


def test_zone_wick_tests_long_and_short():
    lo, hi = 1.1735, 1.1765                     # 1.1750 +/- 15 pips
    # long: two lower wicks into the zone that close back above the floor, one plain bar, one close through
    o = [1.1780, 1.1775, 1.1790, 1.1745]
    c = [1.1782, 1.1778, 1.1795, 1.1720]
    l = [1.1760, 1.1740, 1.1785, 1.1715]
    h = [1.1784, 1.1780, 1.1797, 1.1748]
    df = bars(h, l, o=o, c=c, freq="2h")
    assert sc.zone_wick_tests(df, sc.LONG, lo, hi, 6) == 2
    assert sc.zone_wick_tests(df, sc.LONG, lo, hi, 2) == 0     # lookback excludes the wick bars
    # short mirror: upper wick into the zone from below, close back under the ceiling
    df_s = bars([1.1760], [1.1700], o=[1.1712], c=[1.1708], freq="2h")
    assert sc.zone_wick_tests(df_s, sc.SHORT, lo, hi, 6) == 1
    assert sc.zone_wick_tests(df_s, sc.LONG, lo, hi, 6) == 0


def test_fib_formula_matches_v1_example():
    imp = sc.Impulse(sc.LONG, 4000, 4200, "", "", 0, 5, 0.8, 1, 1, 0, 5)
    assert imp.fib(0.382) == pytest.approx(4123.6) and imp.fib(0.618) == pytest.approx(4076.4)


# --------------------------------------------------------------- impulse
def test_select_impulse_long():
    # flat base, swing high H0, low A, strong rally to B that breaks H0, then two bars to confirm
    h = [10.5, 10.6, 10.7, 11.0, 10.7, 10.5, 10.2, 10.0, 10.3, 10.6, 11.0, 11.5, 12.0, 12.5, 13.0, 12.7, 12.6]
    l = [x - 0.4 for x in h]
    l[7] = 9.4
    c = [(a + b) / 2 for a, b in zip(h, l)]
    pre_h = [10.5 + 0.01 * (i % 3) for i in range(30)]
    df = bars(pre_h + h, [x - 0.4 for x in pre_h] + l, c=[x - 0.2 for x in pre_h] + c)
    piv = sc.find_pivots(df, 2)
    a = sc.atr(df, 14)
    a[:] = 0.5
    imp, why = sc.select_impulse(df, piv, a, sc.LONG, CFG["features"])
    assert imp is not None, why
    assert imp.a_price == pytest.approx(9.4) and imp.b_price == pytest.approx(13.0)


# --------------------------------------------------------------- candles
ZL, ZH = 1.0985, 1.1015                         # 1.1000 key level +/- 15 pips


def _trend(n, first_close, step):
    """n candles with closes first_close, first_close+step, ...; bodies 8 pips in the trend direction."""
    c = [first_close + i * step for i in range(n)]
    o = [x - 0.0008 if step > 0 else x + 0.0008 for x in c]
    return o, [max(a, b) + 0.0002 for a, b in zip(o, c)], [min(a, b) - 0.0002 for a, b in zip(o, c)], c


def _candles(trend, extra, shift=0.0):
    o, h, l, c = (list(x) for x in trend)
    for eo, eh, el, ec in extra:
        o.append(eo); h.append(eh); l.append(el); c.append(ec)
    df = bars(np.array(h) + shift, np.array(l) + shift, o=np.array(o) + shift, c=np.array(c) + shift, freq="2h")
    return df, np.full(len(df), 0.004)


def _c4(df, atr2, direction):
    return sc.candle_signal(df, atr2, UNIVERSE["EURUSD"], direction, CFG["features"]["candle"], ZL, ZH)


DOWN = _trend(7, 1.1100, -0.0015)               # pullback into the zone from above
UP = _trend(7, 1.0900, 0.0015)                  # rally into the zone from below
HAMMER = (1.1005, 1.1010, 1.0980, 1.1008)       # o, h, l, c: long lower wick into the zone


def test_c4_hammer_in_zone_only():
    assert _c4(*_candles(DOWN, [HAMMER]), sc.LONG) == "hammer"
    assert _c4(*_candles(DOWN, [HAMMER], shift=0.0100), sc.LONG) == ""      # same candle, outside the zone
    # same shape after a rally is a hanging man: bearish, not a long signal
    assert _c4(*_candles(UP, [HAMMER]), sc.LONG) == ""
    assert _c4(*_candles(UP, [HAMMER]), sc.SHORT) == "hanging man"


def test_c4_shooting_star_and_inverted_hammer():
    star = (1.0995, 1.1020, 1.0990, 1.0992)
    assert _c4(*_candles(UP, [star]), sc.SHORT) == "shooting star"
    assert _c4(*_candles(DOWN, [star]), sc.LONG) == "inverted hammer"


def test_c4_engulfing_tweezer_morning_star():
    df = bars([1.105, 1.106], [1.099, 1.098], o=[1.104, 1.0995], c=[1.100, 1.1055], freq="2h")
    assert _c4(df, np.array([0.004, 0.004]), sc.LONG) == "bullish engulfing"
    # closes 40 pips above the zone: still valid, the engulfing candle wicked into it
    df = bars([1.1012, 1.1060], [1.0995, 1.0990], o=[1.1010, 1.0998], c=[1.0999, 1.1055], freq="2h")
    assert _c4(df, np.array([0.004, 0.004]), sc.LONG) == "bullish engulfing"
    # only the prior candle touched the zone; the engulfing candle never tested it
    df = bars([1.1030, 1.1070], [1.1010, 1.1018], o=[1.1028, 1.1018], c=[1.1020, 1.1065], freq="2h")
    assert _c4(df, np.array([0.004, 0.004]), sc.LONG) == ""
    tweezer = [(1.1030, 1.1032, 1.0995, 1.1005), (1.1006, 1.1030, 1.0996, 1.1028)]
    assert _c4(*_candles(DOWN, tweezer), sc.LONG) == "tweezer bottom"
    morning = [(1.1040, 1.1042, 1.1008, 1.1010), (1.1005, 1.1010, 1.0990, 1.1004), (1.1006, 1.1037, 1.1004, 1.1035)]
    assert _c4(*_candles(DOWN, morning), sc.LONG) == "morning star"


def test_c4_marubozu_confirms_zone_reversal():
    marubozu = (1.1008, 1.1052, 1.1007, 1.1050)
    assert _c4(*_candles(DOWN, [HAMMER, marubozu]), sc.LONG) == "hammer + marubozu"
    assert _c4(*_candles(DOWN, [HAMMER, marubozu], shift=0.0100), sc.LONG) == ""


# --------------------------------------------------------------- COT
def _series(nets, pubs_start="2025-01-07"):
    d = pd.date_range(pubs_start, periods=len(nets), freq="7D")
    df = pd.DataFrame({"report_date": d, "net": nets, "open_interest": 1000.0})
    df["publication_ts"] = [sc.cot_publication_ts(pd.Timestamp(x), CFG["cot"]) for x in d]
    return sc.CotSeries("X", "TFF", df)


def test_cot_index_and_unavailable():
    s = _series(list(range(52)))
    asof = s.frame["publication_ts"].iloc[-1] + pd.Timedelta(hours=1)
    idx, _ = sc.cot_index_at(s, asof, 52)
    assert idx == pytest.approx(100.0)
    idx, _ = sc.cot_index_at(_series(list(range(40))), asof, 52)
    assert idx is None
    flat = _series([5] * 52)
    assert sc.cot_index_at(flat, asof, 52)[0] is None


def test_cot_publication_not_on_position_date():
    s = _series(list(range(53)))
    tuesday = pd.Timestamp(s.frame["report_date"].iloc[-1]).tz_localize("UTC") + pd.Timedelta(hours=23)
    _, last = sc.cot_index_at(s, tuesday, 52)
    assert pd.Timestamp(last["report_date"]) < pd.Timestamp(s.frame["report_date"].iloc[-1])


@pytest.mark.parametrize("idx,chg,exp", [(95, 1, -2), (85, -1, -1), (50, 1, 1), (50, -1, 0), (50, 0, 0),
                                         (15, 1, 1), (15, -1, 0), (5, 1, 2), (5, -1, 0)])
def test_cot_points_long_table(idx, chg, exp):
    r = sc.CotReading(idx, None, chg, "", "", "TFF", "ok")
    assert sc.cot_points(r, sc.LONG, CFG["cot"]) == exp


def test_cot_points_short_mirror_and_status():
    r = sc.CotReading(5, None, -1, "", "", "TFF", "ok")      # crowded short + falling => short side is crowded
    assert sc.cot_points(r, sc.SHORT, CFG["cot"]) == -2
    assert sc.cot_points(sc.CotReading(5, None, 1, "", "", "", "stale"), sc.LONG, CFG["cot"]) == 0


def test_cot_pair_mapping_inversion_and_cross():
    base = _series(list(range(60)))           # rising: latest index 100
    flat = _series([0] * 30 + list(range(30)))
    cot = {"JPY": base, "EUR": base, "GBP": flat}
    asof = base.frame["publication_ts"].iloc[-1] + pd.Timedelta(hours=1)
    usdjpy = sc.cot_for_instrument(UNIVERSE["USDJPY"], cot, asof, CFG["cot"])
    assert usdjpy.index == pytest.approx(0.0) and usdjpy.weekly_change < 0
    eurgbp = sc.cot_for_instrument(UNIVERSE["EURGBP"], cot, asof, CFG["cot"])
    assert eurgbp.index == pytest.approx(50 + (100 - 100) / 2)
    stale = sc.cot_for_instrument(UNIVERSE["USDJPY"], cot, asof + pd.Timedelta(days=11), CFG["cot"])
    assert stale.status == "stale"
    assert sc.cot_for_instrument(UNIVERSE["AUDUSD"], cot, asof, CFG["cot"]).status == "unavailable"


def test_cot_publication_dst():
    # Tuesday 2026-07-07 -> Friday 15:30 EDT = 19:30 UTC; Tuesday 2026-01-06 -> 20:30 UTC
    assert sc.cot_publication_ts(pd.Timestamp("2026-07-07"), CFG["cot"]).hour == 19
    assert sc.cot_publication_ts(pd.Timestamp("2026-01-06"), CFG["cot"]).hour == 20


# --------------------------------------------------------------- sentiment
ENG = sc.SentimentEngine(CFG)


def H(title, tone, hours, asof):
    h = sc.Headline(title, "t", (asof - pd.Timedelta(hours=hours)).isoformat())
    h.tone, h.theme = tone, ENG.tag_theme(title)
    return h


def test_hawkish_fed_lifts_usdjpy_weighs_eurusd():
    asof = pd.Timestamp("2026-10-02T12:05Z")
    hs = [H("Fed signals hawkish rate hike path", -0.3, 1, asof)]
    assert hs[0].theme == "central_bank"
    s_uj = ENG.score(UNIVERSE["USDJPY"], hs, asof)["S"]
    s_eu = ENG.score(UNIVERSE["EURUSD"], hs, asof)["S"]
    assert s_uj > 0 and s_eu < 0


def test_gold_conflict_inverts_tone_and_thin_news():
    asof = pd.Timestamp("2026-10-02T12:05Z")
    hs = [H("Missile attack sparks escalation fears", -0.8, 1, asof)]
    res = ENG.score(UNIVERSE["XAUUSD"], hs, asof)
    assert res["S"] > 0 and res["thin"] is True       # one headline, weight 0.9 < 1.0
    assert ENG.points(res["S"], res["thin"], sc.LONG) == 0


def test_sentiment_points_thresholds():
    p = ENG.points
    assert p(0.5, False, sc.LONG) == 2 and p(0.2, False, sc.LONG) == 1 and p(0.19, False, sc.LONG) == 0
    assert p(-0.2, False, sc.LONG) == -1 and p(-0.5, False, sc.LONG) == -2 and p(0.6, False, sc.SHORT) == -2


def test_old_headlines_ignored():
    asof = pd.Timestamp("2026-10-02T12:05Z")
    hs = [H("Fed hawkish rate hike", 0.5, 30, asof)]
    assert ENG.score(UNIVERSE["EURUSD"], hs, asof)["S"] is None


# --------------------------------------------------------------- ranking
def gr(checks: str, golden=False, stacked=False):
    """A Row with the given checks passed, e.g. gr('1235', golden=True, stacked=True)."""
    r = sc.Row("X", "FX", "long")
    for i in checks:
        setattr(r, f"c{i}", True)
    r.c3_golden, r.stacked = golden, stacked
    r.technical = len(checks)
    return r


@pytest.mark.parametrize(
    "checks,golden,stacked,kind,section,grade",
    [
        ("1235", True, True, "major", "qualified", "A"),     # C1 + C2 + C3 stacked + C5: A without waiting for C4
        ("1234", True, False, "major", "qualified", "B"),    # golden Fib but not inside the zone: B
        ("125", False, False, "major", "qualified", "B"),    # C1 + C2 + C5
        ("135", True, False, "major", "qualified", "B"),     # C1 + C3 at 50/61.8 + C5
        ("135", False, False, "major", "developing", ""),    # 38.2% only: not enough for B without C2
        ("12", False, False, "major", "developing", ""),     # no C4/C5/C6
        ("2345", True, True, "major", "developing", ""),     # no C1: never graded
        ("1", False, False, "major", "not shown", ""),
        ("1235", True, True, "minor", "developing", ""),     # minor pair: A needs a closed C4
        ("12345", True, True, "minor", "qualified", "A"),
        ("125", False, False, "minor", "developing", ""),    # minor B does not count
        ("123", False, False, "gold", "qualified", "B"),     # gold B = C1 + C2 + C3
        ("125", False, False, "gold", "developing", ""),     # gold needs C3
        ("1235", True, True, "gold", "qualified", "B"),      # gold A needs C4
        ("1234", True, True, "gold", "qualified", "A"),
        ("1235", True, True, "a_only", "qualified", "A"),    # S&P and oil: A setups count
        ("1256", False, False, "a_only", "developing", ""),  # a B setup is only developing
    ],
)
def test_grade_setup(checks, golden, stacked, kind, section, grade):
    s, g, note = sc.grade_setup(gr(checks, golden, stacked), kind)
    assert (s, g) == (section, grade)
    if s == "developing":
        assert note


def test_grade_notes_spell_out_execution():
    assert "Full size" in sc.grade_setup(gr("1235", True, True), "major")[2]
    assert "Smaller size" in sc.grade_setup(gr("125"), "major")[2]
    assert "very small" in sc.grade_setup(gr("123"), "gold")[2]
    assert "size up" in sc.grade_setup(gr("12356", True, True), "major")[2]       # 5+ confluences
    assert sc.setup_kind(UNIVERSE["GBPAUD"]) == "minor" and sc.setup_kind(UNIVERSE["EURUSD"]) == "major"
    assert sc.setup_kind(UNIVERSE["GC"]) == "gold" and sc.setup_kind(UNIVERSE["ES"]) == "a_only"
    assert sc.setup_kind(UNIVERSE["WTI"]) == "a_only" and sc.setup_kind(UNIVERSE["USDCAD"]) == "major"
    assert "A setups only" in sc.grade_setup(gr("1256"), "a_only")[2]


def test_fib_plan_stop_and_targets():
    p = sc.fib_plan({"A": 1.1000, "B": 1.1200}, CFG["features"])     # long impulse of 200 pips
    assert p["stop_786"] == pytest.approx(1.10428) and p["stop_890"] == pytest.approx(1.1022)
    assert p["tp1"] == pytest.approx(1.1254) and p["tp2"] == pytest.approx(1.13236)
    assert sc.fib_plan(None, CFG["features"]) == {}


def test_structure_holding_c6():
    idx4 = pd.date_range("2026-09-01", periods=9, freq="4h", tz="UTC")
    df4 = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}, index=idx4)
    piv = [sc.Pivot(1, "L", 1.10, 3), sc.Pivot(3, "H", 1.13, 5), sc.Pivot(5, "L", 1.11, 7)]   # higher low 1.11
    idx2 = pd.date_range(idx4[5], periods=8, freq="2h", tz="UTC")
    ok = pd.DataFrame({"close": [1.12, 1.115, 1.112, 1.118, 1.12, 1.121, 1.119, 1.12]}, index=idx2)
    assert sc.structure_holding(df4, piv, ok, "long")[0]
    broken = ok.assign(close=[1.12, 1.115, 1.108, 1.118, 1.12, 1.121, 1.119, 1.12])   # 2H closed below 1.11
    held, why = sc.structure_holding(df4, piv, broken, "long")
    assert not held and "compromised" in why
    lower = [sc.Pivot(1, "L", 1.10, 3), sc.Pivot(3, "H", 1.13, 5), sc.Pivot(5, "L", 1.09, 7)]
    assert not sc.structure_holding(df4, lower, ok, "long")[0]


def test_weekly_from_daily_drops_unfinished_week():
    idx = pd.date_range("2026-09-07", "2026-09-23", freq="B", tz="UTC")     # Mon 7 Sep to Wed 23 Sep
    dfd = pd.DataFrame({"open": range(len(idx)), "high": range(1, len(idx) + 1), "low": range(len(idx)),
                        "close": range(len(idx))}, index=idx, dtype=float)
    wk = sc.weekly_from_daily(dfd)
    assert len(wk) == 2 and wk.index[0] == pd.Timestamp("2026-09-07", tz="UTC")
    assert wk["open"].iloc[0] == 0 and wk["close"].iloc[0] == 4 and wk["high"].iloc[1] == 10


def mk(sym, d, section, tech, cot, total):
    r = sc.Row(sym, "FX", d)
    r.section, r.technical, r.cot_points, r.total = section, tech, cot, total
    r.grade = "B" if section == "qualified" else ""
    return r


def test_rank_qualified_first_ties_and_no_padding():
    rows = [mk("EURUSD", "long", "developing", 2, 2, 6), mk("GBPUSD", "long", "qualified", 3, 0, 3),
            mk("AUDUSD", "long", "qualified", 4, 0, 5), mk("NZDUSD", "long", "qualified", 3, 2, 5),
            mk("USDCAD", "long", "not shown", 1, 2, 5)]
    top = sc.rank(rows, 5)
    assert [r.symbol for r in top] == ["AUDUSD", "NZDUSD", "GBPUSD", "EURUSD"]
    assert [r.rank for r in top] == [1, 2, 3, 4]


def test_rank_higher_timeframe_reversal_wins_ties():
    a, b, c = (mk(x, "long", "qualified", 3, 0, 3) for x in ("AUDUSD", "EURUSD", "GBPUSD"))
    a.c4_tf_rank, b.c4_tf_rank, c.c4_tf_rank = 1, 3, 2      # 2H, Daily, 4H
    assert [r.symbol for r in sc.rank([a, b, c], 5)] == ["EURUSD", "GBPUSD", "AUDUSD"]


# --------------------------------------------------------------- chart patterns (C4, Daily and 4H)
CP = CFG["features"]["chart_patterns"]


def _path(points, shift=0.0, last_close=None):
    """Bars along straight segments between (bar, price) points; high/low = price +/- 5 pips."""
    px = []
    for (k0, p0), (k1, p1) in zip(points, points[1:]):
        px += [p0 + (p1 - p0) * (k - k0) / (k1 - k0) for k in range(k0, k1)]
    px.append(points[-1][1])
    px = np.array(px) + shift
    c = px.copy()
    if last_close is not None:
        c[-1] = last_close + shift
    o = np.r_[c[0], c[:-1]]
    return bars(np.maximum(px, c) + 0.0005, np.minimum(px, c) - 0.0005, o=o, c=c, freq="4h")


def _cp(df, direction):
    eu = UNIVERSE["EURUSD"]
    return sc.chart_pattern(df, sc.find_pivots(df, 2), direction, eu, eu.psych_zone_hw, CP, sc.TF_4H)


# bottoms at 1.1000 (key level) and 1.1004, neckline high 1.1065, last 4H bar closes 1.1075
DOUBLE = [(0, 1.1100), (5, 1.1000), (10, 1.1060), (15, 1.1004), (19, 1.1056), (20, 1.1075)]


def test_double_bottom_needs_zone_and_neckline_close():
    assert _cp(_path(DOUBLE), sc.LONG).startswith("double bottom at ")
    assert _cp(_path(DOUBLE, shift=0.0100), sc.LONG) == ""               # bottoms 95 pips from any level
    wick_only = DOUBLE[:-1] + [(20, 1.1068)]                             # high pokes the neckline, close below
    assert _cp(_path(wick_only, last_close=1.1058), sc.LONG) == ""
    assert _cp(_path(DOUBLE), sc.SHORT) == ""


def test_double_bottom_touches_too_close_together():
    tight = [(0, 1.1100), (5, 1.1000), (7, 1.1040), (9, 1.1004), (12, 1.1050), (13, 1.1075)]
    assert _cp(_path(tight), sc.LONG) == ""                              # 4 bars apart: consolidation


def test_inverse_head_and_shoulders():
    ihs = [(0, 1.1100), (4, 1.1030), (8, 1.1070), (12, 1.1000), (16, 1.1072), (20, 1.1035), (24, 1.1068), (25, 1.1090)]
    assert _cp(_path(ihs), sc.LONG).startswith("inverse head and shoulders at ")
    shallow = [(0, 1.1100), (4, 1.1012), (8, 1.1070), (12, 1.1000), (16, 1.1072), (20, 1.1015), (24, 1.1068), (25, 1.1090)]
    assert _cp(_path(shallow), sc.LONG).startswith("double bottom")               # head only 12 pips lower: not an H&S


def test_same_underlying_flag():
    top = [mk("XAUUSD", "long", "qualified", 3, 0, 3), mk("GC", "long", "qualified", 3, 0, 3)]
    sc.mark_same_underlying(top, UNIVERSE)
    assert all(r.same_underlying for r in top)


# --------------------------------------------------------------- bars and schedule
def test_completed_and_resample_utc_anchor():
    idx = pd.date_range("2026-10-01 20:00", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0}, index=idx)
    r4 = sc.resample_utc(df, "4H")
    assert all(t.hour % 4 == 0 for t in r4.index)
    asof = pd.Timestamp("2026-10-02 04:05Z")
    done = sc.completed(r4, "4H", asof)
    assert done.index[-1] == pd.Timestamp("2026-10-02 00:00Z")


def test_resolve_run_daily_cutoff():
    rt, cut = sc.resolve_run("preny", pd.Timestamp("2026-10-02T12:05Z"))
    assert rt == "pre NY" and cut == pd.Timestamp("2026-10-02T00:05Z")
    rt, cut = sc.resolve_run("auto", pd.Timestamp("2026-10-03T00:05Z"))
    assert rt == "evening"


def test_roll_windows():
    rules = CFG["roll_rules"]
    assert sc.in_roll_window(pd.Timestamp("2026-09-15").date(), "es", rules)      # third Friday Sep 18
    assert not sc.in_roll_window(pd.Timestamp("2026-09-01").date(), "es", rules)
    assert sc.in_roll_window(pd.Timestamp("2026-09-25").date(), "gc", rules)      # Oct is active
    assert sc.in_roll_window(pd.Timestamp("2026-09-18").date(), "cl", rules)


def test_check_bars_flags_bad_ohlc():
    df = bars([1, 2], [0.5, 3])
    errs = sc.check_bars(df, 2)
    assert any("impossible" in e for e in errs)


def test_parse_rss():
    xml = """<rss><channel><item><title>Fed holds rates</title><link>http://x</link>
             <pubDate>Fri, 02 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    hs = sc.parse_feed(xml, "fed")
    assert len(hs) == 1 and hs[0].title == "Fed holds rates"


def test_demo_end_to_end(tmp_path):
    paths = sc.run_scan(CFG, Path(__file__).resolve().parents[1], "preny", pd.Timestamp("2026-10-02T12:05Z"),
                        "tradingview", demo=True, out_dir=tmp_path)
    assert paths["html"].exists() and paths["json"].exists() and paths["csv"].exists()
    report = paths["html"].read_text(encoding="utf-8")
    assert all(f">C{i}</th>" in report for i in range(1, 7))
    assert "Developing setups (" in report and "Confluence definitions" in report
    assert all(name in report for name, _ in sc.CONFLUENCES.values())
    rows = json.loads(paths["json"].read_text(encoding="utf-8"))["rows"]
    assert all(isinstance(r[k], bool) for r in rows for k in sc.CONF_KEYS)   # no "True"/"False" text
    # gold rules apply to both directions (the zone lookup once overwrote the instrument kind)
    gold = [r for r in rows if r["symbol"] in ("XAUUSD", "GC")]
    assert gold and all(r["weekly"] for r in gold)
    # gold C1 = Weekly + Daily; the 4H is ignored
    assert all(r["c1"] == (r["daily_bias"] == r["direction"] == r["weekly"]) for r in gold)
    assert not any(r["grade"] == "B" for r in rows if r["symbol"] in ("SPX500", "ES", "WTI", "CL"))
    assert all(r["grade"] in ("A", "B") for r in rows if r["section"] == "qualified")
    assert all(r["grade"] == "A" and r["c4"] for r in rows if r["group"] == "FX cross" and r["section"] == "qualified")


# --------------------------------------------------------------- MT5 and journal (cfg-0.5.0)
def test_mt5_server_time_ny_close_summer_and_winter():
    # Server 00:00 = 17:00 New York the previous day: 21:00 UTC in summer, 22:00 UTC in winter
    summer = int(pd.Timestamp("2026-10-06 00:00").timestamp())
    winter = int(pd.Timestamp("2026-12-07 00:00").timestamp())
    out = sc.mt5_to_utc([summer, winter], "ny_close")
    assert out[0] == pd.Timestamp("2026-10-05 21:00", tz="UTC")
    assert out[1] == pd.Timestamp("2026-12-06 22:00", tz="UTC")
    assert sc.mt5_to_utc([summer], 3)[0] == pd.Timestamp("2026-10-05 21:00", tz="UTC")


class _FailingSource(sc.BarSource):
    name = "fail"

    def get(self, inst, tf):
        raise RuntimeError("terminal closed")


class _OkSource(sc.BarSource):
    name = "ok"

    def get(self, inst, tf):
        return bars([2, 2], [1, 1])


class _NamedSource(sc.BarSource):
    def __init__(self, label, n):
        self.label, self.n = label, n

    def get(self, inst, tf):
        df = bars([2] * self.n, [1] * self.n)
        df.attrs["label"] = self.label
        return df

    def provider(self, inst):
        return f"{self.label}:{inst.symbol}"


def test_routed_source_fx_from_mt5_others_from_tradingview_with_fallback():
    routed = sc.RoutedSource(_FailingSource(), _OkSource(), min_bars=1)
    assert len(routed.get(UNIVERSE["GC"], "D")) == 2 and "GC" not in routed.fallback
    assert len(routed.get(UNIVERSE["EURUSD"], "D")) == 2      # MT5 failed, TradingView used
    assert "EURUSD" in routed.fallback and "MT5 not used" in routed.provider(UNIVERSE["EURUSD"])


def test_routed_source_short_broker_history_takes_whole_pair_from_tradingview():
    deep = sc.RoutedSource(_NamedSource("mt5", 2000), _NamedSource("tv", 2000), min_bars=250)
    assert deep.get(UNIVERSE["EURUSD"], "1H").attrs["label"] == "mt5"
    assert deep.get(UNIVERSE["EURUSD"], "D").attrs["label"] == "mt5"
    shallow = sc.RoutedSource(_NamedSource("mt5", 90), _NamedSource("tv", 2000), min_bars=250)
    assert shallow.get(UNIVERSE["AUDJPY"], "1H").attrs["label"] == "tv"   # no mixing: 1H also from TradingView
    assert shallow.get(UNIVERSE["AUDJPY"], "D").attrs["label"] == "tv"
    assert "too short" in shallow.provider(UNIVERSE["AUDJPY"])


def _deal(pid, t, typ, entry, vol, price, profit=0.0, sym="EURUSD"):
    return {"ticket": pid * 10 + entry, "position_id": pid, "time": pd.Timestamp(t, tz="UTC"), "type": typ,
            "entry": entry, "symbol": sym, "mt5_symbol": sym, "volume": vol, "price": price, "profit": profit,
            "commission": -1.0, "swap": 0.0, "fee": 0.0, "comment": ""}


def test_build_trades_groups_deals_and_open_positions():
    deals = [_deal(1, "2026-10-06 13:00", 1, 0, 1.0, 190.0, sym="CHFJPY"),        # sell in
             _deal(1, "2026-10-06 18:00", 0, 1, 1.0, 189.5, 30.0, sym="CHFJPY"),  # buy out
             _deal(2, "2026-10-06 14:00", 0, 0, 0.5, 1.1260),                     # still open
             {**_deal(0, "2026-10-01 09:00", 2, 0, 0, 0, 1000.0), "commission": 0.0, "comment": "deposit"}]
    pos = [{"ticket": 2, "symbol": "EURUSD", "mt5_symbol": "EURUSD", "direction": "long", "volume": 0.5,
            "open_utc": "2026-10-06 14:00", "open_price": 1.1260, "profit": 5.0, "swap": 0.0, "comment": ""},
           {"ticket": 3, "symbol": "USDJPY", "mt5_symbol": "USDJPY", "direction": "short", "volume": 1.0,
            "open_utc": "2026-09-01 10:00", "open_price": 150.0, "profit": -2.0, "swap": -1.0, "comment": ""}]
    trades, cash = sc.build_trades(deals, pos)
    by = {t["position_id"]: t for t in trades}
    assert by[1]["direction"] == "short" and by[1]["status"] == "closed" and by[1]["exit_price"] == 189.5
    assert by[1]["net"] == pytest.approx(28.0)
    assert by[2]["status"] == "open" and by[2]["net"] == pytest.approx(4.0)
    assert by[3]["status"] == "open" and by[3]["net"] == pytest.approx(-3.0)   # opened before the window
    assert len(cash) == 1 and cash[0]["comment"] == "deposit"


def test_confluences_before_uses_last_scan_before_entry():
    def scan(t, total):
        return {"asof": pd.Timestamp(t, tz="UTC"), "file": f"scan_{total}.html", "run_type": "pre NY",
                "config_version": "x", "rows": {("CHFJPY", "short"): {"section": "qualified", "total": total}}}
    scans = [scan("2026-10-06 00:05", 2), scan("2026-10-06 12:05", 4), scan("2026-10-07 00:05", 5)]
    cf = sc.confluences_before(scans, "CHFJPY", "short", pd.Timestamp("2026-10-06 13:00", tz="UTC"))
    assert cf["total"] == 4 and cf["age_hours"] == pytest.approx(0.9)
    assert sc.confluences_before(scans, "CHFJPY", "long", pd.Timestamp("2026-10-06 13:00", tz="UTC")) is None
    assert sc.confluences_before(scans, "CHFJPY", "short", pd.Timestamp("2026-10-05 13:00", tz="UTC")) is None


def test_flag_reads_text_booleans_from_older_scans():
    assert sc.flag("False") is False and sc.flag("True") is True
    assert sc.flag(np.bool_(True)) is True and sc.flag(None) is False and sc.flag(0) is False


def test_setup_label_and_confluence_text():
    row = {"c1": True, "c2": False, "c3": False, "c4": True, "c5": "True", "c6": "False"}
    assert sc.setup_label(row) == "C1+C4+C5"
    assert sc.confluences_text(row) == "C1 Trend alignment; C4 Reversal at the zone; C5 2H EMA momentum"
    assert sc.confluences_text(row, met=False) == "C2 Key level zone; C3 Fibonacci retracement; C6 Market structure"
    assert sc.setup_label({}) == "none"


def test_load_scan_history_fixes_text_c5(tmp_path):
    row = {"symbol": "EURUSD", "direction": "long", "section": "developing", "c1": True, "c2": True,
           "c3": False, "c4": False, "c5": "False", "c6": False}
    (tmp_path / "scan_20261006T1205Z_preNY.json").write_text(
        json.dumps({"meta": {"asof_utc": "2026-10-06 12:05"}, "rows": [row]}), encoding="utf-8")
    cf = sc.load_scan_history(tmp_path)[0]["rows"][("EURUSD", "long")]
    assert cf["c5"] is False and cf["setup"] == "C1+C2"


def test_setup_results_groups_closed_trades():
    def tr(net, setup_flags, section="qualified", status="closed"):
        cf = {k: k in setup_flags for k in sc.CONF_KEYS}
        cf.update(section=section, setup=sc.setup_label(cf))
        return {"net": net, "status": status, "confluences": cf}
    trades = [tr(50, {"c1", "c4", "c5"}), tr(-20, {"c1", "c4", "c5"}), tr(-10, {"c2", "c5"}, "developing"),
              tr(99, {"c1"}, status="open"), {"net": 5, "status": "closed", "confluences": None}]
    res = sc.setup_results(trades)
    assert res["closed"] == 3
    top = res["by_setup"][0]
    assert top["group"] == "C1+C4+C5" and top["trades"] == 2 and top["win_rate"] == 0.5 and top["net"] == 30
    c5 = next(c for c in res["by_check"] if c["check"] == "C5")
    assert c5["with"]["trades"] == 3 and c5["without"]["trades"] == 0
    c1 = next(c for c in res["by_check"] if c["check"] == "C1")
    assert c1["with"]["net"] == 30 and c1["without"]["net"] == -10
    assert "Results by setup" in sc.render_setup_results(res)


def test_journal_page_without_mt5(tmp_path):
    p = sc.write_journal(CFG, tmp_path, tmp_path / "out", None, pd.Timestamp("2026-10-06 15:00", tz="UTC"), "test")
    txt = p.read_text(encoding="utf-8")
    assert "Trading journal and balance" in txt and "MT5 account unavailable" in txt
    assert (tmp_path / "logs" / "trade_journal.csv").exists()


# --------------------------------------------------------------- schedule and alerts (cfg-0.9.0)
def test_schedule_every_two_hours_keeps_evening_and_preny():
    runs = sc.scheduled_runs(CFG)
    assert len(runs) == 12 and all(m == 5 for _, m, _ in runs)
    assert (0, 5, "evening") in runs and (12, 5, "preny") in runs and (14, 5, "intraday") in runs
    t, k = sc.next_scheduled(pd.Timestamp("2026-10-07 12:30", tz="UTC"), CFG)
    assert (t, k) == (pd.Timestamp("2026-10-07 14:05", tz="UTC"), "intraday")
    t, k = sc.next_scheduled(pd.Timestamp("2026-10-07 23:10", tz="UTC"), CFG)
    assert (t, k) == (pd.Timestamp("2026-10-08 00:05", tz="UTC"), "evening")
    assert sc.scheduled_runs({"runs": {"every_hours": 0}}) == [(0, 5, "evening"), (12, 5, "preny")]


def test_intraday_run_keeps_daily_bias_from_last_evening_run():
    assert sc.resolve_run("intraday", pd.Timestamp("2026-10-07 16:05", tz="UTC")) == \
        ("intraday", pd.Timestamp("2026-10-07 00:05", tz="UTC"))
    assert sc.resolve_run("intraday", pd.Timestamp("2026-10-07 00:01", tz="UTC"))[1] == pd.Timestamp("2026-10-06 00:05", tz="UTC")


def test_new_graded_only_new_or_upgraded():
    def row(sym, grade, total=4):
        r = sc.Row(sym, "FX", "long")
        r.grade, r.total = grade, total
        return r
    prev = {("EURUSD", "long"): "B", ("GBPUSD", "long"): "A", ("USDJPY", "long"): ""}
    rows = [row("EURUSD", "A"), row("GBPUSD", "B"), row("USDJPY", "B"), row("AUDUSD", ""), row("USDCAD", "B")]
    assert [r.symbol for r in sc.new_graded(prev, rows)] == ["EURUSD", "USDCAD", "USDJPY"]   # A first, then by total/symbol


def test_previous_grades_skips_demo_and_reads_newest(tmp_path):
    def scan(stamp, grade, demo=False):
        (tmp_path / f"scan_{stamp}_intraday.json").write_text(json.dumps(
            {"meta": {"demo": demo}, "rows": [{"symbol": "EURUSD", "direction": "long", "grade": grade}]}), encoding="utf-8")
    scan("20261007T1005Z", "B")
    scan("20261007T1205Z", "A", demo=True)
    name, grades = sc.previous_grades(tmp_path)
    assert name == "scan_20261007T1005Z_intraday.json" and grades == {("EURUSD", "long"): "B"}


def test_calendar_snapshot_reused_within_refresh(tmp_path):
    snap = tmp_path / "data" / "calendar_snapshots"
    snap.mkdir(parents=True)
    (snap / "ff_2026-W41_20261007T1005Z.json").write_text(json.dumps([{"title": "CPI"}]), encoding="utf-8")
    (snap / "ff_2026-W40_20261002T1005Z.json").write_text("[]", encoding="utf-8")
    t, evs = sc.latest_calendar_snapshot(snap, pd.Timestamp("2026-10-07 12:05", tz="UTC"))
    assert t == pd.Timestamp("2026-10-07 10:05", tz="UTC") and evs == [{"title": "CPI"}]
    cfg = {**CFG, "paths": {**CFG["paths"], "data_dir": "data"}}
    evs, status = sc.load_calendar(cfg, tmp_path, pd.Timestamp("2026-10-07 12:05", tz="UTC"), demo=False)
    assert evs == [{"title": "CPI"}] and status.startswith("snapshot from")       # no network call
    assert sc.latest_calendar_snapshot(snap, pd.Timestamp("2026-10-12 08:00", tz="UTC")) is None   # new week


def test_prune_only_old_intraday_html_and_csv(tmp_path):
    for name in ("scan_20260901T1405Z_intraday.html", "scan_20260901T1405Z_intraday.csv",
                 "scan_20260901T1405Z_intraday.json", "scan_20260901T1205Z_preNY.html",
                 "scan_20261006T1405Z_intraday.html"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert sc.prune_outputs(tmp_path, pd.Timestamp("2026-10-07 12:05", tz="UTC"), 14) == 2
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["scan_20260901T1205Z_preNY.html", "scan_20260901T1405Z_intraday.json", "scan_20261006T1405Z_intraday.html"]
