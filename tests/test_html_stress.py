"""HTML report stress layer tests: the safe report passes, the naive one is caught."""
import json
import math
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codeguard.rules import analyze_source  # noqa: E402
from examples.demo_reports import NaiveJournalReport, SafeJournalReport  # noqa: E402
from stresslab import html_stress as hs  # noqa: E402

REQUIRED_NAIVE_FAILS = [
    ("hostile_text", "injection"), ("missing_values", "bad_tokens"), ("missing_values", "reconcile"),
    ("missing_values", "chart"), ("extreme_values", "reconcile"), ("single_trade", "reconcile"),
    ("unsorted_duplicates", "chart"), ("scale_50k", "table"), ("normal_200", "offline"),
]


@pytest.fixture(scope="module")
def safe_detail():
    return hs.HtmlReportStress(SafeJournalReport()).run()


@pytest.fixture(scope="module")
def naive_detail():
    return hs.HtmlReportStress(NaiveJournalReport()).run()


def _browser_or_skip():
    try:
        with hs.BrowserSession():
            pass
    except ImportError:
        pytest.skip("playwright not installed")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {e}")


# ------------------------------------------------------------- generators
def test_safe_generator_has_no_fail_on_any_dataset(safe_detail):
    assert set(safe_detail["dataset"]) == set(hs.datasets())
    bad = safe_detail[safe_detail["status"] == "FAIL"]
    assert bad.empty, bad.to_string()


def test_safe_generator_checks_everything(safe_detail):
    checks = set(safe_detail["check"])
    assert {"structure", "bad_tokens", "injection", "offline", "reconcile", "metric_coverage", "table", "chart",
            "timezone", "a11y", "determinism", "scale"} <= checks
    assert (safe_detail[safe_detail["check"] == "metric_coverage"]["status"] == "PASS").all()


@pytest.mark.parametrize("dataset, check", REQUIRED_NAIVE_FAILS)
def test_naive_generator_is_caught(naive_detail, dataset, check):
    row = naive_detail[(naive_detail["dataset"] == dataset) & (naive_detail["check"] == check)]
    assert len(row) == 1 and row["status"].iloc[0] == "FAIL", row.to_string()


def test_scale_budget_for_safe_report(safe_detail):
    r = safe_detail[(safe_detail["dataset"] == "scale_50k") & (safe_detail["check"] == "scale")].iloc[0]
    assert r["status"] == "PASS", r["detail"]


# ------------------------------------------------------------- oracle
def _trades(pnls, r=None):
    n = len(pnls)
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"), "symbol": "EURUSD",
                         "side": "long", "entry": 1.1, "exit": 1.1, "units": 1.0, "pnl": pnls,
                         "r_multiple": r if r is not None else [p / 100 for p in pnls], "strategy": "s", "notes": ""})


def test_oracle_definitions():
    e = hs.expected_metrics(_trades([-50.0]), 1000.0)
    assert e["max_drawdown"] == pytest.approx(5.0)          # peak includes starting equity
    assert e["profit_factor"] == 0.0
    assert hs.expected_metrics(_trades([10.0, 5.0]), 1000.0)["profit_factor"] == math.inf
    assert hs.expected_metrics(_trades([0.0, -0.0]), 1000.0)["profit_factor"] is None
    e = hs.expected_metrics(_trades([10.0, float("nan"), 0.0, -0.0]), 1000.0)
    assert (e["n_trades"], e["excluded_rows"]) == (3, 1)
    assert e["win_rate"] == pytest.approx(100 / 3)           # breakeven is not a win
    empty = hs.expected_metrics(_trades([]), 1000.0)
    assert empty["win_rate"] is None and empty["profit_factor"] is None and empty["n_trades"] == 0


# ------------------------------------------------------------- number parsing and reconcile
@pytest.mark.parametrize("text, value, decimals", [
    ("$1,234.50", 1234.5, 2), ("(1,234.50)", -1234.5, 2), ("−12.5%", -12.5, 1), ("£3", 3.0, 0),
    ("€0.25", 0.25, 2), ("1.5e12", 1.5e12, -11), ("1.2k", 1200.0, -2), ("3.4M", 3.4e6, -5),
    ("2bn", 2e9, -9), ("-$5.00", -5.0, 2), ("+0.125", 0.125, 3),
])
def test_parse_number(text, value, decimals):
    v, d = hs.parse_number(text)
    assert v == pytest.approx(value) and d == decimals


def test_parse_number_special_values():
    assert hs.parse_number("∞") == (math.inf, None)
    assert hs.parse_number("n/a") == (None, None)
    for bad in ("nan", "inf", "None", "12abc"):
        with pytest.raises(ValueError):
            hs.parse_number(bad)


def _page(metric, shown):
    return '<p><span data-metric="' + metric + '">' + shown + "</span></p>"


def test_reconcile_respects_display_rounding():
    exp = {"net_pnl": 1234.567, "excluded_rows": 0}
    assert hs.check_reconcile(_page("net_pnl", "$1,234.57"), exp)[0] == []
    assert hs.check_reconcile(_page("net_pnl", "$1,235"), exp)[0] == []        # 0 dp: half a unit is 0.5
    assert hs.check_reconcile(_page("net_pnl", "$1,234.5"), exp)[0] != []      # 1 dp: 0.067 off > 0.05
    assert hs.check_reconcile(_page("net_pnl", "$1.2k"), exp)[0] == []         # k suffix: tolerance 50
    assert hs.check_reconcile(_page("net_pnl", "nan"), exp)[0] != []


