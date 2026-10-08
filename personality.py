#!/usr/bin/env python3
"""
Empire Game Market Personality engine
=====================================

Implements "Empire Game Market Personality Framework" (October 6, 2026): a five layer model (incentives,
personalities, the 20 year price map, events, evolution) that turns three questions into numbers: who
gains from a move, how each instrument normally behaves, and where on the map the fight should happen.

Cadence
    Daily      regime, scoreboard, calendar, ledger and events, character, incentive gaps, map, story cards,
               likelihoods, grades, scanner alignment, exposure check. Every forecast is logged before the
               outcome and resolved after it, so each day's report checks yesterday's calls.
    Monthly    forecast scorecard, behaviors (hypotheses) validated or invalidated, profile changes, plan
               alignment and pivot triggers, pressure half lives refit.
    Quarterly  validation tests (levels vs random, trait rankings out of sample, pair math identity,
               incentive gap closure), recalibration of T, lambda and grade thresholds, setup retirement,
               pivot triggers, ledger review, personality quarter by quarter.
    Annual     the quarterly validation tests (no second recalibration) and personality year by year over the
               whole history: which instruments changed, and the years each one changed most.
    --run auto (default) runs the daily loop, then the monthly review if last month has none yet, then
    the quarterly and annual reviews if last quarter or last year has none yet. Changes apply automatically
    and are logged.

Inputs
    personality_config.yaml   versioned parameters, universe, sources, scanners to compare against
    personality_ledger.yaml   players, incentive scores (B, K, U, C), levers, pain zones, plans, hypotheses
    personality_events.yaml   events you log (wars, sanctions, policy shifts), with tier and verdict
    scanner_config.yaml       reused for MT5 settings, COT markets and the Forex Factory calendar archive

Usage
    python personality.py --demo                 # synthetic data, no network: check the install
    python personality.py                        # live daily run (+ monthly/quarterly when due)
    python personality.py --run monthly          # force a review now
    python personality.py --run annual           # last calendar year, personality year by year
    python personality.py --asof 2026-10-06      # point in time
    python personality.py --daemon               # stay running, daily at schedule.daily_utc on weekdays

Outputs
    output/personality/daily_<date>.html|json, monthly_<YYYY-MM>.html|json, quarterly_<YYYY-Qn>.html|json,
               annual_<YYYY>.html|json, evolution_<period>.html (second page of each quarterly and annual review)
    data/personality/   the journal: forecasts, traits, changes, flips, hypotheses, parameters, ledger snapshots

Nothing here places orders. It is an analytical framework, not investment advice.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from empire import ENGINE_VERSION
from empire.monitor import build_context, run_daily
from empire.report import render_daily, render_evolution, render_review, write_json
from empire.review import period_bounds, run_review

LOG = logging.getLogger("empire")


def out_dir(ctx) -> Path:
    d = ctx.base / ctx.cfg["paths"]["output_dir"]
    if ctx.demo:
        d = d / "demo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def review_due(ctx, kind: str) -> bool:
    _, _, label = period_bounds(ctx.asof, kind)
    return not (out_dir(ctx) / f"{kind}_{label}.json").exists()


def run_once(cfg_path: Path, asof: Optional[pd.Timestamp], run: str, demo: bool) -> list[Path]:
    ctx = build_context(cfg_path, asof, run, demo)
    od = out_dir(ctx)
    t0 = time.time()
    daily = run_daily(ctx)
    paths = []
    stem = f"daily_{daily['meta']['date']}"
    write_json(od / f"{stem}.json", daily)
    (od / f"{stem}.html").write_text(render_daily(daily), encoding="utf-8")
    paths.append(od / f"{stem}.html")
    LOG.info("daily report written in %.0fs: %s", time.time() - t0, paths[-1])
    kinds = []
    if run in ("monthly", "quarterly"):
        kinds = ["monthly"] + (["quarterly"] if run == "quarterly" else [])
    elif run == "annual":
        kinds = ["annual"]
    elif run == "auto":
        kinds = [k for k in ("monthly", "quarterly", "annual") if review_due(ctx, k)]
    for k in kinds:
        rv = run_review(ctx, k, daily)
        stem = f"{k}_{rv['meta']['period']}"
        write_json(od / f"{stem}.json", rv)
        (od / f"{stem}.html").write_text(render_review(rv), encoding="utf-8")
        paths.append(od / f"{stem}.html")
        LOG.info("%s review written: %s", k, paths[-1])
        if rv.get("evolution"):
            (od / f"evolution_{rv['meta']['period']}.html").write_text(render_evolution(rv), encoding="utf-8")
            paths.append(od / f"evolution_{rv['meta']['period']}.html")
            LOG.info("evolution page written: %s", paths[-1])
    return paths


def daemon(cfg_path: Path, demo: bool) -> None:
    from empire.config import load_config
    cfg = load_config(cfg_path)
    hh, mm = (int(x) for x in str(cfg.get("schedule", {}).get("daily_utc", "22:30")).split(":"))
    LOG.info("daemon: daily run at %02d:%02d UTC, Monday to Friday", hh, mm)
    last = None
    while True:
        now = pd.Timestamp.now(tz="UTC")
        if now.dayofweek < 5 and (now.hour, now.minute) >= (hh, mm) and last != now.date():
            try:
                for p in run_once(cfg_path, None, "auto", demo):
                    print(p)
            except Exception:  # noqa: BLE001
                LOG.exception("run failed")
            last = now.date()
        time.sleep(60)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=f"Empire Game Market Personality engine {ENGINE_VERSION}")
    ap.add_argument("--config", default="personality_config.yaml")
    ap.add_argument("--run", default="auto", choices=["auto", "daily", "monthly", "quarterly", "annual"])
    ap.add_argument("--asof", default=None, help="point in time date, e.g. 2026-10-06 (default: today)")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network; separate state and output folders")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    cfg_path = Path(a.config).resolve()
    log_dir = cfg_path.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_dir / "personality.log", encoding="utf-8")])
    if a.daemon:
        daemon(cfg_path, a.demo)
        return 0
    asof = pd.Timestamp(a.asof) if a.asof else None
    for p in run_once(cfg_path, asof, a.run, a.demo):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
