# CLAUDE.md

Repo: MT5 broker algorithm, daily scanner, trade gate, plus the StressLab
harness (`stresslab/`, `examples/`, `run_stress.py`) specified in
`BUILD_SPEC.md`. Build phases in order; do not start a phase until the
previous one meets its acceptance criteria.

## Ground rules (keep these in every session)

1. Python 3.11+. Dependencies: numpy pandas scipy pytest ruff beautifulsoup4
   (optional: playwright + chromium for browser checks).
2. Every function that uses randomness takes a `seed` and uses
   `np.random.default_rng(seed)`. Never call global `np.random.*`.
3. All timestamps are tz aware UTC. MT5 times are broker server time; convert
   with an explicit, broker confirmed offset.
4. No code runs at import time except definitions. Connecting, downloading,
   writing files, subprocesses and orders go inside functions or under
   `if __name__ == "__main__":`.
5. Every check reports PASS, WARN or FAIL with a one line reason. FAIL = the
   result cannot be trusted. WARN = a human should look.
6. The harness must prove it still works: every bug class it detects has a
   planted bug component (e.g. `LeakyScanner`, `SloppyPersonality`) and a
   test asserting it is caught. Never delete or weaken those tests.
7. Never use lookahead constructs in harness code: `shift(-n)`,
   `rolling(center=True)`, `bfill`, full sample normalisation or ranking.
   Planted bugs carry `# guard: ignore[RULE] reason`.
8. Do not invent statistics, paper titles, URLs or quotes. Synthetic
   magnitudes are illustrative; historical dates say "verify against primary
   sources".

## Commands

    python run_stress.py            # good stack, expect 0 FAIL
    python run_stress.py --buggy    # planted bugs, expect FAILs
    python -m pytest -q
    python -m codeguard gate .      # static rules + import smoke test, expect GATE OPEN
    python -m codeguard run SCRIPT  # explain a crash (frames, DataFrame summaries, hints)
    python run_stress.py --browser  # adds headless Chromium checks on the HTML reports
    python audit_html.py PATHS      # audit HTML files not generated in Python

Codeguard suppressions need a reason: `# guard: ignore[TG301] why this is safe`.
Bump `RULESET_VERSION` in codeguard/rules.py whenever a rule changes.
HTML reports follow the tagging contract in stresslab/html_stress.py (data-metric,
data-table="trades" with data-total, data-chart="equity" JSON via escape_json_for_script).
