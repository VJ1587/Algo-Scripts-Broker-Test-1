import json

import numpy as np
import pandas as pd

from empire.enrichment import relevant, research_context


def artifact(tmp_path, frequency="daily", constant=False):
    dates = pd.date_range("2025-01-01", periods=40, freq="ME" if frequency == "monthly" else "B")
    values = np.ones(40) if constant else np.cumsum(np.sin(np.arange(40))) + 100
    payload = {
        "schema_version": "1.0",
        "series": [
            {
                "id": "test:macro",
                "group": "us_macro",
                "unit": "index",
                "frequency": frequency,
                "retrieved_at": "2028-05-01T10:00:00Z",
                "history": [{"date": str(d.date()), "value": float(v)} for d, v in zip(dates, values, strict=True)],
            }
        ],
    }
    path = tmp_path / "research.json"
    path.write_text(json.dumps(payload))
    return path, dates, values


def test_relevance_excludes_unrelated_sources():
    assert relevant("oil", "WTI") and not relevant("oil", "EURUSD")
    assert relevant("eur_funding", "EURUSD") and not relevant("eur_funding", "USDJPY")
    assert relevant("crop_wheat", "wheat") and not relevant("crop_wheat", "XAUUSD")


def test_historical_decision_rejects_later_snapshot(tmp_path):
    path, dates, values = artifact(tmp_path)
    result = research_context(["EURUSD"], {}, "2025-02-01T20:00:00Z", path)
    assert result["instruments"]["EURUSD"][0]["status"] == "unavailable_at_decision"
    assert "value" not in result["instruments"]["EURUSD"][0]


def test_monthly_changes_not_forward_filled_daily(tmp_path):
    path, dates, values = artifact(tmp_path, "monthly")
    prices = pd.Series(np.exp(values / 100), index=dates)
    result = research_context(["EURUSD"], {"EURUSD": prices}, "2028-05-02T20:00:00Z", path)
    row = result["instruments"]["EURUSD"][0]
    assert row["n"] == 39 and row["correlation"] > 0.999


def test_constant_series_returns_null(tmp_path):
    path, dates, values = artifact(tmp_path, constant=True)
    payload=json.loads(path.read_text())
    payload['series'][0]['retrieved_at']='2025-02-26T10:00:00Z'
    path.write_text(json.dumps(payload))
    result = research_context(
        ["EURUSD"],
        {"EURUSD": pd.Series(np.exp(np.arange(40) / 100), index=dates)},
        "2025-02-26T20:00:00Z",
        path,
    )
    assert result["instruments"]["EURUSD"][0]["correlation"] is None
    assert result["instruments"]["EURUSD"][0]["n"] == 39


def test_funding_model_orientation_units_and_availability(tmp_path):
    rates = [("nyfed:EFFR", "usd_funding", 5.0), ("ecb:EST.B.EU000A2X2A25.WT", "eur_funding", 3.0)]
    series = [{"id": sid, "group": group, "unit": "percent", "frequency": "daily",
               "retrieved_at": "2026-10-14T10:00:00Z", "history": [{"date": "2026-10-13", "value": value}]}
              for sid, group, value in rates]
    path = tmp_path/"rates.json"
    path.write_text(json.dumps({"schema_version": "1.0", "series": series}))
    result = research_context(["EURUSD", "USDEUR", "USDJPY"], {}, "2026-10-14T20:00:00Z", path)
    assert result["models"]["EURUSD"]["funding_carry"]["annualized_reference_carry"] == -0.02
    assert result["models"]["USDEUR"]["funding_carry"]["differential_pct_points"] == 2.0
    assert result["models"]["USDJPY"]["funding_carry"] is None
    prior = research_context(["EURUSD"], {}, "2026-10-14T09:00:00Z", path)
    assert prior["models"]["EURUSD"]["available"] == 0
    stale = research_context(["EURUSD"], {}, "2026-11-14T20:00:00Z", path)
    assert stale["models"]["EURUSD"]["funding_carry"] is None


def test_funding_model_uses_common_rate_date(tmp_path):
    series = [{"id": sid, "group": group, "unit": "percent", "frequency": "daily",
               "retrieved_at": "2026-10-14T10:00:00Z", "history": rows}
              for sid, group, rows in [
                  ("nyfed:EFFR", "usd_funding", [{"date":"2026-10-12", "value":5}, {"date":"2026-10-13", "value":8}]),
                  ("ecb:EST.B.EU000A2X2A25.WT", "eur_funding", [{"date":"2026-10-12", "value":3}])]]
    path=tmp_path/"rates.json"; path.write_text(json.dumps({"schema_version":"1.0", "series":series}))
    carry=research_context(["EURUSD"], {}, "2026-10-14T20:00:00Z", path)["models"]["EURUSD"]["funding_carry"]
    assert carry["rate_date"] == "2026-10-12" and carry["differential_pct_points"] == -2
