"""Hand-derived checks for the supplied Pine rules; not TradingView runtime parity."""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from gold_es_scaling.cli import COLUMNS, feed_record, load_config, write_feed
from gold_es_scaling.data import validate
from gold_es_scaling.features import atr, ema, patterns, pine_round, structures
from gold_es_scaling.strategy import (
    Book,
    Config,
    Profile,
    ScalingStrategy,
    Setup,
    Trade,
    daily,
    rounded_stop,
    scan,
    sizing,
    targets,
)


def bars(rows, step="15min", start="2026-10-01T00:00Z"):
    frame = pd.DataFrame(
        rows,
        columns=["open", "high", "low", "close"],
        index=pd.date_range(start, periods=len(rows), freq=step),
    )
    frame["time_close"] = frame.index + pd.Timedelta(step)
    return frame


def bar(o=4000, h=4001, low=3999, c=4000, date="2026-10-08T12:00Z"):
    time = pd.Timestamp(date)
    return SimpleNamespace(
        Index=time,
        open=o,
        high=h,
        low=low,
        close=c,
        time_close=time + pd.Timedelta(minutes=15),
    )


def trade(pending=False):
    return Trade(
        1,
        1,
        pending,
        4000,
        3950,
        0.02,
        "BREAKOUT" if pending else "PULLBACK",
        [4090, 4190],
        0,
    )


def state(imp=3):
    return dict(
        trend=1,
        bull=False,
        bear=False,
        sig_l=1,
        sig_s=0,
        stop_l=3990,
        stop_s=4050,
        limit_l=4015,
        limit_s=3985,
        imp_l=imp,
        imp_s=-1,
        text_l="fixture",
        text_s="",
    )


def test_pine_round_and_profile_levels_stops_targets():
    assert pine_round(1.5) == 2 and pine_round(-1.5) == -1
    gold = Config()
    assert rounded_stop(1, 4000, 3900, gold.profile) == 3950
    assert rounded_stop(-1, 4000, float("nan"), gold.profile) == 4050
    assert targets(1, 4005, 50, gold) == [4090, 4190, 4290, 4390, 4490]
    assert targets(-1, 4005, 50, gold) == [3910, 3810, 3710, 3610, 3510]
    assert targets(1, 4089, 50, gold)[0] == 4190  # 4090 is less than 0.5R away
    assert targets(1, 6501, 50, Config(profile=Profile.es())) == [
        6595,
        6695,
        6795,
        6895,
        6995,
    ]


@pytest.mark.parametrize(
    "equity,risk,qty",
    [(10000, 1, 0.02), (12500, 1.5, 0.03), (15000, 2, 0.06), (20000, 3, 0.12)],
)
def test_equity_tiers_and_floor_sizing(equity, risk, qty):
    pct, amount, size, micro = sizing(equity, 50, Config())
    assert (
        pct == risk
        and amount == equity * risk / 100
        and size == pytest.approx(qty)
        and micro is None
    )
    assert sizing(10000, 10, Config(profile=Profile.es()))[2:] == (0, 2)


def test_pine_first_close_ema_and_first_range_atr():
    assert ema(np.array([10, 12, 14.0]), 3).tolist() == [10, 11, 12.5]
    frame = bars(
        [
            (9, 10, 8, 9),
            (9, 12, 9, 11),
            (11, 13, 10, 12),
            (12, 15, 11, 14),
            (14, 16, 13, 15),
        ]
    )
    result = atr(frame, 3)
    assert np.isnan(result[1])
    assert result[2] == pytest.approx(8 / 3)
    assert result[3] == pytest.approx(28 / 9)
    assert result[4] == pytest.approx(83 / 27)


def test_structure_confirmed_pivots_and_reversal_need_lower_high():
    frame = bars(
        [
            (9, 10, 8, 9),
            (12, 13, 11, 12),
            (10, 11, 9, 10),
            (14, 15, 10, 14),
            (12, 14, 11, 12),
            (10, 11, 8, 10),
            (12, 13, 10, 12),
            (10, 12, 9, 10),
            (7, 10, 6, 7),
        ]
    )
    result = structures(frame, 1)
    assert np.isnan(result[1]["high"])  # high 13 not confirmed yet
    assert result[2]["high"] == 13
    assert result[3]["trend"] == 1 and result[3]["bull"]
    assert result[7]["high"] == 13 and result[7]["prev_high"] == 15
    assert result[8]["trend"] == -1 and result[8]["bear"]
    assert result[:6] == structures(
        frame.iloc[:6], 1
    )  # same NaN object identity isn't used beyond warmup


