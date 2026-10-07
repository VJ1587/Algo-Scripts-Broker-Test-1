"""Codeguard tests: every rule fires on a planted sample, clean code stays clean."""
import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codeguard import debugrun, engine  # noqa: E402
from codeguard.rules import RULES, analyze_source, fingerprint  # noqa: E402

SAMPLES = ROOT / "codeguard" / "samples"


def rules_in(code, path="snippet.py"):
    return {f.rule for f in analyze_source(textwrap.dedent(code), path)}


def _scan_dir(d, **kw):
    kw.setdefault("use_ruff", False)
    return engine.scan([d], root=d, cfg=engine.Config(), **kw)


# ------------------------------------------------------------- samples
def test_every_rule_is_triggered_by_some_sample():
    res = engine.scan([SAMPLES], root=ROOT, use_cache=False, use_ruff=False)
    missing = set(RULES) - {f.rule for f in res.findings}
    assert not missing, f"rules never triggered by samples: {sorted(missing)}"


def test_clean_sample_has_no_findings_including_ruff():
    res = engine.scan([SAMPLES / "clean_scanner.py"], root=ROOT, use_cache=False, use_ruff=True)
    assert res.findings == [], [(f.rule, f.line, f.message) for f in res.findings]


def test_sample_findings_per_file():
    res = engine.scan([SAMPLES], root=ROOT, use_cache=False, use_ruff=False)
    by = {}
    for f in res.findings:
        by.setdefault(Path(f.path).name, set()).add(f.rule)
    assert {"TG402", "TG404", "TG405"} <= by["bad_journal.py"]
    assert by["bad_syntax.py"] == {"TG000"}
    expected = {f"TG{n}" for n in (101, 102, 103, 104, 105, 201, 202, 203, 301, 302, 303, 304, 305, 306, 307, 308,
                                   401, 403)}
    assert expected <= by["bad_mt5_scanner.py"]


# ------------------------------------------------------------- lookahead patterns
@pytest.mark.parametrize("code, rule", [
    ("y = s.shift(-1)", "TG101"),
    ("y = s.shift(periods=-3)", "TG101"),
    ("y = df['close'].shift(-n)", "TG101"),
    ("y = s.rolling(20, center=True).mean()", "TG102"),
    ("y = s.bfill()", "TG103"),
    ("y = s.backfill()", "TG103"),
    ("y = s.fillna(method='bfill')", "TG103"),
    ("z = (x - x.mean()) / x.std()", "TG104"),
    ("r = x.rank(pct=True)", "TG105"),
    ("import numpy as np\nv = np.random.rand(3)", "TG201"),
    ("import numpy as np\nrng = np.random.default_rng()", "TG201"),
    ("import random\nv = random.choice([1, 2])", "TG201"),
    ("from datetime import datetime\nt = datetime.now()", "TG202"),
    ("import datetime\nt = datetime.datetime.utcnow()", "TG202"),
    ("import pandas as pd\nt = pd.Timestamp.now()", "TG202"),
    ("ok = price == 1.105", "TG203"),
])
def test_bad_patterns_are_caught(code, rule):
    assert rule in rules_in(code)


@pytest.mark.parametrize("code", [
    "y = s.shift(1)",
    "y = s.rolling(20).mean()",
    "y = s.ffill()",
    "r = s.rolling(50)\nz = (s - r.mean()) / r.std()",
    "z = (x - x.expanding(20).mean()) / x.expanding(20).std()",
    "q = x.rolling(250).rank(pct=True)",
    "class J:\n    def add(self, t):\n        self.trades.append(t)",
    "trades = []\ntrades.append(1)",
    "import numpy as np\nrng = np.random.default_rng(42)\nv = rng.normal()",
    "from datetime import datetime, timezone\nt = datetime.now(timezone.utc)",
    "import pandas as pd\nt = pd.Timestamp.now(tz='UTC')",
    "ok = qty == 0.0",
    "meta = {}\nmeta['freshness']['MT5'] = 'ok'",
])
def test_clean_patterns_are_not_flagged(code):
    assert rules_in(code) == set()


