"""Codeguard CLI.

    python -m codeguard check [PATHS] [--changed [REF]] [--baseline F] [--update-baseline]
                                      [--fail-on BLOCK|WARN|never] [--format md|json|sarif] [--out DIR]
    python -m codeguard gate  [PATHS]   check + import smoke test; prints GATE OPEN / GATE CLOSED
    python -m codeguard smoke [PATHS]
    python -m codeguard run SCRIPT [-- ARGS...]
    python -m codeguard rules

With a baseline file present, only NEW findings gate; otherwise all findings do.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from codeguard import engine
from codeguard.rules import RULES, RULESET_VERSION


def _add_scan_args(p):
    p.add_argument("paths", nargs="*", default=["."])
    p.add_argument("--root", default=".", help="repo root (config, cache, baseline live here)")
    p.add_argument("--baseline", default=None, help=f"baseline file (default: ROOT/{engine.BASELINE_FILE})")
    p.add_argument("--update-baseline", action="store_true", help="record current findings as the baseline")
    p.add_argument("--changed", nargs="?", const="HEAD", default=None, metavar="REF",
                   help="only files changed versus REF (default HEAD) plus untracked files")
    p.add_argument("--fail-on", default="BLOCK", choices=["BLOCK", "WARN", "never", "block", "warn"])
    p.add_argument("--jobs", type=int, default=None)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--no-ruff", action="store_true")
    p.add_argument("--format", default="md", choices=["md", "json", "sarif"])
    p.add_argument("--out", default=None, help="also write codeguard.md/.json/.sarif into this folder")
    p.add_argument("--quiet", action="store_true", help="print only the summary")


def _do_check(a):
    root = Path(a.root).resolve()
    res = engine.scan(a.paths, root=root, jobs=a.jobs, use_cache=not a.no_cache, use_ruff=not a.no_ruff,
                      changed_ref=a.changed)
    bpath = Path(a.baseline) if a.baseline else root / engine.BASELINE_FILE
    info, gated_pool, extra = "", res.findings, {}
    if a.update_baseline:
        engine.write_baseline(bpath, res.findings)
        info = f"Baseline updated: {len(res.findings)} findings recorded in {bpath.name}"
    else:
        base = engine.load_baseline(bpath)
        if base is not None:
            new, known, fixed = engine.split_new(res.findings, base)
            gated_pool = new
            info = (f"Baseline {bpath.name}: {len(new)} new, {len(known)} known, "
                    f"{fixed} baseline item(s) fixed since it was recorded")
            extra = {"new": len(new), "known": len(known), "fixed": fixed}
    gated = engine.blocking(gated_pool, a.fail_on)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "codeguard.md").write_text(engine.to_markdown(res, gated_pool, info), encoding="utf-8")
        (out / "codeguard.json").write_text(engine.to_json(res, gated, extra), encoding="utf-8")
        (out / "codeguard.sarif").write_text(json.dumps(engine.to_sarif(res), indent=1), encoding="utf-8")
    if not a.quiet:
        if a.format == "json":
            print(engine.to_json(res, gated, extra))
        elif a.format == "sarif":
            print(json.dumps(engine.to_sarif(res), indent=1))
        else:
            print(engine.to_markdown(res, gated_pool, info))
    sev = Counter(f.severity for f in res.findings)
    print(f"codeguard: {res.stats['n_files']} files ({res.stats['analyzed']} analyzed, {res.stats['cached']} cached) "
          f"in {res.stats['seconds']}s; BLOCK {sev.get('BLOCK', 0)} WARN {sev.get('WARN', 0)} "
          f"INFO {sev.get('INFO', 0)}; {res.stats['ruff']}", file=sys.stderr)
    if info:
        print(f"codeguard: {info}", file=sys.stderr)
    print(f"codeguard: {len(gated)} finding(s) at or above --fail-on {a.fail_on}", file=sys.stderr)
    return res, gated


def _do_smoke(res, root, jobs):
    from codeguard.smoke import smoke
    results = smoke(res.files, root=root, findings=res.findings, jobs=jobs)
    counts = Counter(r.status for r in results)
    for r in results:
        if r.status != "ok":
            print(f"  smoke {r.status.upper():8} {r.path}: {r.detail}")
    print(f"smoke: {counts.get('ok', 0)} imported, {counts.get('fail', 0)} failed, "
          f"{counts.get('timeout', 0)} timed out, {counts.get('skipped', 0)} skipped")
    return results, counts.get("fail", 0) + counts.get("timeout", 0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="codeguard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "gate", "smoke"):
        _add_scan_args(sub.add_parser(name))
    pr = sub.add_parser("run")
    pr.add_argument("script")
    pr.add_argument("args", nargs=argparse.REMAINDER)
    pr.add_argument("--report", default=None, help="write the JSON report here")
    sub.add_parser("rules")
    a = ap.parse_args(argv)

    if a.cmd == "rules":
        print(f"codeguard ruleset {RULESET_VERSION}")
        for r in RULES.values():
            print(f"{r.id}  {r.severity:5}  {r.title}\n        fix: {r.fix}")
        return 0
    if a.cmd == "run":
        from codeguard.debugrun import run
        args = a.args[1:] if a.args[:1] == ["--"] else a.args
        rep = run(a.script, args, report_path=a.report)
        return 0 if rep["status"] == "ok" else 1
    if a.cmd == "check":
        _, gated = _do_check(a)
        return 1 if gated else 0
    if a.cmd == "smoke":
        a.quiet = True
        a.fail_on = "never"
        res, _ = _do_check(a)
        _, bad = _do_smoke(res, Path(a.root).resolve(), a.jobs)
        return 1 if bad else 0
    # gate
    a.quiet = a.quiet or a.format == "md"
    res, gated = _do_check(a)
    for f in gated[:50]:
        print(f"  {f.severity} {f.path}:{f.line}:{f.col} {f.rule} {f.message}")
    _, bad = _do_smoke(res, Path(a.root).resolve(), a.jobs)
    if gated or bad:
        print(f"GATE CLOSED: {len(gated)} blocking finding(s), {bad} import failure(s)")
        return 1
    print("GATE OPEN: 0 blocking findings, 0 import failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
