"""Monthly and quarterly reviews: where the reports build on each other.

Monthly   forecast scorecard for the month and to date; every written behavior (hypothesis) re-tested and
          its status logged (validated, regime-dependent, invalidated, inconclusive); personality changes and
          flips opened or closed; measured profile snapshot compared with last month; plan alignment trend and
          pivot triggers; event library by type; pressure half lives refitted (applied automatically).
Quarterly everything monthly, plus the validation tests from the framework (levels versus random, trait
          rankings out of sample, pair beta identity, incentive gap closure), recalibration of T and lambda,
          grade threshold recalibration, setup retirement, the two pivot triggers, and the ledger review list.
All parameter changes apply automatically (owner decision) and are logged with their reason and evidence.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from .journal import fit_temperature, scorecard
from .pairs import gap_closure_rate
from .players import cycle_staleness, evaluate_hypothesis

PROFILE_TRAITS = ["energy", "conviction", "patience", "fear_beta", "rate_beta", "resource_beta", "asymmetry", "event_reactivity"]


def period_bounds(asof: pd.Timestamp, kind: str) -> tuple[pd.Timestamp, pd.Timestamp, str]:
    """The period just completed (or running, if forced mid-period): previous month or quarter."""
    if kind == "monthly":
        first_this = asof.replace(day=1)
        start = (first_this - pd.DateOffset(months=1)).normalize()
        end = first_this - pd.Timedelta(days=1)
        return start, end, start.strftime("%Y-%m")
    q = (asof.month - 1) // 3
    first_this_q = pd.Timestamp(asof.year, 3 * q + 1, 1)
    start = first_this_q - pd.DateOffset(months=3)
    end = first_this_q - pd.Timedelta(days=1)
    return start, end, f"{start.year}-Q{(start.month - 1) // 3 + 1}"


def _spearman(a: list[float], b: list[float]) -> Optional[float]:
    if len(a) < 4:
        return None
    ra, rb = pd.Series(a).rank(), pd.Series(b).rank()
    v = ra.corr(rb)
    return None if pd.isna(v) else float(v)


def hypotheses_step(ctx, kind_label: str) -> dict:
    res = []
    tc = getattr(ctx, "traits_cache", {})
    regimes = getattr(ctx, "regimes", pd.Series(dtype=object))
    for h in ctx.ledger.hypotheses:
        subj, metric = h.get("subject"), h.get("metric", "")
        now = long = None
        reg_vals: dict[str, Optional[float]] = {}
        if metric.startswith("corr:"):
            other = metric.split(":", 1)[1]
            a, b = ctx.rets.get(subj), ctx.rets.get(other)
            if a is not None and b is not None:
                j = pd.concat([a, b], axis=1, join="inner").dropna()
                if len(j) > 400:
                    c = j.iloc[:, 0].rolling(60).corr(j.iloc[:, 1])
                    now = float(c.iloc[-1])
                    long = float(c.iloc[-1260:].median())
                    if not regimes.empty:
                        for reg, g in c.groupby(regimes.reindex(c.index)):
                            if len(g.dropna()) > 120:
                                reg_vals[str(reg)] = float(g.median())
        else:
            t = tc.get(subj)
            if t is not None and metric in t:
                s = t[metric].dropna()
                if len(s) > 250:
                    now, long = float(s.iloc[-1]), float(s.iloc[-5040:].median())
                    if not regimes.empty:
                        for reg, g in s.groupby(regimes.reindex(s.index)):
                            if len(g) > 120:
                                reg_vals[str(reg)] = float(g.median())
        res.append(evaluate_hypothesis(h, now, long, reg_vals))
    prev = ctx.journal.read("hypotheses_log.csv")
    last_status = {}
    if not prev.empty:
        for _, r in prev.iterrows():
            last_status[r["id"]] = r["status"]
    changes = [{"id": r["id"], "from": last_status[r["id"]], "to": r["status"], "text": r["text"]}
               for r in res if r["id"] in last_status and last_status[r["id"]] != r["status"]]
    ctx.journal.append("hypotheses_log.csv", [{"period": kind_label, "date": str(ctx.asof.date()), "id": r["id"], "subject": r["subject"],
                                               "metric": r["metric"], "expect": r["expect"], "now": r["now"], "long_run": r["long_run"],
                                               "status": r["status"], "regimes": r["regimes"]} for r in res])
    return {"results": res, "changes": changes}


def profile_snapshot(ctx, kind_label: str, views: dict) -> dict:
    """Measured personality per instrument for this period, compared with the previous snapshot."""
    rows = []
    for sym, v in views.items():
        if v.get("error"):
            continue
        b = v.get("bands") or {}
        row = {"period": kind_label, "date": str(ctx.asof.date()), "symbol": sym}
        for t in PROFILE_TRAITS:
            row[t] = (b.get(t) or {}).get("value")
            row[f"{t}_pct"] = (b.get(t) or {}).get("pct")
        row["band"] = v.get("overall_band")
        rows.append(row)
    prev = ctx.journal.read("profiles.csv")
    changes = []
    if not prev.empty:
        last = prev[prev["period"] == prev["period"].iloc[-1]]
        lp = {r["symbol"]: r for _, r in last.iterrows()}
        for r in rows:
            o = lp.get(r["symbol"])
            if o is None:
                continue
            for t in PROFILE_TRAITS:
                try:
                    a, b = float(o[f"{t}_pct"]), float(r[f"{t}_pct"])
                except (TypeError, ValueError):
                    continue
                if abs(a - b) >= 25:
                    changes.append({"symbol": r["symbol"], "trait": t, "from_pct": round(a, 1), "to_pct": round(b, 1),
                                    "reading": _trait_reading(t, b)})
                if t in ("fear_beta", "resource_beta", "rate_beta"):
                    try:
                        va, vb = float(o[t]), float(r[t])
                        if np.sign(va) != np.sign(vb) and max(abs(va), abs(vb)) > 0:
                            changes.append({"symbol": r["symbol"], "trait": t, "from_pct": round(a, 1), "to_pct": round(b, 1),
                                            "reading": f"sign changed {va:+.4g} -> {vb:+.4g}"})
                    except (TypeError, ValueError):
                        pass
    ctx.journal.append("profiles.csv", rows)
    return {"rows": rows, "changes": changes}


def _trait_reading(t: str, pct: float) -> str:
    hi = pct >= 75
    lo = pct <= 25
    return {"energy": "wilder than usual" if hi else "quieter than usual" if lo else "normal energy",
            "conviction": "trending more" if hi else "ranging more" if lo else "normal persistence",
            "patience": "slower to revert" if hi else "faster to revert" if lo else "normal reversion",
            "asymmetry": "falls sharper than rises" if hi else "rises sharper than falls" if lo else "symmetric"}.get(t, "")


def alignment_step(ctx, events_view: dict) -> dict:
    cur = events_view.get("alignment") or {}
    prev = ctx.journal.read("alignment_log.csv")
    last = {}
    if not prev.empty:
        for _, r in prev.iterrows():
            last[r["goal"]] = r["A"]
    pivots = []
    for gid, a in cur.items():
        try:
            old = float(last.get(gid)) if last.get(gid) not in (None, "") else None
        except ValueError:
            old = None
        if old is not None and a["A"] is not None and np.sign(old) > 0 and np.sign(a["A"]) < 0:
            pivots.append({"goal": gid, "from": old, "to": a["A"], "text": "turned from positive to negative: pivot trigger for every trade anchored to it"})
    ctx.journal.append("alignment_log.csv", [{"date": str(ctx.asof.date()), "goal": g, "player": a["player"], "A": a["A"],
                                              "events": a["events"], "credibility": a["credibility"]} for g, a in cur.items()])
    return {"scores": cur, "pivots": pivots}


def event_half_lives(ctx, lib: dict) -> list[dict]:
    """Refit pressure half lives by event type from measured persistence (applied automatically)."""
    minimum = int(ctx.cfg.get("events", {}).get("min_events_for_half_life", 10))
    cur = dict(ctx.p("event_half_life", {}) or {})
    changed = []
    for t, s in lib.items():
        if s["n"] >= minimum and s.get("half_life") is not None:
            old = cur.get(t)
            new = float(s["half_life"])
            if old is None or abs(float(old) - new) >= 1.0:
                cur[t] = new
                changed.append({"type": t, "old": old, "new": new, "n": s["n"]})
    if changed:
        ctx.journal.change_param(ctx.params, "event_half_life", cur, "pressure half lives refit from measured persistence",
                                 "; ".join(f"{c['type']} n={c['n']}" for c in changed), str(ctx.asof.date()))
        ctx.journal.save_params(ctx.params)
    return changed


# =============================================================================
# Quarterly validation tests
# =============================================================================

def level_edge_test(views: dict) -> dict:
    rows = []
    for sym, v in views.items():
        fe = v.get("level_edge")
        if not fe:
            continue
        rows.append({"symbol": sym, **{f"{f}_z": (fe.get(f) or {}).get("z") for f in ("round_major", "round_mid", "market", "pain")},
                     "p0": v.get("p0"), "round_touches": sum((fe.get(f) or {}).get("touches", 0) for f in ("round_major", "round_mid"))})
    rz = [r.get("round_major_z") for r in rows if r.get("round_major_z") is not None] + \
         [r.get("round_mid_z") for r in rows if r.get("round_mid_z") is not None]
    mz = [r.get("market_z") for r in rows if r.get("market_z") is not None]

    def verdict(z: list) -> str:
        if not z:
            return "not measurable"
        share = sum(x > 2 for x in z) / len(z)
        return "edge" if share >= 0.5 else "weak" if float(np.median(z)) > 0 else "no edge"

    return {"rows": rows, "round": verdict(rz), "market": verdict(mz),
            "round_median_z": None if not rz else round(float(np.median(rz)), 2),
            "market_median_z": None if not mz else round(float(np.median(mz)), 2)}


def trait_oos_test(ctx) -> dict:
    """Fit on the first 14 years, check on the last 6: do the trait rankings between instruments hold?"""
    tc = getattr(ctx, "traits_cache", {})
    out = {}
    split_years = 6
    for t in PROFILE_TRAITS:
        a, b = [], []
        for sym, df in tc.items():
            s = df[t].dropna()
            if len(s) < 2000:
                continue
            cut = s.index[-1] - pd.DateOffset(years=split_years)
            early, late = s[s.index < cut], s[s.index >= cut]
            if len(early) > 500 and len(late) > 250:
                a.append(float(early.median()))
                b.append(float(late.median()))
        rho = _spearman(a, b)
        out[t] = {"instruments": len(a), "rank_corr": None if rho is None else round(rho, 2),
                  "pass": None if rho is None else rho >= 0.5}
    return out


def pair_beta_test(views: dict) -> dict:
    rows = []
    for sym, v in views.items():
        bc = (v.get("pair") or {}).get("beta_check") or {}
        for b, x in bc.items():
            if x.get("predicted") is not None and x.get("measured") is not None:
                rows.append({"symbol": sym, "beta": b, **x})
    if len(rows) < 3:
        return {"rows": rows, "corr": None, "pass": None}
    p = np.array([r["predicted"] for r in rows])
    m = np.array([r["measured"] for r in rows])
    c = float(np.corrcoef(p, m)[0, 1]) if np.std(p) > 0 and np.std(m) > 0 else None
    return {"rows": rows, "corr": None if c is None else round(c, 3), "pass": None if c is None else c >= 0.9}


def gap_test(ctx) -> dict:
    out = {}
    for sym, G in (getattr(ctx, "gap_hist", {}) or {}).items():
        if G is None or len(G) < 500:
            continue
        out[sym] = gap_closure_rate(G)
    rates = [v["rate"] for v in out.values() if v["rate"] is not None]
    return {"pairs": out, "median_rate": None if not rates else round(float(np.median(rates)), 3),
            "pass": None if not rates else float(np.median(rates)) > 0.5,
            "note": "before trading costs; the pass mark is better than a coin flip after costs"}


def recalibrate(ctx, fc: pd.DataFrame) -> list[dict]:
    """Quarterly, automatic: T and lambda from the forecast log; grade thresholds; retire failing setups."""
    lk = ctx.cfg["likelihood"]
    changes = []
    live = "demo" if ctx.demo else "live"      # demo runs calibrate on their own demo log, never on live data
    d = fc[(fc.get("status") == "resolved") & (fc.get("source") == live)] if not fc.empty else fc
    n = 0 if d is None or d.empty else len(d)
    min_n = int(lk.get("min_forecasts_for_fit", 30))
    run = str(ctx.asof.date())
    if n >= min_n:
        best = fit_temperature(d, list(lk.get("T_grid", [0.1, 0.2, 0.35, 0.5, 0.75, 1, 1.5, 2, 3, 5])),
                               list(lk.get("lambda_grid", [0, 0.5, 1, 2, 4])))
        if best:
            for k, key in (("T", "T"), ("lambda", "lambda")):
                if best[k] != ctx.p(key):
                    changes.append({"key": key, "old": ctx.p(key), "new": best[k]})
                    ctx.journal.change_param(ctx.params, key, best[k], "quarterly calibration on the forecast log",
                                             f"log loss {best['logloss']} on {best['n']} resolved live forecasts", run)
    else:
        changes.append({"key": "T", "note": f"kept: {n} resolved live forecasts, need {min_n}"})
    # grade thresholds: after 30 cases per grade, nudge the direction threshold toward the measured hit rate
    gmin = int(ctx.cfg["grading"].get("min_cases_for_recalibration", 30))
    for g_name, key in (("A", "p_dir_a"), ("B", "p_dir_b")):
        g = d[d["grade"] == g_name] if n else pd.DataFrame()
        if len(g) >= gmin:
            hit = pd.to_numeric(g["hit"], errors="coerce").mean()
            th = float(ctx.p(key))
            new = th
            if hit < th - 0.05:
                new = min(th + 0.025, 0.75)
            elif hit > th + 0.10:
                new = max(th - 0.025, 0.50)
            if new != th:
                changes.append({"key": key, "old": th, "new": new})
                ctx.journal.change_param(ctx.params, key, new, f"grade {g_name} hit rate {hit:.2f} against threshold {th:.2f}",
                                         f"{len(g)} resolved grade {g_name} forecasts", run)
    # retire setups that run below a coin flip after 30 cases
    retired = list(ctx.p("retired_setups") or [])
    if n:
        for st, g in d.groupby("setup_type"):
            if st in ("none", "") or st in retired:
                continue
            hit = pd.to_numeric(g["hit"], errors="coerce").mean()
            if len(g) >= gmin and hit < 0.5:
                retired.append(st)
                changes.append({"key": "retired_setups", "new": st, "hit_rate": round(float(hit), 3)})
        if retired != list(ctx.p("retired_setups") or []):
            ctx.journal.change_param(ctx.params, "retired_setups", retired, "setup below a coin flip after 30 cases",
                                     "forecast log", run)
    ctx.journal.save_params(ctx.params)
    return changes


def pivot_checks(ctx, views: dict, lvl: dict, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    out = []
    run = str(ctx.asof.date())
    if lvl["round"] == "no edge" and ctx.p("round_levels_enabled", True):
        ctx.journal.change_param(ctx.params, "round_levels_enabled", False,
                                 "pivot trigger: round zones show no edge over random zones",
                                 f"median round z {lvl['round_median_z']}", run)
        ctx.journal.save_params(ctx.params)
        out.append({"trigger": "round zones show no edge", "action": "fixed grid dropped; maps now use market-made levels"})
    elif lvl["round"] == "no edge":
        out.append({"trigger": "round zones still show no edge", "action": "grid stays off"})
    elif lvl["round"] == "edge" and not ctx.p("round_levels_enabled", True):
        ctx.journal.change_param(ctx.params, "round_levels_enabled", True, "round zones regained an edge over random zones",
                                 f"median round z {lvl['round_median_z']}", run)
        ctx.journal.save_params(ctx.params)
        out.append({"trigger": "round zones regained an edge", "action": "fixed grid restored"})
    regimes = getattr(ctx, "regimes", pd.Series(dtype=object))
    dx = ctx.series.get("DXY")
    if not regimes.empty and dx is not None and not dx.bars.empty:
        q = regimes[(regimes.index >= start) & (regimes.index <= end)]
        sq = q[q == "dollar squeeze"]
        if len(sq) >= 20:
            r = np.log(dx.close).diff().reindex(sq.index).sum()
            if r < 0:
                out.append({"trigger": "the dollar fell during a dollar squeeze regime",
                            "action": f"center of gravity read may be wrong ({len(sq)} days, dollar {r:+.3%}): rebuild the influence map"})
    return out


def run_review(ctx, kind: str, daily: dict) -> dict:
    start, end, label = period_bounds(ctx.asof, kind)
    fc = ctx.journal.read("forecasts.csv")
    period_fc = fc
    if not fc.empty:
        rd = pd.to_datetime(fc["resolved_date"].replace("", None), errors="coerce")
        period_fc = fc[(rd >= start) & (rd <= end)]
    views = daily["views"]
    out: dict[str, Any] = {"meta": {"kind": kind, "period": label, "start": str(start.date()), "end": str(end.date()),
                                    "date": str(ctx.asof.date()), "config_version": ctx.cfg["config_version"],
                                    "ledger_version": ctx.ledger.version, "demo": ctx.demo},
                           "scorecard_period": scorecard(period_fc), "scorecard_to_date": scorecard(fc),
                           "hypotheses": hypotheses_step(ctx, label), "profiles": profile_snapshot(ctx, label, views),
                           "alignment": alignment_step(ctx, daily["events"]), "regime": daily["regime"]}
    pc = ctx.journal.read("personality_changes.csv")
    if not pc.empty:
        d = pd.to_datetime(pc["date"])
        out["personality_changes"] = pc[(d >= start) & (d <= ctx.asof)].to_dict("records")
        out["open_changes"] = pc[pc["status"] == "open"].to_dict("records")
    fl = ctx.journal.read("flips.csv")
    if not fl.empty:
        out["flips"] = fl.to_dict("records")
    out["faces"] = {s: {"now": v.get("regime_face"), "all": v.get("faces")} for s, v in views.items() if not v.get("error")}
    out["event_library"] = daily["events"].get("library")
    out["half_life_changes"] = event_half_lives(ctx, daily["events"].get("library") or {})
    out["cycle_staleness"] = cycle_staleness(ctx.ledger, ctx.asof)
    ph = ctx.journal.read("params_changelog.csv")
    if not ph.empty:
        out["param_changes"] = ph[pd.to_datetime(ph["date"]) >= start].to_dict("records")
    lc = ctx.journal.read("ledger_changes.csv")
    if not lc.empty:
        out["ledger_changes"] = lc[pd.to_datetime(lc["run"]) >= start].to_dict("records")
    if kind == "quarterly":
        lvl = level_edge_test(views)
        out["validation"] = {"levels": lvl, "traits": trait_oos_test(ctx), "pair_math": pair_beta_test(views),
                             "incentive_gap": gap_test(ctx)}
        out["recalibration"] = recalibrate(ctx, fc)
        out["pivots"] = pivot_checks(ctx, views, lvl, start, end)
        out["ledger_review"] = [{"player": p, "weight": v.get("weight"), "evidence": v.get("weight_evidence", "working assumption")}
                                for p, v in ctx.ledger.players.items()]
        out["independence_note"] = ("P_opp assumes direction, reach and hold are roughly independent. The forecast log "
                                    "resolves direction only; reach and hold outcomes are not yet resolved, so the "
                                    "independence check is pending.")
    ctx.journal.append("reviews.csv", [{"date": str(ctx.asof.date()), "kind": kind, "period": label,
                                        "params_version": ctx.params["version"]}])
    return out