def test_untagged_metrics_are_coverage_not_failures_except_hidden_exclusions():
    probs, n_checked, untagged = hs.check_reconcile("<p>nothing tagged</p>", {"excluded_rows": 0})
    assert probs == [] and n_checked == 0 and "net_pnl" in untagged
    probs, _, _ = hs.check_reconcile("<p>nothing tagged</p>", {"excluded_rows": 3})
    assert probs and "does not disclose" in probs[0]


# ------------------------------------------------------------- injection, chart, table
def test_injection_detector_catches_json_block_breakout():
    page = ('<script type="application/json" data-chart="equity">[{"n": "</script>'
            '<script>window.XSSMARK=1</script>"}]</script>')
    probs = hs.check_injection(page)
    assert any("executable" in p for p in probs)
    assert any("does not parse" in p for p in probs)


def test_escaped_json_is_inert_and_round_trips():
    obj = [{"note": "</script><script>window.XSSMARK=1</script> & <b>x</b>"}]
    s = hs.escape_json_for_script(obj)
    assert "<" not in s and ">" not in s and "&" not in s
    assert json.loads(s) == obj
    page = '<script type="application/json" data-chart="x">' + s + "</script>"
    assert hs.check_injection(page) == []
    with pytest.raises(ValueError):
        hs.escape_json_for_script([float("nan")])


def test_chart_rejects_nan_that_python_json_accepts():
    page = '<script type="application/json" data-chart="equity">[{"t": "2024-01-01T00:00:00Z", "equity": NaN}]</script>'
    status, msg = hs.check_chart(page, {"n_trades": 1, "final_equity": 1.0})
    assert status == "FAIL" and "NaN" in msg


def test_table_with_td_header_row_is_counted_correctly():
    rows = "".join("<tr><td>2024-01-0" + str(i) + "</td><td>" + str(i) + "</td></tr>" for i in range(1, 4))
    page = '<table data-table="trades"><tr><td>Time</td><td>P&L</td></tr>' + rows + "</table>"
    assert hs.check_table(page, 3)[0] == "PASS"
    assert hs.check_table(page, 4)[0] == "FAIL"
    assert any("<th>" in p for p in hs.check_a11y(page))


def test_bad_tokens_ignore_scripts_and_find_visible_ones():
    assert hs.check_bad_tokens("<p>ok</p><script>var x = null; var y = NaN;</script>") == []
    assert hs.check_bad_tokens("<td>nan</td>") != []
    assert hs.check_bad_tokens("<td>[object Object]</td>") != []
    assert hs.check_bad_tokens("<td>financial info</td>") == []


def test_structure_flags_unclosed_tags_and_duplicate_ids():
    st = dict((m, s) for s, m in hs.check_structure('<div id="a"><div id="a"><span>x</div>'))
    msgs = " ".join(st)
    assert "duplicate ids" in msgs and "span" in msgs
    clean = hs.check_structure('<html lang="en"><head><meta charset="utf-8"><title>t</title></head>'
                               "<body><p>one<p>two<ul><li>a<li>b</ul><img src='data:,'></body></html>")
    assert all(s != "FAIL" for s, _ in clean)


# ------------------------------------------------------------- files not generated in Python
def test_audit_html_file_detects_utf16_and_lists_tables(tmp_path):
    p = tmp_path / "mt5.htm"
    p.write_bytes(("<html lang='en'><head><meta charset='utf-16'><meta name='viewport' content='x'>"
                   "<title>r</title></head><body>All times UTC<table><tr><th>a</th></tr><tr><td>1</td></tr>"
                   "</table></body></html>").encode("utf-16"))
    res = hs.audit_html_file(p)
    assert res["encoding"].startswith("utf-16")
    assert res["tables"][0]["rows"] >= 1
    assert res["status"] == "WARN"


def test_audit_html_file_fails_naive_page(tmp_path):
    trades, meta = hs.datasets()["missing_values"]
    p = tmp_path / "naive.html"
    p.write_text(NaiveJournalReport().render(trades, meta), encoding="utf-8")
    assert hs.audit_html_file(p)["status"] == "FAIL"


# ------------------------------------------------------------- codeguard TG405
def test_codeguard_tg405_flags_naive_report_only():
    src = (ROOT / "examples" / "demo_reports.py").read_text(encoding="utf-8")
    naive_line = next(i for i, ln in enumerate(src.splitlines(), 1) if ln.startswith("class NaiveJournalReport"))
    tg405 = [f.line for f in analyze_source(src, "examples/demo_reports.py") if f.rule == "TG405"]
    assert tg405, "TG405 should flag the naive report"
    assert all(line > naive_line for line in tg405), "TG405 must not flag the safe report"


# ------------------------------------------------------------- browser
def test_browser_safe_report_is_inert_and_offline():
    _browser_or_skip()
    d = hs.HtmlReportStress(SafeJournalReport(), browser=True, only=["hostile_text", "unicode_long_text"]).run()
    b = d[d["check"].str.startswith("browser")]
    assert len(b) == 8 and (b["status"] == "PASS").all(), b.to_string()


def test_browser_catches_naive_report():
    _browser_or_skip()
    d = hs.HtmlReportStress(NaiveJournalReport(), browser=True, only=["hostile_text"]).run()
    b = {r.check: (r.status, r.detail) for r in d.itertuples() if r.check.startswith("browser")}
    assert b["browser:xss"] == ("FAIL", "INJECTED SCRIPT EXECUTED (window.XSSMARK set)")
    assert b["browser:js_errors"][0] == "FAIL" and "Chart is not defined" in b["browser:js_errors"][1]
    assert b["browser:network"][0] == "FAIL" and "cdn.jsdelivr.net" in b["browser:network"][1]