def test_candle_precedence_and_daily_center_break_not_zone_edge():
    frame = bars([(100, 101, 98, 99), (98.5, 101.5, 98, 101)])
    pats, _ = patterns(frame)
    assert pats[1][0] == "Bullish engulfing"
    daily_frame = bars([(2999, 3002, 2998, 2999), (2999, 3003, 2998, 3001)], step="1D")
    result = daily(daily_frame, Config())
    assert result[1]["break_l"]  # source f_daily uses center 3000, not upper edge 3015


def test_pending_fill_does_not_check_stop_on_fill_bar():
    engine = ScalingStrategy()
    su = Setup(trades=[trade(True)])
    book = Book()
    events = []
    engine.manage(su, book, "A", bar(h=4100, low=3940), 1, 1, state(), True, 8, events)
    assert [event["event"] for event in events] == ["LIMIT_FILLED"]
    assert not su.trades[0].pending and book.pnl == 0
    engine.manage(su, book, "A", bar(h=4000, low=3940), 2, 1, state(), False, 8, events)
    assert not su.trades and book.pnl == -100


def test_original_stop_has_priority_over_same_bar_be_and_target():
    engine = ScalingStrategy()
    su = Setup(trades=[trade()])
    book = Book()
    events = []
    engine.manage(su, book, "B", bar(h=4100, low=3940), 1, 1, state(), True, 1, events)
    assert [event["event"] for event in events] == ["STOP_LOSS"]
    assert book.pnl == -100 and su.losses == 1 and su.rearm == 1


def test_breakeven_is_not_retested_on_its_activation_bar():
    engine = ScalingStrategy()
    su = Setup(trades=[trade()])
    book = Book()
    events = []
    engine.manage(su, book, "B", bar(h=4003, low=3990), 1, 1, state(), True, 1, events)
    assert len(su.trades) == 1 and su.trades[0].stop == 4000
    assert [event["event"] for event in events] == ["MOVE_TO_BREAKEVEN"]
    engine.manage(su, book, "B", bar(h=4002, low=3995), 2, 1, state(), True, 1, events)
    assert not su.trades and su.breakevens == 1 and book.pnl == 0


def test_tp_alerts_do_not_realize_partials_and_structure_exit_closes_whole_size():
    engine = ScalingStrategy()
    su = Setup(trades=[trade()])
    book = Book()
    events = []
    engine.manage(
        su, book, "B", bar(h=4100, low=3990, c=4095), 1, 1, state(), False, 1, events
    )
    assert su.trades[0].quantity == 0.02 and su.trades[0].tp_hit == 1 and book.pnl == 0
    broken = state() | {"bear": True, "trend": -1}
    engine.manage(
        su,
        book,
        "B",
        bar(o=4090, h=4095, low=4080, c=4090),
        2,
        1,
        broken,
        True,
        1,
        events,
    )
    assert not su.trades and book.pnl == 180 and su.wins == 1


def test_pending_limit_window_is_strictly_greater_than_source_ratio():
    engine = ScalingStrategy()
    su = Setup(trades=[trade(True)])
    book = Book()
    events = []
    engine.manage(
        su, book, "A", bar(h=4100, low=4050, c=4080), 240, 1, state(), False, 8, events
    )
    assert len(su.trades) == 1
    engine.manage(
        su, book, "A", bar(h=4100, low=4050, c=4080), 241, 1, state(), False, 8, events
    )
    assert not su.trades and events[-1]["event"] == "LIMIT_CANCELLED"


