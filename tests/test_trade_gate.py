"""Unit tests for trade_gate.py. Run: python -m pytest -q"""
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scanner as sc  # noqa: E402
import trade_gate as tg  # noqa: E402

CFG = sc.load_config(ROOT / "scanner_config.yaml")
UNIVERSE = {i.symbol: i for i in sc.build_universe(CFG)}
NO_NEWS = tg.newsmod.parse_events([], pd.Timestamp("2026-10-06", tz="UTC"))
SETTINGS = {"allow_live_algo": False, "max_scan_age_hours": 1e9, "ladder_expiry_hours": 24, "max_quote_age_seconds": 30}


def row(symbol="EURUSD", direction="long", A=1.1000, B=1.1500, section="qualified"):
    return {"symbol": symbol, "direction": direction, "section": section, "rank": 1, "total": 4,
            "c1": True, "c2": True, "c3": False, "c4": True, "c5": True, "c6": False,
            "zone_level": 1.125, "zone_low": 1.1235, "zone_high": 1.1265, "candle": "2H hammer",
            "ref_price": 1.126, "flags": [], "calendar": {},
            "impulse": {} if A is None else {"A": A, "B": B}}


# --------------------------------------------------------------- levels (same formulas as broker_v11_algo._plan)
def test_fx_long_levels_match_v1_rules():
    entries, stop, tp1, tp2, notes = tg.propose_levels(row(), UNIVERSE["EURUSD"])
    assert entries == [Decimal("1.13090"), Decimal("1.12500"), Decimal("1.11910")]
    assert stop == Decimal("1.10535")                       # 89.3 % retracement, nearest tick
    assert tp1 == Decimal("1.16350") and tp2 == Decimal("1.18090")
    assert notes == [] and not tg.validate_levels("long", entries, stop, tp1, tp2)


def test_fx_short_levels_mirror():
    entries, stop, tp1, tp2, _ = tg.propose_levels(row(direction="short", A=1.1500, B=1.1000), UNIVERSE["EURUSD"])
    assert entries == [Decimal("1.11910"), Decimal("1.12500"), Decimal("1.13090")]
    assert stop == Decimal("1.14465") and tp1 == Decimal("1.08650")
    assert not tg.validate_levels("short", entries, stop, tp1, tp2)


def test_gold_needs_owner_stop_and_flags_short_test():
    entries, stop, *_, notes = tg.propose_levels(row("XAUUSD", "short", 4300.0, 4150.0), UNIVERSE["XAUUSD"])
    assert len(entries) == 3 and stop is None
    assert any("structural" in n for n in notes) and any("long only" in n for n in notes)


def test_no_impulse_means_owner_enters_levels():
    entries, stop, tp1, tp2, notes = tg.propose_levels(row(A=None), UNIVERSE["EURUSD"])
    assert entries == [] and stop is None and "yourself" in notes[0]


def test_validate_catches_wrong_side_stop_and_target():
    errs = tg.validate_levels("long", [Decimal("1.13")], Decimal("1.14"), Decimal("1.12"), None)
    assert any("stop" in e for e in errs) and any("TP1" in e for e in errs)


# --------------------------------------------------------------- sizing
def test_sizing_rounds_down_and_stays_inside_risk():
    spec = tg.SizeSpec(Decimal("100000"), Decimal("0.01"), Decimal("700"), Decimal("0.01"))
    entries, stop, *_ = tg.propose_levels(row(), UNIVERSE["EURUSD"])
    allowed = Decimal("2000")                                 # 2 % of 100,000
    lots, why = tg.size_lots(entries, list(tg.RULES.ladder_weights), stop, spec, Decimal(0), allowed)
    assert not why and all(q == q.quantize(Decimal("0.01")) for q in lots)
    risk = tg.plan_risk(entries, lots, stop, spec, Decimal(0))
    assert risk <= allowed and risk > allowed * Decimal("0.95")


def test_split_for_targets():
    assert tg.split_for_targets(Decimal("0.30"), Decimal("0.01"), Decimal("0.01")) == [(Decimal("0.15"), "tp1"), (Decimal("0.15"), "tp2")]
    assert tg.split_for_targets(Decimal("0.01"), Decimal("0.01"), Decimal("0.01")) == [(Decimal("0.01"), "tp1")]


# --------------------------------------------------------------- permissions (demo first)
def test_algo_permission_matrix():
    demo = tg.Account("d", "Demo", "mt5", allow_algo=True)
    live = tg.Account("l", "Live", "mt5", allow_algo=True)
    manual = tg.Account("m", "Futures", "manual")
    off = tg.Account("o", "Off", "mt5", allow_algo=False)
    assert tg.algo_permission(demo, SETTINGS, "demo", True) == (True, "")
    assert not tg.algo_permission(live, SETTINGS, "real", True)[0]
    assert tg.algo_permission(live, {**SETTINGS, "allow_live_algo": True}, "real", True)[0]
    assert not tg.algo_permission(demo, SETTINGS, "demo", False)[0]       # terminal Algo Trading off
    assert not tg.algo_permission(manual, SETTINGS, None, None)[0]
    assert not tg.algo_permission(off, SETTINGS, "demo", True)[0]


