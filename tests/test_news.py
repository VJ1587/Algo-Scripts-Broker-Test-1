"""Unit tests for news.py (Forex Factory red-folder events and the reaction tracker)."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import broker_v11_algo as bv  # noqa: E402
import news  # noqa: E402

T0 = pd.Timestamp("2026-10-02 12:30", tz="UTC")
RAW = [{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00",
        "impact": "High", "forecast": "150K", "previous": "142K"},
       {"title": "Retail Sales m/m", "country": "CAD", "date": "2026-10-02T08:30:00-04:00", "impact": "Medium"},
       {"title": "Bank Holiday", "country": "CHF", "date": "2026-10-02T00:00:00-04:00", "impact": "Holiday"}]


def test_parse_converts_eastern_to_utc_and_keeps_fields():
    df = news.parse_events(RAW, T0)
    nfp = df.iloc[0]
    assert nfp["event_time"] == T0 and nfp["currency"] == "USD" and nfp["forecast"] == "150K"
    assert list(news.red(df)["title"]) == ["Non-Farm Employment Change"]


def test_archive_keeps_first_seen_time_and_latest_forecast(tmp_path):
    snap = tmp_path / "snaps"
    snap.mkdir()
    (snap / "ff_2026-W40_20260928T0005Z.json").write_text(json.dumps(RAW), encoding="utf-8")
    later = [dict(RAW[0], forecast="155K")]
    (snap / "ff_2026-W40_20260930T1205Z.json").write_text(json.dumps(later), encoding="utf-8")
    df = news.load_archive(snap)
    nfp = df[df["title"] == "Non-Farm Employment Change"].iloc[0]
    assert nfp["published_at"] == pd.Timestamp("2026-09-28 00:05", tz="UTC") and nfp["forecast"] == "155K"
    assert len(df) == 3


def test_events_near_window_and_currency():
    df = news.parse_events(RAW, T0)
    assert len(news.events_near(df, ["EUR", "USD"], T0 + pd.Timedelta(minutes=29), 30)) == 1
    assert news.events_near(df, ["EUR", "USD"], T0 + pd.Timedelta(minutes=31), 30).empty
    assert news.events_near(df, ["CAD"], T0, 30).empty          # medium impact is not red


def test_backtest_export_is_read_by_broker_news_calendar(tmp_path):
    df = news.parse_events(RAW, pd.Timestamp("2026-09-28", tz="UTC"))
    p = tmp_path / "news.csv"
    assert news.export_backtest(df, p) == 1
    cal = bv.NewsCalendar.from_csv(str(p))
    t_ns = int(T0.value)
    assert cal.in_blackout(("EUR", "USD"), t_ns) and not cal.in_blackout(("EUR", "JPY"), t_ns)


def m5(start, closes):
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="UTC")
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c, "high": c + 0.0002, "low": c - 0.0002, "close": c}, index=idx)


def test_reaction_signs_by_base_and_quote(tmp_path):
    # USD event at 12:30: EURUSD falls 30 pips (USD up), USDJPY rises 50 pips (USD up)
    start = T0 - pd.Timedelta(hours=1)
    n_before, n_after = 12, 60
    eur = m5(start, [1.1300] * n_before + list(np.linspace(1.1290, 1.1270, n_after)))
    jpy = m5(start, [150.00] * n_before + list(np.linspace(150.20, 150.50, n_after)))
    bars = {"EURUSD": eur, "USDJPY": jpy}
    df = news.parse_events(RAW, T0)
    log = tmp_path / "r.csv"
    added = news.update_reactions(df, ["EURUSD", "USDJPY", "EURJPY"], lambda p: bars[p], log, T0 + pd.Timedelta(hours=5))
    assert added == 2
    ev, cur = news.summarize_reactions(log)
    row = ev.iloc[0]
    assert row["currency"] == "USD" and row["move_4h"] > 0 and row["agree_1h"] == 2   # USD strengthened on both
    assert news.update_reactions(df, ["EURUSD", "USDJPY"], lambda p: bars[p], log, T0 + pd.Timedelta(hours=5)) == 0
    assert cur.iloc[0]["currency"] == "USD" and cur.iloc[0]["events"] == 1


def test_reaction_skips_events_whose_window_is_open(tmp_path):
    df = news.parse_events(RAW, T0)
    calls = []
    n = news.update_reactions(df, ["EURUSD"], lambda p: calls.append(p), tmp_path / "r.csv", T0 + pd.Timedelta(hours=2))
    assert n == 0 and calls == []
