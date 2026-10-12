"""Validate and package this independent scaling strategy plus its MT5 reader."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    source = ROOT / "Gold_ES_Scaling.mq5"
    binary = ROOT / "Gold_ES_Scaling.ex5"
    log = ROOT / "validation" / "Gold_ES_Scaling.log"
    raw = log.read_bytes()
    text = (
        raw.decode("utf-16")
        if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
        else raw.decode("utf-8-sig")
    )
    assert re.search(r"Result: 0 errors, 0 warnings", text)
    assert binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime
    code = source.read_text()
    assert "OrderSend(" not in code and "#include" not in code
    assert "InpAllowDiagnosticData=false" in code and "InpRepeatAlerts=false" in code
    smoke = json.loads((ROOT / "validation" / "cli_smoke.json").read_text())
    assert len(smoke) == 2 and all(item["returncode"] == 0 for item in smoke)
    pine = ROOT / "source" / "gold_es_scaling_v2_8_0.pine"
    summary = {
        "strategy_id": "gold_es_scaling",
        "source_version": "2.8.0",
        "implementation": "Python engine plus independent read-only MT5 bridge",
        "python_tests": "22 scaling checks; 33 including existing futures profile checks",
        "cli_smoke": smoke,
        "compile_errors": 0,
        "compile_warnings": 0,
        "tradingview_runtime_parity": False,
        "mt5_terminal_runtime_tested": False,
        "orders_placed": False,
        "source_pine_sha256": hashlib.sha256(pine.read_bytes()).hexdigest(),
        "mq5_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "ex5_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    (ROOT / "validation" / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    bundle = ROOT / "Gold_ES_Scaling_Python_MT5.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in (source, binary):
            archive.write(path, "Scripts/" + path.name)
        for name in ("README.md", "CHANGELOG.md"):
            archive.write(ROOT / name, name)
        for folder in ("source", "config", "tests"):
            for path in sorted((ROOT / folder).rglob("*")):
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix in (".py", ".json", ".pine")
                ):
                    archive.write(
                        path,
                        "Python/gold_es_scaling/" + path.relative_to(ROOT).as_posix(),
                    )
        for path in sorted(ROOT.glob("*.py")):
            archive.write(path, "Python/gold_es_scaling/" + path.name)
        for name in ("README.md", "CHANGELOG.md", "requirements-lock.txt"):
            archive.write(ROOT / name, "Python/gold_es_scaling/" + name)
        for path in (source, binary):
            archive.write(path, "Python/gold_es_scaling/" + path.name)
        for name in ("summary.json", "cli_smoke.json", "Gold_ES_Scaling.log"):
            archive.write(ROOT / "validation" / name, "validation/" + name)
            archive.write(
                ROOT / "validation" / name, "Python/gold_es_scaling/validation/" + name
            )
    print("Validated independent Gold / ES scaling Python+MT5 delivery:", bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