def test_apply_mods_rounds_and_records():
    t = tg.Ticket("x", "s", row(), tg.Account("a", "A", "manual"), entries=[Decimal("1.1309"), Decimal("1.125"), Decimal("1.1191")])
    t.stop = Decimal("1.10535")
    errs = tg.apply_mods(t, "stop=1.104999 e2=1.1251 lots=0.1,0.2,0.3 bogus", Decimal("0.00001"))
    assert t.stop == Decimal("1.10500") and t.entries[1] == Decimal("1.12510") and t.lots_manual
    assert len(t.modifications) == 3 and errs and "bogus" in errs[0]


def test_accounts_example_loads():
    accts, settings = tg.load_accounts(ROOT / "accounts.example.yaml")
    assert {a.platform for a in accts} == {"mt5", "manual"} and settings["allow_live_algo"] is False


# --------------------------------------------------------------- full flow with a fake broker
class FakeMt5:
    TRADE_ACTION_PENDING, TRADE_ACTION_DEAL, TRADE_ACTION_REMOVE = 5, 1, 8
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT = 0, 1, 2, 3
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TIME_GTC, ORDER_TIME_SPECIFIED = 0, 2
    TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED = 10009, 10008

    def __init__(self):
        self.sent, self.checked = [], []

    def symbol_info(self, s):
        return SimpleNamespace(name=s, trade_tick_value=1.0, trade_tick_size=0.00001, volume_min=0.01, volume_max=700.0,
                               volume_step=0.01, trade_stops_level=1, point=0.00001, filling_mode=1, expiration_mode=15)

    def symbol_info_tick(self, s):
        return SimpleNamespace(time=0, bid=1.1350, ask=1.1351)

    def order_check(self, r):
        self.checked.append(r)
        return SimpleNamespace(retcode=0, comment="Done")

    def order_send(self, r):
        self.sent.append(r)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_PLACED, order=1000 + len(self.sent), comment="Placed")


class FakeSession:
    def __init__(self, acct, mode="demo", algo_on=True):
        self.acct, self.mode, self.algo_on, self.error = acct, mode, algo_on, ""
        self.mt5 = FakeMt5()

    def open(self):
        return True

    def close(self):
        pass

    def size_spec(self, symbol):
        return tg.SizeSpec(Decimal("100000"), Decimal("0.01"), Decimal("700"), Decimal("0.01"), Decimal("0.00001"), "fake")

    def capital(self):
        return Decimal("100000"), Decimal(0), []

    def quote(self, symbol):
        return Decimal("1.1350"), Decimal("1.1351"), 1.0


def scan_file(tmp_path, rows):
    p = tmp_path / "scan.json"
    p.write_text(json.dumps({"meta": {"asof_utc": "2026-10-06 12:05", "run_type": "pre NY", "config_version": "cfg-0.5.0",
                                      "stamp": "20261006T1205Z", "demo": False}, "rows": rows}), encoding="utf-8")
    return p


def run_gate(tmp_path, answers, accounts, rows=None, mode="demo", news_df=None):
    said = []
    it = iter(answers)
    prompter = tg.Prompter(lambda q: next(it), said.append)
    sessions = []

    def factory(a):
        s = FakeSession(a, mode)
        sessions.append(s)
        return s
    gate = tg.Gate(accounts, SETTINGS, CFG, base=tmp_path, prompter=prompter, session_factory=factory,
                   news_loader=lambda: news_df if news_df is not None else NO_NEWS)
    done = gate.run(scan_file(tmp_path, rows or [row()]))
    log = pd.read_csv(tmp_path / "logs" / "trade_decisions.csv")
    return done, log, said, sessions


def test_skip_is_logged_and_nothing_sent(tmp_path):
    done, log, _, sessions = run_gate(tmp_path, ["n"], [tg.Account("d", "Demo", "mt5", allow_algo=True)])
    assert done == [] and list(log["decision"]) == ["skipped"] and sessions == []


def test_algo_on_demo_sends_only_after_confirm(tmp_path):
    acct = tg.Account("d", "Demo", "mt5", allow_algo=True)
    done, log, said, sessions = run_gate(tmp_path, ["y", "1", "", "a", "CONFIRM"], [acct])
    assert log["decision"].iloc[-1] == "sent (algo)" and len(done) == 1
    m = sessions[0].mt5
    assert len(m.checked) == len(m.sent) == 6                   # 3 legs x (TP1 half + TP2 half)
    assert all(r["magic"] == tg.MAGIC and r["sl"] == 1.10535 for r in m.sent)
    assert {r["tp"] for r in m.sent} == {1.1635, 1.1809}


