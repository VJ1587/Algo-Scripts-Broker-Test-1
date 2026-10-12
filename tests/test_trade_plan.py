"""Hand-calculated tests for trade_plan.py (cfg-0.14.0). Run: python -m pytest -q tests/test_trade_plan.py"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scanner as sc  # noqa: E402
import trade_plan as tp  # noqa: E402

CFG = sc.load_config(Path(__file__).resolve().parents[1] / "scanner_config.yaml")
UNIVERSE = {i.symbol: i for i in sc.build_universe(CFG)}


def test_rr_grade_boundaries():
    assert tp.rr_grade(3.0) == "A"
    assert tp.rr_grade(2.99) == "B"
    assert tp.rr_grade(2.0) == "B"
    assert tp.rr_grade(1.99) == "C"
    assert tp.rr_grade(1.0) == "C"
    assert tp.rr_grade(0.99) == tp.FAIL
    assert tp.rr_grade(None) == ""


def test_long_usdcad_case_fails_below_one_to_one():
    # ref 1.42559, 10 pip range 1.42459-1.42559, swing low 1.42067 - 3 pip buffer = stop 1.42037
    # TP1 nearest of swing high 1.42992 and next level 1.45000 = 1.42992
    # risk 1.42559 - 1.42037 = 0.00522, reward 1.42992 - 1.42559 = 0.00433, R:R 0.8295 -> 0.83
    p = tp.build("long", 1.42559, 0.0010, 0.0003,
                 stops=[("Fib 78.6%", None), ("4H swing", 1.42067)],
                 targets=[("Fib -27%", None), ("4H swing", 1.42992), ("key level", 1.45)])
    assert p["entry_low"] == pytest.approx(1.42459)
    assert p["entry_high"] == pytest.approx(1.42559)
    assert p["stop"] == pytest.approx(1.42037)
    assert p["stop_source"] == "4H swing"
    assert p["tp1"] == pytest.approx(1.42992)
    assert p["risk"] == pytest.approx(0.00522)
    assert p["reward"] == pytest.approx(0.00433)
    assert p["rr"] == 0.83
    assert p["rr_grade"] == tp.FAIL
    assert "not a trade" in p["note"]


def test_short_gold_case_grades_c():
    # ref 4194.645, $2 range 4194.645-4196.645 (worst = 4194.645), swing high 4227.53 + $1 = stop 4228.53
    # TP1 nearest of next level 4150 and swing low 4100 = 4150
    # risk 33.885, reward 44.645, R:R 1.3175 -> 1.32
    p = tp.build("short", 4194.645, 2.0, 1.0,
                 stops=[("Daily swing", 4227.53)],
                 targets=[("key level", 4150.0), ("Daily swing", 4100.0)])
    assert (p["entry_low"], p["entry_high"]) == (pytest.approx(4194.645), pytest.approx(4196.645))
    assert p["stop"] == pytest.approx(4228.53)
    assert p["tp1"] == 4150.0
    assert p["rr"] == 1.32
    assert p["rr_grade"] == "C"


def test_three_to_one_is_a():
    # long ref 1.1000, range 1.0990-1.1000, stop 1.0950 + buffer 0 -> risk 0.0050; TP1 1.1150 -> reward 0.0150
    p = tp.build("long", 1.1000, 0.0010, 0.0, [("4H swing", 1.0950)], [("key level", 1.1150)])
    assert p["rr"] == 3.0 and p["rr_grade"] == "A"


def test_stop_inside_entry_range_is_skipped_for_next_candidate():
    # Fib stop 1.0995 sits inside the 1.0990-1.1000 range, so the 4H swing 1.0950 is used: 1.0950 - 0.0003
    p = tp.build("long", 1.1000, 0.0010, 0.0003,
                 [("Fib 78.6%", 1.0995), ("4H swing", 1.0950)], [("key level", 1.1250)])
    assert p["stop_source"] == "4H swing"
    assert p["stop"] == pytest.approx(1.0947)


def test_targets_behind_entry_are_ignored_and_missing_ones_reported():
    p = tp.build("long", 1.1000, 0.0010, 0.0, [("4H swing", 1.0950)], [("4H swing", 1.0980)])
    assert p["tp1"] is None and p["rr"] is None and p["rr_grade"] == ""
    assert p["note"] == "no target level beyond the entry"
    p = tp.build("short", 1.1000, 0.0010, 0.0, [("4H swing", 1.0950)], [("key level", 1.0750)])
    assert p["stop"] is None and p["note"] == "no structural stop beyond the entry range"


def test_next_grid_level():
    assert tp.next_grid_level(1.42559, "long", 0.025) == pytest.approx(1.45)
    assert tp.next_grid_level(1.42559, "short", 0.025) == pytest.approx(1.425)
    assert tp.next_grid_level(1.4250, "long", 0.025) == pytest.approx(1.45)    # on a level: the next one
    assert tp.next_grid_level(1.4250, "short", 0.025) == pytest.approx(1.40)
    assert tp.next_grid_level(4194.645, "short", 50) == pytest.approx(4150)


def test_last_swing_takes_most_recent_beyond_threshold():
    seq = [sc.Pivot(1, "L", 1.4238, 3), sc.Pivot(2, "H", 1.4285, 4), sc.Pivot(3, "L", 1.4206, 5),
           sc.Pivot(4, "H", 1.4281, 6), sc.Pivot(5, "L", 1.4247, 7)]
    assert tp.last_swing(seq, "L", 1.4246, -1) == 1.4206   # newest first: 1.4247 is not below 1.4246, 1.4206 is
    assert tp.last_swing(seq, "H", 1.4256, 1) == 1.4281
    assert tp.last_swing(seq, "H", 1.4290, 1) is None


def test_width_keys_and_config():
    assert tp.width_key(UNIVERSE["USDCAD"].asset, UNIVERSE["USDCAD"].quote) == "fx"
    assert tp.width_key(UNIVERSE["USDJPY"].asset, UNIVERSE["USDJPY"].quote) == "jpy"
    assert tp.width_key(UNIVERSE["XAUUSD"].asset, UNIVERSE["XAUUSD"].quote) == "gold"
    assert tp.width_key(UNIVERSE["ES"].asset, UNIVERSE["ES"].quote) == "spx"
    assert tp.width_key(UNIVERSE["CL"].asset, UNIVERSE["CL"].quote) == "oil"
    w = CFG["trade_plan"]["entry_width"]
    assert w["fx"] == 0.0010 and w["jpy"] == 0.10 and w["gold"] == 2.0 and w["spx"] == 5.0 and w["oil"] == 1.0
    assert w["spx"] == 20 * UNIVERSE["ES"].tick   # 20 ES ticks of 0.25


def test_trade_plan_for_reversal_uses_nearest_target():
    r = sc.Row("EURUSD", "FX major", "long")
    r.ref_price = 1.1000
    r.plan = {"stop_786": 1.0960, "tp1": 1.1100}
    seq = [sc.Pivot(1, "L", 1.0940, 3), sc.Pivot(2, "H", 1.1060, 4)]
    p = sc.trade_plan_for(r, UNIVERSE["EURUSD"], CFG["trade_plan"], seq, "4H")
    # range 1.0990-1.1000; stop Fib 1.0960 - 0.0003 = 1.0957; TP1 nearest of 1.1100, swing 1.1060, level 1.1250 = 1.1060
    # risk 0.0043, reward 0.0060, R:R 1.395 -> 1.4, C
    assert p["entry_low"] == pytest.approx(1.0990)
    assert p["stop"] == pytest.approx(1.0957) and p["stop_source"] == "Fib 78.6%"
    assert p["tp1"] == pytest.approx(1.1060) and p["tp1_source"] == "4H swing"
    assert p["rr"] == 1.4 and p["rr_grade"] == "C" and p["trade_type"] == "Key level reversal"


def test_tp_rule_first_takes_priority_order_not_nearest():
    targets = [("Fib -27%", 1.1150), ("4H swing", 1.1060), ("key level", 1.1250)]
    near = tp.build("long", 1.1000, 0.0010, 0.0, [("4H swing", 1.0950)], targets)
    first = tp.build("long", 1.1000, 0.0010, 0.0, [("4H swing", 1.0950)], targets, tp_rule="first")
    assert near["tp1_source"] == "4H swing" and near["rr"] == 1.2              # 0.0060 / 0.0050
    assert first["tp1_source"] == "Fib -27%" and first["rr"] == 3.0 and first["rr_grade"] == "A"   # 0.0150 / 0.0050
    # first candidate missing or behind the entry: falls to the next in order
    first = tp.build("long", 1.1000, 0.0010, 0.0, [("4H swing", 1.0950)],
                     [("Fib -27%", None), ("4H swing", 1.0990), ("key level", 1.1250)], tp_rule="first")
    assert first["tp1_source"] == "key level"
    with pytest.raises(ValueError):
        tp.build("long", 1.1, 0.001, 0.0, [], [], tp_rule="best")


def test_scanner_trade_type():
    assert tp.scanner_trade_type("long", True, "neutral") == tp.TREND_PULLBACK
    assert tp.scanner_trade_type("long", False, "long") == tp.TREND_PULLBACK
    assert tp.scanner_trade_type("long", False, "short") == tp.KEY_LEVEL_REVERSAL
    assert tp.scanner_trade_type("short", False, "neutral") == tp.KEY_LEVEL_REVERSAL


def test_trade_plan_for_trend_pullback_targets_fib_extension():
    r = sc.Row("EURUSD", "FX major", "long")
    r.ref_price, r.daily_bias, r.c1 = 1.1000, "long", True
    r.plan = {"stop_786": 1.0960, "tp1": 1.1100}
    seq = [sc.Pivot(1, "L", 1.0940, 3), sc.Pivot(2, "H", 1.1060, 4)]
    p = sc.trade_plan_for(r, UNIVERSE["EURUSD"], CFG["trade_plan"], seq, "4H")
    # same levels as the reversal case, but TP1 = Fib -27% 1.1100: reward 0.0100, risk 0.0043, R:R 2.33 -> B
    assert p["trade_type"] == "Trend pullback"
    assert p["tp1"] == pytest.approx(1.1100) and p["tp1_source"] == "Fib -27%"
    assert p["rr"] == 2.33 and p["rr_grade"] == "B"
