"""FredDaily reads the shared FRED cache when it holds the series, and never touches the network then."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from empire.data import FredDaily  # noqa: E402


def test_reads_shared_cache_and_applies_start(tmp_path, monkeypatch):
    (tmp_path / "DGS10.csv").write_text("date,value\n2026-10-06,4.10\n2026-10-07,4.11\n2026-10-08,4.12\n")
    monkeypatch.setitem(sys.modules, "requests", None)          # any download attempt would raise
    df, spread = FredDaily("2026-10-07", cache_dir=str(tmp_path)).fetch({"fred": "DGS10"}, 0)
    assert list(df["close"]) == [4.11, 4.12] and spread is None


def test_uses_env_var_for_the_cache_folder(tmp_path, monkeypatch):
    (tmp_path / "VIXCLS.csv").write_text("date,value\n2026-10-08,16.5\n")
    monkeypatch.setenv("FRED_CACHE_DIR", str(tmp_path))
    src = FredDaily("2026-01-01")
    assert src.fetch({"fred": "VIXCLS"}, 0)[0]["close"].iloc[-1] == 16.5
    assert src.last_origin.startswith("shared cache")


def test_invert_still_applies_to_cached_values(tmp_path):
    (tmp_path / "DEXJPUS.csv").write_text("date,value\n2026-10-08,150.0\n")
    df, _ = FredDaily("2026-01-01", cache_dir=str(tmp_path)).fetch({"fred": "DEXJPUS", "invert": True}, 0)
    assert df["close"].iloc[0] == pytest.approx(1 / 150.0)
