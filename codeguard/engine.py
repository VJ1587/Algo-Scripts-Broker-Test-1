"""Codeguard engine: discovery, config, cache, parallel analysis, ruff, baseline, reports."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from codeguard.rules import (RULES, RULESET_VERSION, SEVERITY_RANK, Finding, analyze_source, apply_suppressions,
                             fingerprint)

DEFAULT_EXCLUDE_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env", ".env", "__pycache__", ".mypy_cache",
                        ".pytest_cache", ".ruff_cache", ".tox", ".nox", ".eggs", "build", "dist", "node_modules",
                        "site-packages", "dist-packages"}
CACHE_FILE = ".codeguard_cache.json"
BASELINE_FILE = ".codeguard_baseline.json"
PARALLEL_THRESHOLD = 20
RUFF_BATCH = 400
RUFF_SELECT = "F,E9,B,PD,NPY,S,PERF,PLE,ASYNC"
RUFF_IGNORE = "B006,S101,S105,S106,S110,S112,S603,S607,S301,S307,S311,NPY002,BLE001,DTZ,PD901,PD011"
RUFF_BLOCK_PREFIXES = ("F821", "F822", "F823", "E9", "PLE", "F632", "B015", "B018")


# --------------------------------------------------------------------- config
@dataclass
class Config:
    exclude: list[str] = field(default_factory=list)
    disable: set[str] = field(default_factory=set)
    severity: dict[str, str] = field(default_factory=dict)
    per_path_disable: dict[str, list[str]] = field(default_factory=dict)


def load_config(root: Path) -> Config:
    p = Path(root) / "codeguard.toml"
    if not p.exists():
        return Config()
    data = tomllib.loads(p.read_text(encoding="utf-8")).get("codeguard", {})
    sev = {k.upper(): str(v).upper() for k, v in data.get("severity", {}).items()}
    for k, v in sev.items():
        if v not in SEVERITY_RANK:
            raise ValueError(f"codeguard.toml: severity for {k} must be BLOCK, WARN or INFO, got {v}")
    return Config(exclude=list(data.get("exclude", [])), disable={r.upper() for r in data.get("disable", [])},
                  severity=sev,
                  per_path_disable={g: [r.upper() for r in rules] for g, rules in data.get("per_path_disable", {}).items()})


def _glob_match(rel: str, pattern: str) -> bool:
    pat = pattern.strip().replace("\\", "/")
    prefix = pat
    for suf in ("/**", "/*", "/"):
        if prefix.endswith(suf):
            prefix = prefix[: -len(suf)]
    return rel == prefix or rel.startswith(prefix + "/") or fnmatch.fnmatch(rel, pat)


def apply_config(findings: list[Finding], cfg: Config) -> list[Finding]:
    out = []
    for f in findings:
        if f.rule in cfg.disable:
            continue
        if any(f.rule in rules and _glob_match(f.path, g) for g, rules in cfg.per_path_disable.items()):
            continue
        if f.rule in cfg.severity:
            f = Finding(**{**f.to_dict(), "severity": cfg.severity[f.rule]})
        out.append(f)
    return out


# --------------------------------------------------------------------- discovery
def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def discover(paths, root: Path, cfg: Config) -> list[Path]:
    """Python files under ``paths``. Config excludes do not apply to a path you named explicitly."""
    root = Path(root)
    out: dict[str, Path] = {}
    for p in paths:
        p = Path(p)
        if not p.is_absolute():
            p = (Path.cwd() / p)
        if p.is_file():
            if p.suffix == ".py":
                out[_rel(p, root)] = p
            continue
        target_rel = _rel(p, root)
        active = [e for e in cfg.exclude if not (target_rel != "." and _glob_match(target_rel, e))]
        for dirpath, dirnames, filenames in os.walk(p):
            d = Path(dirpath)
            keep = []
            for dn in dirnames:
                rel = _rel(d / dn, root)
                if dn in DEFAULT_EXCLUDE_DIRS or dn.endswith(".egg-info"):
                    continue
                if any(_glob_match(rel, e) for e in active):
                    continue
                keep.append(dn)
            dirnames[:] = sorted(keep)
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    fp = d / fn
                    rel = _rel(fp, root)
                    if not any(_glob_match(rel, e) for e in active):
                        out[rel] = fp
    return [out[k] for k in sorted(out)]


def changed_files(root: Path, ref: str = "HEAD") -> set[str]:
    """``git diff --name-only REF`` plus untracked files, as repo relative paths."""
    def git(*args):
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
        if r.returncode:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
        return [x for x in r.stdout.splitlines() if x.strip()]
    top = Path(git("rev-parse", "--show-toplevel")[0])
    names = git("diff", "--name-only", ref) + git("ls-files", "--others", "--exclude-standard")
    return {_rel(top / n, root) for n in names}


# --------------------------------------------------------------------- analysis
def _key(text: str) -> str:
    return hashlib.sha256((RULESET_VERSION + "\0" + text).encode("utf-8", "surrogatepass")).hexdigest()


def _analyze_one(args):
    rel, text = args
    return rel, [f.to_dict() for f in analyze_source(text, rel)]


def _ruff_cmd():
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    try:
        r = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, check=False)
        if r.returncode == 0:
            return [sys.executable, "-m", "ruff"]
    except OSError:
        pass
    return None


def run_ruff(files: list[tuple[str, Path]], root: Path) -> tuple[dict[str, list[dict]], str]:
    """One batched ruff call per RUFF_BATCH files. Returns ({rel: [finding dicts]}, status)."""
    cmd = _ruff_cmd()
    if cmd is None:
        return {}, "ruff not installed; continued without it"
    out: dict[str, list[dict]] = {rel: [] for rel, _ in files}
    by_abs = {str(p.resolve()): rel for rel, p in files}
    for i in range(0, len(files), RUFF_BATCH):
        batch = files[i:i + RUFF_BATCH]
        r = subprocess.run([*cmd, "check", "--isolated", "--output-format", "json", "--exit-zero",
                            "--select", RUFF_SELECT, "--ignore", RUFF_IGNORE,
                            *[str(p) for _, p in batch]], cwd=root, capture_output=True, text=True, check=False)
        try:
            items = json.loads(r.stdout or "[]")
        except json.JSONDecodeError:
            return {}, f"ruff output unreadable; skipped ({r.stderr.strip()[:120]})"
        for it in items:
            code = it.get("code")
            if not code or code == "invalid-syntax":
                continue
            rel = by_abs.get(str(Path(it.get("filename", "")).resolve()))
            if rel is None:
                continue
            loc = it.get("location") or {}
            sev = "BLOCK" if code.startswith(RUFF_BLOCK_PREFIXES) else "WARN"
            fix = (it.get("fix") or {}).get("message") or f"see: ruff rule {code}"
            out[rel].append(Finding(code, sev, rel, int(loc.get("row", 1)), int(loc.get("column", 1)),
                                    it.get("message", ""), fix, "", "ruff").to_dict())
    return out, f"ruff ran on {len(files)} file(s)"


@dataclass
class ScanResult:
    findings: list[Finding]
    files: list[str]
    stats: dict


def scan(paths=(".",), root=".", jobs: int | None = None, use_cache: bool = True, use_ruff: bool = True,
         changed_ref: str | None = None, cfg: Config | None = None) -> ScanResult:
    t0 = time.perf_counter()
    root = Path(root).resolve()
    cfg = cfg or load_config(root)
    files = discover(paths, root, cfg)
    rel_files = [(_rel(p, root), p) for p in files]
    if changed_ref is not None:
        changed = changed_files(root, changed_ref)
        rel_files = [(r, p) for r, p in rel_files if r in changed and p.exists()]

    cache_path = root / CACHE_FILE
    cache = {"ruleset": RULESET_VERSION, "files": {}}
    if use_cache and cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if loaded.get("ruleset") == RULESET_VERSION:
                cache = loaded
        except (json.JSONDecodeError, OSError):
            pass
    entries = cache["files"]

    texts, todo, cached, need_ruff = {}, [], 0, []
    for rel, p in rel_files:
        text = p.read_text(encoding="utf-8-sig", errors="surrogateescape")
        texts[rel] = text
        k = _key(text)
        e = entries.get(rel)
        if e and e.get("key") == k:
            cached += 1
            if use_ruff and e.get("ruff") is None:
                need_ruff.append((rel, p))
        else:
            entries[rel] = {"key": k, "findings": None, "ruff": None}
            todo.append((rel, text))
            if use_ruff:
                need_ruff.append((rel, p))

    n_jobs = jobs or os.cpu_count() or 1
    if len(todo) > PARALLEL_THRESHOLD and n_jobs > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            results = list(ex.map(_analyze_one, todo, chunksize=max(1, len(todo) // (n_jobs * 4))))
    else:
        results = [_analyze_one(t) for t in todo]
    for rel, fl in results:
        entries[rel]["findings"] = fl

    ruff_status = "ruff disabled (--no-ruff)"
    if use_ruff:
        if need_ruff:
            ruff_map, ruff_status = run_ruff(need_ruff, root)
            if ruff_map:
                for rel, fl in ruff_map.items():
                    lines = texts[rel].splitlines()
                    fs = [Finding.from_dict(d) for d in fl]
                    for f in fs:
                        f.snippet = lines[f.line - 1].rstrip() if 0 < f.line <= len(lines) else ""
                    entries[rel]["ruff"] = [f.to_dict() for f in apply_suppressions(fs, lines)]
        else:
            ruff_status = "ruff results reused from cache"

    if use_cache:
        if changed_ref is None:
            for rel in [r for r in entries if not (root / r).exists()]:
                del entries[rel]
        try:
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass

    findings = []
    for rel, _ in rel_files:
        e = entries[rel]
        findings += [Finding.from_dict(d) for d in (e["findings"] or [])]
        if use_ruff:
            findings += [Finding.from_dict(d) for d in (e.get("ruff") or [])]
    findings = apply_config(findings, cfg)
    findings.sort(key=lambda f: (f.path, f.line, f.col, f.rule))
    stats = {"n_files": len(rel_files), "analyzed": len(todo), "cached": cached, "ruff": ruff_status,
             "jobs": n_jobs if len(todo) > PARALLEL_THRESHOLD else 1,
             "seconds": round(time.perf_counter() - t0, 3)}
    return ScanResult(findings, [r for r, _ in rel_files], stats)


# --------------------------------------------------------------------- baseline ratchet
def load_baseline(path: Path) -> Counter | None:
    path = Path(path)
    if not path.exists():
        return None
    return Counter(json.loads(path.read_text(encoding="utf-8")).get("fingerprints", {}))


def write_baseline(path: Path, findings: list[Finding]) -> None:
    fps = Counter(fingerprint(f) for f in findings)
    doc = {"ruleset": RULESET_VERSION, "count": len(findings), "fingerprints": dict(sorted(fps.items())),
           "findings": [{"rule": f.rule, "path": f.path, "line": f.line, "snippet": f.snippet} for f in findings]}
    Path(path).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")


def split_new(findings: list[Finding], baseline: Counter) -> tuple[list[Finding], list[Finding], int]:
    """Split into (new, known, n_fixed). Counts matter: a second copy of a known bug is new."""
    left = Counter(baseline)
    new, known = [], []
    for f in findings:
        fp = fingerprint(f)
        if left[fp] > 0:
            left[fp] -= 1
            known.append(f)
        else:
            new.append(f)
    return new, known, sum(v for v in left.values() if v > 0)


def blocking(findings: list[Finding], fail_on: str) -> list[Finding]:
    if fail_on.lower() == "never":
        return []
    lvl = SEVERITY_RANK[fail_on.upper()]
    return [f for f in findings if f.severity in ("BLOCK", "WARN") and SEVERITY_RANK[f.severity] >= lvl]


# --------------------------------------------------------------------- reports
def to_markdown(res: ScanResult, gated: list[Finding], baseline_info: str = "") -> str:
    fs = res.findings
    sev = Counter(f.severity for f in fs)
    lines = ["# Codeguard report", "",
             f"Files: {res.stats['n_files']} (analyzed {res.stats['analyzed']}, cached {res.stats['cached']}); "
             f"{res.stats['ruff']}; {res.stats['seconds']}s", "",
             f"Findings: BLOCK {sev.get('BLOCK', 0)}, WARN {sev.get('WARN', 0)}, INFO {sev.get('INFO', 0)}"]
    if baseline_info:
        lines += ["", baseline_info]
    lines += ["", "## Summary by rule", "", "| rule | severity | count | title |", "|---|---|---|---|"]
    by_rule = Counter(f.rule for f in fs)
    for rule, n in sorted(by_rule.items(), key=lambda kv: (-SEVERITY_RANK.get(_sev_of(fs, kv[0]), 0), kv[0])):
        title = RULES[rule].title if rule in RULES else "ruff"
        lines.append(f"| {rule} | {_sev_of(fs, rule)} | {n} | {title} |")
    gated_ids = {id(f) for f in gated}
    lines += ["", "## Findings", ""]
    if not fs:
        lines.append("No findings.")
    for f in fs:
        mark = " (new)" if baseline_info and id(f) in gated_ids else ""
        lines += [f"### {f.path}:{f.line}:{f.col} {f.rule} {f.severity}{mark}", "", f.message, "",
                  "```python", f.snippet, "```", "", f"Fix: {f.fix}", ""]
    return "\n".join(lines) + "\n"


def _sev_of(fs, rule):
    return next((f.severity for f in fs if f.rule == rule), "WARN")


def to_json(res: ScanResult, gated: list[Finding], extra: dict | None = None) -> str:
    return json.dumps({"summary": {**res.stats, **(extra or {}), "n_findings": len(res.findings),
                                   "n_gated": len(gated)},
                       "findings": [f.to_dict() for f in res.findings]}, indent=1)


def to_sarif(res: ScanResult) -> dict:
    level = {"BLOCK": "error", "WARN": "warning", "INFO": "note"}
    rule_ids = sorted({f.rule for f in res.findings} | set(RULES))
    rules = []
    for rid in rule_ids:
        r = RULES.get(rid)
        rules.append({"id": rid, "name": rid,
                      "shortDescription": {"text": r.title if r else f"ruff {rid}"},
                      "help": {"text": r.fix if r else f"See ruff documentation for {rid}."},
                      "defaultConfiguration": {"level": level[r.severity] if r else "warning"}})
    results = [{"ruleId": f.rule, "level": level.get(f.severity, "warning"),
                "message": {"text": f"{f.message}. Fix: {f.fix}"},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": f.path},
                                                    "region": {"startLine": max(1, f.line),
                                                               "startColumn": max(1, f.col)}}}],
                "partialFingerprints": {"codeguard/v1": fingerprint(f)}}
               for f in res.findings]
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "codeguard", "version": RULESET_VERSION, "rules": rules}},
                      "results": results}]}
