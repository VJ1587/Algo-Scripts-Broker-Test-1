"""Run the full StressLab suite and write reports/.

    python run_stress.py                # good demo stack, expect 0 FAIL
    python run_stress.py --buggy        # planted bug stack, expect FAILs
    python run_stress.py --update-golden

Exit code 1 if any check FAILs.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build_suite(buggy: bool, update_golden: bool, browser: bool, verbose: bool = True):
    from examples.demo_components import (DemoPersonality, DonchianScanner, FixedFractionalExecutor,
                                          LeakyScanner, SimpleJournal, SloppyPersonality)
    from stresslab.suite import StressSuite

    scanner_cls = LeakyScanner if buggy else DonchianScanner
    params = {"lookback": 55, "atr_len": 14, "stop_atr": 2.0, "target_atr": 3.0}
    report = None
    try:
        from examples import demo_reports  # Phase 3
        report = demo_reports.NaiveJournalReport() if buggy else demo_reports.SafeJournalReport()
    except ImportError:
        report = None
    return StressSuite(
        personality=SloppyPersonality() if buggy else DemoPersonality(),
        scanner=scanner_cls(**params),
        executor_factory=lambda: FixedFractionalExecutor(risk_frac=0.01, max_open=1),
        journal_factory=SimpleJournal,
        scanner_factory=scanner_cls,
        scanner_params=params,
        seeds=(1, 2, 3),
        golden_dir=ROOT / "golden",
        update_golden=update_golden,
        report_generator=report,
        report_browser=browser,
        verbose=verbose,
    )


def _md_table(df) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v).replace("|", "/").replace("\n", " ") for v in row) + " |"
              for row in df.itertuples(index=False, name=None)]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--buggy", action="store_true", help="run the planted bug stack")
    ap.add_argument("--update-golden", action="store_true", help="rewrite golden master snapshots")
    ap.add_argument("--browser", action="store_true", help="also run Playwright browser checks on HTML reports")
    ap.add_argument("--out", default=str(ROOT / "reports"), help="output folder")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    import pandas as pd

    tag = "buggy" if args.buggy else "good"
    t0 = time.perf_counter()
    suite = build_suite(args.buggy, args.update_golden, args.browser)
    scorecard, scenarios, mc, sens = suite.run_all()
    elapsed = time.perf_counter() - t0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    scorecard.to_csv(out / f"scorecard_{tag}.csv", index=False)
    scenarios.to_csv(out / f"scenarios_{tag}.csv", index=False)
    suite.html_detail.to_csv(out / f"html_reports_{tag}.csv", index=False)
    counts = scorecard["status"].value_counts().reindex(["PASS", "WARN", "FAIL"], fill_value=0)
    md = [f"# StressLab scorecard ({tag})", "",
          f"PASS {counts['PASS']}, WARN {counts['WARN']}, FAIL {counts['FAIL']}; {elapsed:.1f}s", "",
          "## Checks", "", _md_table(scorecard), "", "## Scenarios", "",
          _md_table(scenarios.drop(columns=["detail"]).round(3))]
    if mc:
        md += ["", "## Monte Carlo (baseline scenarios pooled)", "",
               _md_table(pd.DataFrame([{k: (round(v, 4) if isinstance(v, float) else v) for k, v in mc.items()}]))]
    if sens:
        md += ["", "## Parameter sensitivity", "",
               _md_table(pd.DataFrame([{k: round(v, 4) for k, v in sens.items() if isinstance(v, (int, float))}]))]
    (out / f"scorecard_{tag}.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print()
    print(f"Scorecard ({tag}): PASS {counts['PASS']}  WARN {counts['WARN']}  FAIL {counts['FAIL']}  "
          f"in {elapsed:.1f}s")
    print(f"Reports written to {out}/scorecard_{tag}.md/.csv, scenarios_{tag}.csv, html_reports_{tag}.csv")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
