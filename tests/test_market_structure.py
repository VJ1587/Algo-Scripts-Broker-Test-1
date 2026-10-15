"""Tests for the reference market structure implementation (structure/python/market_structure.py)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "structure" / "python"))
import market_structure as ms  # noqa: E402


def zigzag(legs, step_up=0.4, step_dn=-0.45, up=8, dn=5, start=100.0):
    path = np.r_[np.full(up, step_up), np.full(dn, step_dn)]
    c = np.cumsum(np.tile(path, legs)) + start
    return c + 0.2, c - 0.2, c


def test_atr_matches_wilder_seeded_with_first_n():
    h, l, c = np.full(20, 2.0), np.full(20, 1.0), np.full(20, 1.5)
    a = ms.atr(h, l, c, 14)
    assert np.isnan(a[12]) and a[13] == pytest.approx(1.0) and a[19] == pytest.approx(1.0)


def test_uptrend_labels_and_trend():
    st = ms.analyze(*zigzag(8))
    labels = [s.label for s in st.swings]
    assert labels[:2] == ["H", "L"]
    assert set(labels[2:]) == {"HH", "HL"}
    assert st.last_trend == ms.UP
    # every swing is confirmed after its own bar plus the 3-bar window
    assert all(s.confirm >= s.index + 3 for s in st.swings)


def test_downtrend_mirror():
    h, l, c = zigzag(8, step_up=0.45, step_dn=-0.4, up=5, dn=8, start=200.0)
    st = ms.analyze(h, l, c)
    assert st.last_trend == ms.DOWN
    assert {s.label for s in st.swings[2:]} == {"LH", "LL"}


def test_small_wiggle_without_reaction_is_not_a_swing():
    # a 0.1 bump on bars 0.4 tall (ATR 0.4): high 100.3 to low 99.8 is 0.5, under 1.5 x ATR = 0.6
    c = np.r_[np.full(20, 100.0), [100.05, 100.1, 100.05], np.full(10, 100.0)]
    st = ms.analyze(c + 0.2, c - 0.2, c)
    assert not st.swings


def test_close_below_last_higher_low_breaks_uptrend():
    h, l, c = zigzag(8)
    hl = ms.analyze(h, l, c).last_higher_low
    h2, l2, c2 = np.r_[h, hl + 0.1], np.r_[l, hl - 1.0], np.r_[c, hl - 0.5]
    assert ms.analyze(h2, l2, c2).last_trend == ms.RANGE


def test_poi_and_health():
    st = ms.analyze(*zigzag(8))
    hl = st.last_higher_low
    lo, hi, n = ms.poi(st, daily_atr=1.0, box_half=0.1, mid_step=50.0)
    assert lo <= hl - 0.1 and hi >= hl + 0.1 and n >= 1
    assert ms.health(st, hl + 0.5) == "healthy"
    assert ms.health(st, hl - 0.5) == "challenged"
    read = ms.read_market(st, st, st, hl + 0.5)
    assert read["direction"] == ms.UP and read["weekly_agrees"] and read["health"] == "healthy"
