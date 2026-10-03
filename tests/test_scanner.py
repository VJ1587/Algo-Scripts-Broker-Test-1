"""Unit tests for scanner.py. Run: python -m pytest -q"""
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
def test_bullish_engulfing_and_rejection():
    eu = UNIVERSE["EURUSD"]
    df = bars([1.105, 1.106], [1.099, 1.098], o=[1.104, 1.0995], c=[1.100, 1.1055], freq="2h")
    atr2 = np.array([0.004, 0.004])
    assert sc.candle_signal(df, atr2, eu, sc.LONG, CFG["features"]["candle"]) == "bullish engulfing"
    # rejection: long lower wick, small body near high
    df = bars([1.1000, 1.1000], [1.0990, 1.0960], o=[1.0995, 1.0990], c=[1.0995, 1.0998], freq="2h")
    assert sc.candle_signal(df, np.array([0.004, 0.004]), eu, sc.LONG, CFG["features"]["candle"]) == "bullish rejection"


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
def mk(sym, d, section, tech, cot, total):
    r = sc.Row(sym, "FX", d)
    r.section, r.technical, r.cot_points, r.total = section, tech, cot, total
    return r


def test_rank_qualified_first_ties_and_no_padding():
    rows = [mk("EURUSD", "long", "developing", 2, 2, 6), mk("GBPUSD", "long", "qualified", 3, 0, 3),
            mk("AUDUSD", "long", "qualified", 4, 0, 5), mk("NZDUSD", "long", "qualified", 3, 2, 5),
            mk("USDCAD", "long", "not shown", 1, 2, 5)]
    top = sc.rank(rows, 5)
    assert [r.symbol for r in top] == ["AUDUSD", "NZDUSD", "GBPUSD", "EURUSD"]
    assert [r.rank for r in top] == [1, 2, 3, 4]


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
