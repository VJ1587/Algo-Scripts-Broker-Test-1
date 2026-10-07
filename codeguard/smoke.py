"""Import smoke test: import every module in a fresh interpreter, in parallel, with a timeout.

Modules are imported by DOTTED NAME from the repo root so relative imports and
dataclasses behave as in real use. Files whose name is not a valid identifier
fall back to loading by path (registered in ``sys.modules`` first).

Files with a TG307 finding (side effect at import time) are SKIPPED, so a
smoke test can never connect to a broker, download data or place an order.
Files that do not parse (TG000) are skipped too; the scan already blocks them.
"""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_BY_NAME = ("import importlib, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "importlib.import_module(sys.argv[2])\n")
_BY_PATH = ("import importlib.util, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "spec = importlib.util.spec_from_file_location(sys.argv[2], sys.argv[3])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "sys.modules[sys.argv[2]] = mod\n"
            "spec.loader.exec_module(mod)\n")


@dataclass
class SmokeResult:
    path: str
    module: str
    status: str  # ok | fail | timeout | skipped
    detail: str = ""


def module_name(rel: str) -> tuple[str, bool]:
    """(dotted name, importable_by_name) for a repo relative .py path."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    ok = bool(parts) and all(p.isidentifier() for p in parts)
    name = ".".join(parts) if ok else "_smoke_" + "".join(c if c.isalnum() else "_" for c in rel[:-3])
    return name, ok


def _import_one(root: Path, rel: str, timeout: float) -> SmokeResult:
    name, by_name = module_name(rel)
    if by_name:
        cmd = [sys.executable, "-c", _BY_NAME, str(root), name]
    else:
        cmd = [sys.executable, "-c", _BY_PATH, str(root), name, str(root / rel)]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "CODEGUARD_SMOKE": "1"}
    try:
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except subprocess.TimeoutExpired:
        return SmokeResult(rel, name, "timeout", f"import took longer than {timeout:.0f}s")
    if r.returncode == 0:
        return SmokeResult(rel, name, "ok")
    tail = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
    return SmokeResult(rel, name, "fail", tail[-1] if tail else f"exit code {r.returncode}")


def smoke(files: list[str], root=".", findings=(), timeout: float = 60.0, jobs: int | None = None) -> list[SmokeResult]:
    root = Path(root).resolve()
    skip: dict[str, str] = {}
    for f in findings:
        if f.rule == "TG307" and f.path not in skip:
            skip[f.path] = f"skipped: import time side effect at line {f.line} (TG307)"
        elif f.rule == "TG000":
            skip[f.path] = "skipped: does not parse (TG000)"
    out = [SmokeResult(r, module_name(r)[0], "skipped", skip[r]) for r in files if r in skip]
    todo = [r for r in files if r not in skip]
    with ThreadPoolExecutor(max_workers=jobs or min(16, (os.cpu_count() or 2) * 2)) as ex:
        out += list(ex.map(lambda r: _import_one(root, r, timeout), todo))
    return sorted(out, key=lambda s: s.path)
