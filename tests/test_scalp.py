"""Unit tests for futures_scalp_scanner.py. Run: python -m pytest -q"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import futures_scalp_scanner as fs  # noqa: E402

CFG = fs.load_config(Path(__file__).resolve().parents[1] / "scalp_config.yaml")
INST = fs.build_instruments(CFG)
ES = INST["ES"]
S = CFG["setups"]


def frame(c, h=None, l=None, o=None, start="2026-10-07T10:00Z", freq="5min", vol=True, emas=(9, 15, 30, 65, 200)):
    c = np.asarray(c, float)
    h = c + 0.5 if h is None else np.asarray(h, float)
    l = c - 0.5 if l is None else np.asarray(l, float)
    o = np.r_[c[0], c[:-1]] if o is None else np.asarray(o, float)
    df = pd.DataFrame({"open": o, "high": np.maximum.reduce([h, o, c]), "low": np.minimum.reduce([l, o, c]),
                       "close": c}, index=pd.date_range(start, periods=len(c), freq=freq, tz="UTC"))
    if vol:
        df["volume"] = 100.0
    return fs.build_frame(df, CFG, emas=emas, with_vwap=vol)


# ------------------------------------------------------------ helpers / indicators
def test_tick_rounding_and_risk():
    s = fs.make_signal(ES, "x", fs.LONG, "ARMED", "Limit", 6500.13, 6497.6, [8, 12], "2026-10-08T14:00Z")
    assert s.entry == 6500.25 and s.stop == 6497.5
    assert s.targets == [6502.25, 6503.25]
    assert s.risk_ticks == 11 and s.risk_usd == 137.5
    assert INST["CL"].rt(62.004) == 62.0 and INST["GC"].decimals == 1


def test_rsi_extremes():
    assert fs.rsi(np.arange(40, dtype=float))[-1] == 100.0
    assert fs.rsi(np.arange(40, 0, -1, dtype=float))[-1] == 0.0


def test_session_vwap_resets_at_globex_open_and_pivot_uses_prior_session():
    # 17:55, 18:00 and 18:05 ET on Oct 6 (21:55Z, 22:00Z, 22:05Z): a new session starts at 18:00 ET
    idx = pd.DatetimeIndex(["2026-10-06T21:55Z", "2026-10-06T22:00Z", "2026-10-06T22:05Z"])
    df = pd.DataFrame({"open": [10, 20, 30], "high": [10, 20, 30], "low": [10, 20, 30], "close": [10, 20, 30],
                       "volume": [1, 1, 1]}, index=idx, dtype=float)
    v = fs.session_vwap(df, "America/New_York", "18:00")
    assert list(v) == [10, 20, 25]
    assert fs.prior_session_pivot(df, "America/New_York", "18:00") == 10


def test_completed_drops_forming_bar():
    df = frame(np.full(5, 100.0)).df
    asof = df.index[-1] + pd.Timedelta(minutes=2)
    assert len(fs.completed(df, fs.TF_5M, asof)) == 4


# ------------------------------------------------------------ MOMO / Base
def _momo_series(breakout=None):
    c = list(np.linspace(6400, 6450, 40))
    h, l = [x + 2 for x in c], [x - 2 for x in c]
    for _ in range(5):                       # 5-bar base 6450-6451.5
        c.append(6450.75); h.append(6451.5); l.append(6450.0)
    if breakout:
        c.append(6453.0); h.append(6453.5); l.append(6450.5)
    return frame(c, h, l)


def test_momo_armed_both_sides():
    sigs = fs.momo_base(_momo_series(), ES, S["momo"], CFG)
    by = {s.side: s for s in sigs}
    assert by[fs.LONG].status == "ARMED" and by[fs.LONG].entry == 6452.0      # 6451.5 + 2 ticks
    assert by[fs.LONG].stop == 6449.75
    assert by[fs.SHORT].entry == 6449.5 and by[fs.SHORT].stop == 6451.75
    assert by[fs.LONG].targets == [6454.0, 6455.0]


def test_momo_triggered_on_break():
    sigs = fs.momo_base(_momo_series(breakout=True), ES, S["momo"], CFG)
    assert [(s.side, s.status) for s in sigs] == [(fs.LONG, "TRIGGERED")]


# ------------------------------------------------------------ Trend
def test_trend_long_at_9ema():
    c = 6400 + 0.25 * np.arange(260)
    f = frame(c, c + 0.5, c - 0.5)
    sigs = fs.trend_trade(f, ES, S["trend"], CFG)
    assert len(sigs) == 1 and sigs[0].side == fs.LONG
    assert sigs[0].entry2 is not None and sigs[0].entry > sigs[0].entry2
    # The last bar dips to the 9 EMA (1 point below price) -> AT LEVEL
    l2 = c - 0.5
    l2[-1] = c[-1] - 1.5
    assert fs.trend_trade(frame(c, c + 0.5, l2), ES, S["trend"], CFG)[0].status == "AT LEVEL"


def test_trend_rejects_wide_gap():
    c = 6400 + 3.0 * np.arange(260)          # gap ~ 9 points = 36 ticks > 10
    assert fs.trend_trade(frame(c), ES, S["trend"], CFG) == []


# ------------------------------------------------------------ RMA
def test_rma_extension_then_pullback_to_ema30():
    c = list(np.full(240, 6400.0)) + list(6400 + np.arange(1, 21) * 1.0)   # +20 points away from flat EMAs
    f = frame(c)
    p = dict(S["rma"], levels=["ema200"])
    sig = fs.rma_trade(f, ES, p, CFG)
    assert len(sig) == 1 and sig[0].side == fs.LONG and sig[0].status == "ARMED"
    lvl = f.emas[200][-1]
    assert abs(sig[0].entry - lvl) <= ES.tick
    # Now pull back through the level on the last bar
    c2 = c + [lvl + 1]
    l2 = list(np.array(c2) - 0.5)
    l2[-1] = lvl - 0.25
    sig2 = fs.rma_trade(frame(c2, l=l2), ES, p, CFG)
    assert sig2 and sig2[0].status == "AT LEVEL"


def test_rma_needs_minimum_extension():
    c = list(np.full(240, 6400.0)) + [6401, 6402]
    assert fs.rma_trade(frame(c), ES, dict(S["rma"], levels=["ema200"]), CFG) == []


# ------------------------------------------------------------ FFMA
def _ffma_frames(turn: bool):
    c = list(np.full(40, 6400.0)) + list(6400 + np.arange(1, 16) * 4.0)   # straight up 60 points
    f5 = frame(c)
    end = f5.df.index[-1] + pd.Timedelta(minutes=5)
    c1 = [c[-1]] * 30 + ([c[-1] - 0.5] if turn else [c[-1] + 0.25])
    o1 = [c[-1]] * 31
    f1 = frame(c1, o=o1, start=end - pd.Timedelta(minutes=30), freq="1min", vol=False, emas=(9, 15))
    return f5, f1


def test_ffma_watch_then_trigger_on_1m_turn():
    f5, f1 = _ffma_frames(turn=False)
    s = fs.ffma_trade(f5, f1, ES, S["ffma"], CFG)
    assert len(s) == 1 and s[0].side == fs.SHORT and s[0].status == "WATCH"
    assert s[0].stop == f5.df["high"].iloc[-1] + ES.tick
    f5, f1 = _ffma_frames(turn=True)
    s = fs.ffma_trade(f5, f1, ES, S["ffma"], CFG)
    assert s[0].status == "TRIGGERED" and s[0].entry_type == "Market"


# ------------------------------------------------------------ DB / DT
def test_double_bottom_armed_and_at_level():
    down = list(np.linspace(6440, 6420, 20))           # 20-point drop into the first bottom
    up = list(np.linspace(6421, 6426, 6))              # bounce
    back = [6424, 6423]
    c = list(np.full(20, 6440.0)) + down + up + back
    sig = [s for s in fs.dbdt_trade(frame(c), ES, S["dbdt"], CFG) if s.side == fs.LONG]
    assert len(sig) == 1 and sig[0].status == "ARMED" and sig[0].entry == 6419.5
    sig = [s for s in fs.dbdt_trade(frame(c + [6420.0]), ES, S["dbdt"], CFG) if s.side == fs.LONG]
    assert sig[0].status == "AT LEVEL"


def test_double_bottom_needs_prior_move():
    c = list(np.full(20, 6425.0)) + list(np.linspace(6425, 6420, 20)) + list(np.linspace(6421, 6426, 6)) + [6424]
    assert [s for s in fs.dbdt_trade(frame(c), ES, S["dbdt"], CFG) if s.side == fs.LONG] == []


# ------------------------------------------------------------ ORB
def _orb_frame(after):
    """Bars from 09:00 ET; the 09:30-09:45 range is 6500-6505, then `after` closes."""
    pre = [6502.0] * 6
    rng_c = [6501.0, 6504.0, 6502.0]
    c = pre + rng_c + list(after)
    h = [x + 0.5 for x in c]
    l = [x - 0.5 for x in c]
    h[6:9] = [6505.0, 6505.0, 6503.0]
    l[6:9] = [6500.0, 6502.0, 6501.0]
    return frame(c, h, l, start="2026-10-08T13:00Z")


def _asof(f):
    return f.df.index[-1] + pd.Timedelta(minutes=5)


def test_orb_armed_inside_15m_range():
    f = _orb_frame([6502.0, 6503.0])
    sigs = fs.orb_trades(f, ES, S["orb"], CFG, _asof(f))
    assert {(s.side, s.status, s.entry) for s in sigs} == {(fs.LONG, "ARMED", 6505.25), (fs.SHORT, "ARMED", 6499.75)}


def test_orb_momo_triggered_and_trend_pullback():
    f = _orb_frame([6503.0, 6506.0])
    s = fs.orb_trades(f, ES, S["orb"], CFG, _asof(f))
    assert [(x.setup, x.side, x.status) for x in s] == [("ORB MOMO", fs.LONG, "TRIGGERED")]
    f = _orb_frame([6506.0, 6512.0, 6516.0, 6510.0, 6505.2])   # ran 11.5 points, back to the ORB high
    s = fs.orb_trades(f, ES, S["orb"], CFG, _asof(f))
    assert s[0].setup == "ORB Trend" and s[0].status == "AT LEVEL" and s[0].entry == 6505.0
    assert s[0].stop == 6502.5           # 50% of a 5-point ORB (10 ticks) is smaller than 16 ticks


def test_orb_off_outside_rth():
    f = _orb_frame([6502.0])
    assert fs.orb_trades(f, ES, S["orb"], CFG, pd.Timestamp("2026-10-08T21:00Z")) == []


# ------------------------------------------------------------ 1-Min Range Break
def _rb_frames(break_out: bool):
    c5 = 6400 + 0.5 * np.arange(60) + np.tile([0, 0.75, -0.25], 20)       # rising, RSI > 50
    c5[-1] += 1.0                                                          # RSI still rising
    f5 = frame(c5, emas=(9, 15, 30, 65, 200))
    c = list(6400 + 0.25 * np.arange(40))                   # 1m uptrend into a flat top
    top = c[-1] + 1.0
    hh = [x + 0.25 for x in c] + [top, top - 0.25, top, top]
    ll = [x - 0.25 for x in c] + [top - 1.25, top - 1.0, top - 1.0, top - 0.75]
    cc = c + [top - 0.25, top - 0.5, top - 0.25, top - 0.25]
    if break_out:
        hh.append(top + 0.75); ll.append(top - 0.25); cc.append(top + 0.5)
    f1 = frame(cc, hh, ll, freq="1min", vol=False, emas=(9, 15))
    return f5, f1, top


def test_range_break_armed_and_triggered():
    f5, f1, top = _rb_frames(False)
    s = fs.range_break(f5, f1, ES, S["range_break"], CFG)
    assert len(s) == 1 and s[0].side == fs.LONG and s[0].status == "ARMED"
    assert s[0].entry == top + 0.25 and s[0].entry_type == "Buy Stop Limit"
    f5, f1, top = _rb_frames(True)
    s = fs.range_break(f5, f1, ES, S["range_break"], CFG)
    assert s[0].status == "TRIGGERED" and s[0].entry == top + 0.25


# ------------------------------------------------------------ end to end
def test_demo_scan_runs(tmp_path):
    asof = pd.Timestamp("2026-10-08T15:10Z")
    sigs, errs = fs.run_scan(CFG, fs.DemoSource(asof), list(INST.values()), asof, fs.SETUP_KEYS)
    assert not [e for e in errs if "only" not in e and "closed" not in e]   # data warnings only
    assert all(s.status in fs.STATUS_RANK for s in sigs)
    p = fs.write_outputs(sigs, errs, asof, tmp_path, {})
    assert p.exists() and (tmp_path / "latest.json").exists()
    assert fs.main(["--demo", "--out-dir", str(tmp_path)]) == 0