# ------------------------------------------------------------- suppressions
def test_suppression_requires_a_reason():
    assert "TG101" not in rules_in("y = s.shift(-1)  # guard: ignore[TG101] forward label for research only")
    assert "TG101" in rules_in("y = s.shift(-1)  # guard: ignore[TG101]")
    assert "TG101" in rules_in("y = s.shift(-1)  # guard: ignore[TG101] ok")
    assert "TG101" in rules_in("y = s.shift(-1)  # guard: ignore[TG102] wrong rule named here")


# ------------------------------------------------------------- broker safety
def test_order_dict_requires_sl():
    no_sl = "req = {'action': 1, 'symbol': 'EURUSD', 'volume': 0.1, 'type': 0, 'price': 1.1}"
    with_sl = "req = {'action': 1, 'symbol': 'EURUSD', 'volume': 0.1, 'type': 0, 'price': 1.1, 'sl': 1.09}"
    assert "TG305" in rules_in(no_sl)
    assert "TG305" not in rules_in(with_sl)
    assert "TG305" in rules_in("req = dict(action=1, symbol='X', volume=1, type=0)")


def test_mt5_alias_tracking_and_checked_results():
    assert "TG304" in rules_in("import MetaTrader5 as broker\ndef f():\n    broker.order_send(r)")
    assert "TG304" not in rules_in("import MetaTrader5 as mt5\ndef f():\n    ok = mt5.order_send(r)")


def test_credentials():
    assert "TG306" in rules_in("API_KEY = 'abcd1234'")
    assert "TG306" in rules_in("connect(password='hunter2')")
    assert "TG306" not in rules_in("import os\nAPI_KEY = os.environ['API_KEY']")
    assert "TG306" not in rules_in("API_KEY_ENV = 'FINNHUB_API_KEY'")  # names an env var, not a secret


def test_side_effect_inside_main_guard_is_fine():
    code = """
    import subprocess
    import MetaTrader5 as mt5
    def main():
        subprocess.run(['ls'])
    if __name__ == "__main__":
        ok = mt5.initialize()
        subprocess.run(['ls'])
        main()
    """
    assert "TG307" not in rules_in(code)


def test_module_level_side_effect_in_assign_is_flagged():
    assert "TG307" in rules_in("import subprocess\nrc = subprocess.run(['ls']).returncode\n")
    assert "TG307" in rules_in("import pandas as pd\ndf = pd.DataFrame()\ndf.to_csv('x.csv')\n")
    assert "TG307" not in rules_in("import subprocess\nrun = lambda: subprocess.run(['ls'])\n")


def test_assert_allowed_in_tests_only():
    assert "TG308" in rules_in("assert x > 0", path="pkg/mod.py")
    assert "TG308" not in rules_in("assert x > 0", path="tests/test_mod.py")


def test_append_assigned_back_and_concat_in_loop():
    assert "TG402" in rules_in("df = df.append(row)")
    assert "TG402" in rules_in("self.df = self.df.append(row)")
    assert "TG401" in rules_in("import pandas as pd\nfor x in xs:\n    out = pd.concat([out, x])")
    assert "TG401" not in rules_in("import pandas as pd\nout = pd.concat([a, b])")


def test_html_fstring_escaping():
    assert "TG405" in rules_in("h = f\"<td>{r['notes']}</td>\"")
    assert "TG405" in rules_in("h = f'<title>{meta.title}</title>'")
    assert "TG405" not in rules_in("import html\nh = f\"<td>{html.escape(r['notes'])}</td>\"")
    assert "TG405" not in rules_in("h = f\"<td>{r['pnl']:.2f}</td>\"")
    assert "TG405" not in rules_in("h = f\"value {r['notes']}\"")


