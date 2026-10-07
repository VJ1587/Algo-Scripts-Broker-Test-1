"""Audit HTML files that were not generated in Python (MT5 reports, TradingView exports, hand edits).

    python audit_html.py report.html reports/            # files or folders (recursive *.htm*)
    python audit_html.py journal.html --expected exp.json   # also reconcile tagged metrics

Prints PASS / WARN / FAIL per file with its issues. Exit code 1 if any file
FAILs (structure, bad tokens such as nan/None, external resources, injection,
encoding, or reconcile when --expected is given).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _files(paths):
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(x for x in p.rglob("*.htm*") if x.is_file())
        elif p.is_file():
            yield p
        else:
            print(f"skip: {p} does not exist", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--expected", help="JSON file with expected metric values (n_trades, net_pnl, ...)")
    ap.add_argument("--json", action="store_true", help="print machine readable results")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from stresslab.html_stress import audit_html_file

    expected = json.loads(Path(a.expected).read_text(encoding="utf-8")) if a.expected else None
    results = [audit_html_file(f, expected) for f in _files(a.paths)]
    if a.json:
        print(json.dumps(results, indent=1, default=str))
    else:
        for r in results:
            tables = ", ".join(f"#{t['index']} {t['rows']}x{t['cols']}" for t in r["tables"]) or "none"
            print(f"{r['status']:4}  {r['path']}  [{r['encoding']}; tables: {tables}]")
            for status, check, msg in r["issues"]:
                print(f"      {status:4} {check}: {msg}")
    counts = {s: sum(r["status"] == s for r in results) for s in ("PASS", "WARN", "FAIL")}
    print(f"audited {len(results)} file(s): PASS {counts['PASS']}, WARN {counts['WARN']}, FAIL {counts['FAIL']}")
    return 1 if counts["FAIL"] or not results else 0


if __name__ == "__main__":
    sys.exit(main())