def test_scaling_caps_impulse_counts_shared_numbers_and_zero_size_source_alerts():
    engine = ScalingStrategy()
    book = Book()
    su = Setup()
    events = []
    engine.enter(su, book, "B", bar(), 0, 1, state(), True, 1, events)
    engine.enter(su, book, "B", bar(), 1, 1, state(), True, 1, events)
    engine.enter(su, book, "B", bar(), 2, 1, state(), True, 1, events)
    assert len(su.trades) == 2 and book.week == 2
    engine.enter(su, book, "B", bar(), 2, 1, state(4), True, 1, events)
    assert len(su.trades) == 3 and book.week == 3
    other = Setup()
    engine.enter(other, book, "A", bar(), 2, 1, state(), True, 8, events)
    assert other.trades[0].number == 4
    es = ScalingStrategy(Config(profile=Profile.es()))
    e = []
    es.enter(Setup(), Book(), "B", bar(), 0, 1, state(), True, 1, e)
    assert e[0]["quantity_below_minimum"] and e[0]["micro_equivalent"] == 2


def test_breakout_entry_uses_limit_edge_and_reentry_tag():
    engine = ScalingStrategy()
    su = Setup(rearm=1)
    events = []
    s = state() | {"sig_l": 3, "stop_l": 3960}
    engine.enter(su, Book(), "B", bar(c=4030), 0, 1, s, True, 1, events)
    tr = su.trades[0]
    assert tr.pending and tr.entry == 4015 and tr.stop == 3965
    assert events[0]["reentry"] and events[0]["entry_range"] is None


def test_setup_a_does_not_enter_until_its_close():
    engine = ScalingStrategy()
    su = Setup()
    book = Book()
    events = []
    engine.enter(su, book, "A", bar(), 7, 1, state(), False, 8, events)
    assert events == []
    engine.enter(su, book, "A", bar(), 8, 1, state(), True, 8, events)
    assert book.week == 1


def test_validation_and_profile_override(tmp_path):
    frame = bars([(4000, 4002, 3998, 4001)])
    validate(frame)
    with pytest.raises(ValueError):
        validate(pd.concat([frame, frame]))
    with pytest.raises(ValueError):
        Config(chart_seconds=60)
    config = tmp_path / "profile.json"
    config.write_text('{"profile_name":"Gold"}')
    assert load_config(config, "ES").profile.value_per_point == 50
    assert load_config(None, None, "GCM6").profile.value_per_point == 100


def test_feature_prefix_invariance_and_rerun_state_reproducibility():
    rows = [
        (4000 + i * 0.3, 4002 + i * 0.3, 3998 + i * 0.3, 4000.5 + i * 0.3)
        for i in range(80)
    ]
    frame = bars(rows)
    cfg = Config(bias_mode="Buys only", setup_pivot=1)
    full = scan(frame, 14, cfg)
    prefix = scan(frame.iloc[:60], 14, cfg)
    assert json.dumps(full[:60], sort_keys=True) == json.dumps(prefix, sort_keys=True)
    frames = {
        "chart": frame,
        "a": bars(rows, step="2h"),
        "s": bars(rows, step="4h"),
        "daily": bars(rows, step="1D"),
        "weekly": bars(rows, step="7D"),
    }
    one = ScalingStrategy(cfg).evaluate(frames)
    assert one == ScalingStrategy(cfg).evaluate(frames)
    short = {key: value for key, value in frames.items()}
    short["chart"] = frame.iloc[:60]
    assert one[:60] == ScalingStrategy(cfg).evaluate(short)


def test_feed_contains_all_management_events_and_tracks_both_profiles(tmp_path):
    cfg = Config()
    chart = bars([(4000, 4002, 3998, 4001)])
    frames = {"chart": chart}
    latest = dict(
        time_open=chart.index[0].isoformat(),
        time_close=chart.time_close.iloc[0].isoformat(),
        events=[
            {
                "event": "MOVE_TO_BREAKEVEN",
                "setup": "B",
                "trade": {"number": 1, "side": 1},
            }
        ],
        setups={},
        book={"week": 1, "pnl": 0},
    )
    record = feed_record(
        [latest],
        frames,
        cfg,
        symbol="XAUUSD",
        now=chart.time_close.iloc[0],
        synthetic=True,
        replay=True,
    )
    assert record["signal"] == 0 and record["status"] == "FLAT"
    assert json.loads(record["payload_json"])["latest"]["events"] == latest["events"]
    output = tmp_path / "feed.tsv"
    write_feed(output, record)
    lines = output.read_text().splitlines()
    assert lines[0].split("\t") == list(COLUMNS) and len(lines[1].split("\t")) == 26
    assert b"\r\n" in output.read_bytes()