def test_syntax_error_is_tg000_and_bom_is_fine(tmp_path):
    assert rules_in("def f(:\n  pass") == {"TG000"}
    (tmp_path / "bom.py").write_text("\ufeffx = 1\n", encoding="utf-8")
    assert _scan_dir(tmp_path, use_cache=False).findings == []


# ------------------------------------------------------------- baseline ratchet
def test_baseline_ratchet_moved_line_known_duplicate_new(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("y = s.shift(-1)\n")
    base_path = tmp_path / "base.json"
    engine.write_baseline(base_path, _scan_dir(tmp_path, use_cache=False).findings)
    base = engine.load_baseline(base_path)
    f.write_text("\n\nimport os\n\ny = s.shift(-1)\n")  # same bug, moved down
    new, known, fixed = engine.split_new(_scan_dir(tmp_path, use_cache=False).findings, base)
    assert (len(new), len(known), fixed) == (0, 1, 0)
    f.write_text("y = s.shift(-1)\nz = s.shift(-1)\n")  # a second copy is NEW
    new, known, fixed = engine.split_new(_scan_dir(tmp_path, use_cache=False).findings, base)
    assert (len(new), len(known)) == (1, 1)
    f.write_text("y = s.shift(1)\n")  # fixed
    new, known, fixed = engine.split_new(_scan_dir(tmp_path, use_cache=False).findings, base)
    assert (len(new), len(known), fixed) == (0, 0, 1)


def test_fingerprint_ignores_line_and_whitespace():
    a = analyze_source("y = s.shift(-1)\n", "m.py")[0]
    b = analyze_source("\n\ny  =  s.shift(-1)\n", "m.py")[0]
    assert a.line != b.line and fingerprint(a) == fingerprint(b)


# ------------------------------------------------------------- cache and config
def test_cache_reuses_and_invalidates(tmp_path):
    for i in range(30):
        (tmp_path / f"m{i:02d}.py").write_text(f"x{i} = s.shift(-{i + 1})\n")
    r1 = _scan_dir(tmp_path)
    assert (r1.stats["analyzed"], r1.stats["cached"]) == (30, 0)
    r2 = _scan_dir(tmp_path)
    assert (r2.stats["analyzed"], r2.stats["cached"]) == (0, 30)
    assert len(r2.findings) == len(r1.findings) == 30
    (tmp_path / "m05.py").write_text("x5 = s.shift(1)\n")
    r3 = _scan_dir(tmp_path)
    assert (r3.stats["analyzed"], r3.stats["cached"]) == (1, 29)
    assert len(r3.findings) == 29
    (tmp_path / "m06.py").unlink()
    _scan_dir(tmp_path)
    cache = json.loads((tmp_path / engine.CACHE_FILE).read_text())
    assert "m06.py" not in cache["files"]


def test_parallel_matches_serial(tmp_path):
    for i in range(25):
        (tmp_path / f"p{i}.py").write_text("import numpy as np\nv = np.random.rand(2)\nw = s.bfill()\n")
    par = _scan_dir(tmp_path, use_cache=False, jobs=4)
    ser = _scan_dir(tmp_path, use_cache=False, jobs=1)
    assert [f.to_dict() for f in par.findings] == [f.to_dict() for f in ser.findings]


def test_config_exclude_disable_severity_per_path(tmp_path):
    (tmp_path / "a.py").write_text("y = s.shift(-1)\nv = s.bfill()\n")
    (tmp_path / "skip").mkdir()
    (tmp_path / "skip" / "b.py").write_text("y = s.shift(-1)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_c.py").write_text("ok = x == 1.5\n")
    (tmp_path / "codeguard.toml").write_text(
        '[codeguard]\nexclude = ["skip"]\ndisable = ["TG103"]\n'
        '[codeguard.severity]\nTG101 = "WARN"\n'
        '[codeguard.per_path_disable]\n"tests/**" = ["TG203"]\n')
    res = engine.scan([tmp_path], root=tmp_path, use_cache=False, use_ruff=False)
    assert [(f.path, f.rule, f.severity) for f in res.findings] == [("a.py", "TG101", "WARN")]
    explicit = engine.scan([tmp_path / "skip"], root=tmp_path, use_cache=False, use_ruff=False)
    assert [f.path for f in explicit.findings] == ["skip/b.py"]  # named explicitly, so scanned


def test_ruff_findings_are_merged_and_suppressible(tmp_path):
    if engine._ruff_cmd() is None:
        pytest.skip("ruff not installed")
    (tmp_path / "r.py").write_text("import os\nimport sys  # guard: ignore[F401] kept for a plugin hook\n")
    res = _scan_dir(tmp_path, use_cache=False, use_ruff=True)
    assert [(f.rule, f.line, f.source) for f in res.findings] == [("F401", 1, "ruff")]


# ------------------------------------------------------------- outputs
def test_sarif_shape():
    res = engine.scan([SAMPLES], root=ROOT, use_cache=False, use_ruff=False)
    s = engine.to_sarif(res)
    assert s["version"] == "2.1.0" and "$schema" in s
    run = s["runs"][0]
    assert run["tool"]["driver"]["name"] == "codeguard"
    assert {r["id"] for r in run["tool"]["driver"]["rules"]} >= set(RULES)
    assert len(run["results"]) == len(res.findings)
    r0 = run["results"][0]
    assert r0["level"] in ("error", "warning", "note")
    loc = r0["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"].endswith(".py") and loc["region"]["startLine"] >= 1
    json.dumps(s)


def test_markdown_lists_rules_and_fixes():
    res = engine.scan([SAMPLES / "bad_journal.py"], root=ROOT, use_cache=False, use_ruff=False)
    md = engine.to_markdown(res, res.findings)
    assert "## Summary by rule" in md and "TG402" in md and "Fix:" in md and "```python" in md


# ------------------------------------------------------------- smoke and debug runner
def test_smoke_skips_side_effect_modules_and_reports_failures(tmp_path):
    from codeguard.smoke import smoke
    (tmp_path / "good.py").write_text("X = 1\n")
    (tmp_path / "bad.py").write_text("import not_a_real_module_xyz\n")
    (tmp_path / "danger.py").write_text("import subprocess\nsubprocess.run(['false'])\n")
    (tmp_path / "my-script.py").write_text("Y = 2\n")
    res = _scan_dir(tmp_path, use_cache=False)
    out = {r.path: r.status for r in smoke(res.files, root=tmp_path, findings=res.findings, timeout=30)}
    assert out == {"bad.py": "fail", "danger.py": "skipped", "good.py": "ok", "my-script.py": "ok"}


def test_debug_runner_reports_empty_dataframe(tmp_path):
    rep = debugrun.run(SAMPLES / "bad_journal.py", [], report_path=tmp_path / "r.json", echo=False)
    assert rep["status"] == "crashed" and rep["exception"] == "IndexError"
    frame = [fr for fr in rep["frames"] if fr["function"] == "best_trade"][0]
    assert frame["locals"]["df"]["shape"] == [0, 3]
    assert any("EMPTY DataFrame" in h for h in rep["hints"])
    assert "EMPTY DataFrame" in debugrun.format_report(rep)
    assert json.loads((tmp_path / "r.json").read_text())["exception"] == "IndexError"


def test_debug_runner_restores_interpreter_state(tmp_path):
    import warnings
    script = tmp_path / "w.py"
    script.write_text("import sys, warnings\nfor _ in range(2):\n    warnings.warn('careful')\nsys.exit(0)\n")
    argv, path, show = list(sys.argv), list(sys.path), warnings.showwarning
    rep = debugrun.run(script, ["--x"], echo=False)
    assert rep["status"] == "ok" and rep["warnings"][0]["count"] == 2
    assert sys.argv == argv and sys.path == path and warnings.showwarning is show