def test_anything_but_confirm_cancels(tmp_path):
    acct = tg.Account("d", "Demo", "mt5", allow_algo=True)
    done, log, _, sessions = run_gate(tmp_path, ["y", "1", "", "a", "confirm"], [acct])
    assert done == [] and log["decision"].iloc[-1] == "cancelled" and sessions[0].mt5.sent == []


def test_live_account_cannot_use_algo_by_default(tmp_path):
    acct = tg.Account("l", "Live", "mt5", allow_algo=True)
    done, log, said, sessions = run_gate(tmp_path, ["y", "1", "", "a", "CONFIRM"], [acct], mode="real")
    assert log["decision"].iloc[-1] == "approved (manual)" and sessions[0].mt5.sent == []
    assert any("demo first" in s for s in said)


def test_modifications_and_manual_account_logged(tmp_path):
    manual = tg.Account("f", "Futures", "manual")
    answers = ["y", "1", "50000", "0", "stop=1.1040 lots=0.1,0.1,0.2", "", "m", "CONFIRM"]
    done, log, _, _ = run_gate(tmp_path, answers, [manual])
    last = log.iloc[-1]
    assert last["decision"] == "approved (manual)" and "stop 1.10535 -> 1.10400" in last["modifications"]
    assert (tmp_path / "tickets" / f"{last['ticket_id']}.json").exists()


def test_risk_above_ceiling_blocks_algo_and_needs_override(tmp_path):
    acct = tg.Account("d", "Demo", "mt5", allow_algo=True)
    answers = ["y", "1", "lots=5,5,5", "", "a", "no", ]
    done, log, said, sessions = run_gate(tmp_path, answers, [acct])
    assert log["decision"].iloc[-1] == "cancelled" and sessions[0].mt5.sent == []
    assert any("ceiling" in s for s in said)


def test_no_impulse_starts_market_at_live_price_and_owner_sets_stop(tmp_path):
    acct = tg.Account("d", "Demo", "mt5", allow_algo=True)
    answers = ["y", "1", "stop=1.1300 tp1=1.1450 tp2=1.1500", "", "m", "CONFIRM"]
    done, log, said, _ = run_gate(tmp_path, answers, [acct], rows=[row(A=None)])
    last = log.iloc[-1]
    assert last["decision"] == "approved (manual)" and last["route"] == "M"
    assert str(last["entries"]) == "1.1351"                      # long at the live ask
    assert float(last["risk"]) <= 2000.0 + 0.01                   # sized inside 2 % of 100,000


def test_zero_balance_gives_no_budget_note(tmp_path):
    class Broke(FakeSession):
        def capital(self):
            return Decimal(0), Decimal(0), []
    said = []
    it = iter(["y", "1", "cancel"])
    gate = tg.Gate([tg.Account("d", "Demo", "mt5", allow_algo=True)], SETTINGS, CFG, base=tmp_path,
                   prompter=tg.Prompter(lambda q: next(it), said.append), session_factory=lambda a: Broke(a),
                   news_loader=lambda: NO_NEWS)
    gate.run(scan_file(tmp_path, [row()]))
    assert any("no risk budget" in s for s in said)


def test_red_news_inside_window_blocks_algo(tmp_path):
    now = pd.Timestamp.now(tz="UTC")
    news_df = tg.newsmod.parse_events([{"title": "Non-Farm Employment Change", "country": "USD",
                                        "date": (now + pd.Timedelta(minutes=10)).isoformat(), "impact": "High"}], now)
    acct = tg.Account("d", "Demo", "mt5", allow_algo=True)
    done, log, said, sessions = run_gate(tmp_path, ["y", "1", "", "a", "CONFIRM"], [acct], news_df=news_df)
    assert log["decision"].iloc[-1] == "approved (manual)" and sessions[0].mt5.sent == []
    assert any("news blackout" in s for s in said) and "blackout" in log["notes"].iloc[-1]


def test_red_news_for_other_currency_does_not_block(tmp_path):
    now = pd.Timestamp.now(tz="UTC")
    news_df = tg.newsmod.parse_events([{"title": "BOJ Policy Rate", "country": "JPY",
                                        "date": (now + pd.Timedelta(minutes=10)).isoformat(), "impact": "High"}], now)
    acct = tg.Account("d", "Demo", "mt5", allow_algo=True)
    _, log, _, sessions = run_gate(tmp_path, ["y", "1", "", "a", "CONFIRM"], [acct], news_df=news_df)
    assert log["decision"].iloc[-1] == "sent (algo)" and sessions[0].mt5.sent