def test_daily_confirmation_updates_on_next_chart_open_not_daily_close():
    rows = [
        (3990, 4000, 3980, 3990),
        (4020, 4030, 4010, 4020),
        (4000, 4010, 3990, 4000),
        (4040, 4050, 4000, 4040),
    ]
    chart = bars(
        [(4040, 4042, 4038, 4040), (4040, 4043, 4039, 4041)], start="2026-09-30T23:45Z"
    )
    frames = {
        "chart": chart,
        "daily": bars(rows, step="1D", start="2026-09-27T00:00Z"),
        "weekly": bars(rows, step="7D", start="2026-08-01T00:00Z"),
        "a": bars([(4040, 4042, 4038, 4040)], step="2h", start="2026-09-30T22:00Z"),
        "s": bars([(4040, 4042, 4038, 4040)], step="4h", start="2026-09-30T20:00Z"),
    }
    history = ScalingStrategy(
        Config(daily_pivot=1, weekly_pivot=1, use_swing=False)
    ).evaluate(frames)
    assert history[0]["auto_bias"] == 0 and history[0]["daily"] == 0
    assert history[1]["auto_bias"] == 1 and history[1]["daily"] == 1


def test_new_york_sunday_resets_numbering_but_keeps_old_pending_trade():
    chart = bars(
        [
            (3990, 4000, 3980, 3990),
            (4005, 4010, 4001, 4005),
            (3995, 4000, 3990, 3995),
            (4020, 4025, 3995, 4020),
        ],
        start="2026-10-09T23:00Z",
    )
    following = bars(
        [(4025, 4030, 4020, 4025)], start="2026-10-12T00:00Z"
    )  # Sunday 20:00 NY
    chart = pd.concat([chart, following])
    flat = [(4000, 4002, 3998, 4000)]
    frames = {
        "chart": chart,
        "a": bars(flat, step="2h", start="2026-10-09T20:00Z"),
        "s": bars(flat, step="4h", start="2026-10-09T16:00Z"),
        "daily": bars(flat, step="1D", start="2026-10-08T00:00Z"),
        "weekly": bars(flat, step="7D", start="2026-10-01T00:00Z"),
    }
    history = ScalingStrategy(
        Config(bias_mode="Buys only", setup_pivot=1, use_swing=False)
    ).evaluate(frames)
    assert history[3]["book"]["week"] == 1
    assert history[3]["events"][0]["event"] == "ENTRY"
    assert history[4]["book"]["week"] == 0
    assert history[4]["setups"]["B"]["trades"][0]["number"] == 1


def test_s_daily_reversal_is_a_distinct_entry_and_max_open_cap_is_enforced():
    chart = bars([(4008, 4010, 4007, 4009)], start="2026-10-08T23:45Z")
    flat = [(4008, 4010, 4007, 4009)]
    frames = {
        "chart": chart,
        "a": bars(flat, step="2h", start="2026-10-08T22:00Z"),
        "s": bars(flat, step="4h", start="2026-10-08T20:00Z"),
        "daily": bars([(4008, 4010, 3980, 4009)], step="1D", start="2026-10-08T00:00Z"),
        "weekly": bars(flat, step="7D", start="2026-09-20T00:00Z"),
    }
    history = ScalingStrategy(Config(bias_mode="Buys only")).evaluate(frames)
    entries = [event for event in history[0]["events"] if event["event"] == "ENTRY"]
    assert (
        len(entries) == 1
        and entries[0]["setup"] == "S"
        and entries[0]["signal_code"] == 4
    )
    assert entries[0]["trade"]["entry"] == 4009 and entries[0]["trade"]["stop"] == 3959
    cfg = Config(max_open=1)
    engine = ScalingStrategy(cfg)
    su = Setup()
    book = Book()
    events = []
    engine.enter(su, book, "B", bar(), 0, 1, state(), True, 1, events)
    engine.enter(su, book, "B", bar(), 1, 1, state(4), True, 1, events)
    assert len(su.trades) == 1 and book.week == 1
