"""The daily loop: game first, chart last.

    1 Regime      which of the four regimes is in force, and has it changed
    2 Scoreboard  dollar index versus the equal weight dollar: do they agree
    3 Calendar    scheduled windows in the next 5 days
    4 Ledger      incentive scores, events, pressure, plan alignment
    5 Character   band status, distance from normal, drift, relationship flips
    6 Gap         pairs more than 2 units from their drivers
    7 Map         nearest scored zones above and below
    8 Story       one card per instrument; yesterday's forecasts checked against what happened
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from . import ENGINE_VERSION
from .config import MAJORS, Inst, build_instruments, load_config, load_yaml, usd_sign
from .data import DataLoader, Series, clean_daily, last_completed_session, returns
from .events import (QUALIFYING_QUESTIONS, counts_for_pressure, event_study, event_weight, fingerprint,
                     load_calendar_events, load_owner_events, needle_test, persistence_half_life, pressure_series)
from .journal import FORECAST_FIELDS, Journal, resolve_forecasts
from .levels import (Zone, build_map, distance_units, forward_excursions, outcome_base_rates, touch_probability,
                     touch_probability_formula, vacuum_between, zone_edge_ok)
from .pairs import carry_appeal, dominance, incentive_gap, predicted_pair_beta, predicted_pair_energy
from .players import merge_scorecard, alignment_scores, cycle_staleness, incentive_weights, parse_ledger
from .push import efficiency_ratio, push_score, push_signals
from .regimes import REGIME_TEXT, regime_faces, regime_frame, relationship_status
from .stories import (DIR, alignment_cell, ev_entry, exposure_check, gate_results, grade, likelihood,
                      load_scanner, setup_type)
from .traits import (DISTANCE_TRAITS, atr_series, character_distance, compute_traits, cross_from_legs,
                     currency_log_values, drift_streak, ewma_drift, measured_range_band, solo_indices, trait_bands,
                     vol_range_band)

LOG = logging.getLogger("empire.monitor")
HIST_DAYS = 5040   # about 20 years of trading days


@dataclass
class Ctx:
    cfg: dict
    base: Path
    asof: pd.Timestamp
    stamp: str
    run_type: str
    demo: bool
    insts: dict
    ledger: Any
    ledger_path: Path
    journal: Journal
    params: dict
    series: dict = field(default_factory=dict)
    rets: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    scanner_cfg: dict = field(default_factory=dict)

    def p(self, key: str, default=None):
        return self.params["values"].get(key, default)


def default_params(cfg: dict) -> dict:
    lk, g = cfg["likelihood"], cfg["grading"]
    return {"T": float(lk.get("T_initial", 2.0)), "lambda": float(lk.get("lambda_initial", 1.0)),
            "p_dir_a": float(g.get("p_dir_a", 0.60)), "p_dir_b": float(g.get("p_dir_b", 0.55)),
            "ev_min": float(g.get("ev_min", 0.5)), "reward_min": float(g.get("reward_min", 2.0)),
            "pressure_pct_min": float(g.get("pressure_pct_min", 90)), "retired_setups": [],
            "round_levels_enabled": True, "event_half_life": dict(cfg.get("events", {}).get("half_life_defaults", {}))}


def build_context(cfg_path: Path, asof: Optional[pd.Timestamp], run_type: str, demo: bool) -> Ctx:
    cfg = load_config(cfg_path)
    base = cfg_path.parent
    ledger_path = base / cfg["paths"]["ledger"]
    ledger = parse_ledger(merge_scorecard(load_yaml(ledger_path), base / cfg["paths"].get("cycle_scorecard", "ledger/cycle_scorecard.yaml")))
    state = base / cfg["paths"]["state_dir"]
    if demo:
        state = state / "demo"
    j = Journal(state)
    params = j.params(default_params(cfg))
    now = pd.Timestamp.now(tz="UTC")
    if asof is None:
        asof_date = last_completed_session(now)      # never treat today's unfinished bar as complete
    else:
        asof_date = (asof.tz_localize(None) if asof.tzinfo else asof).normalize()
    scfg = {}
    sc_path = base / cfg["paths"].get("scanner_config", "scanner_config.yaml")
    if sc_path.exists():
        scfg = load_yaml(sc_path)
    return Ctx(cfg, base, asof_date, now.strftime("%Y%m%dT%H%MZ"), run_type, demo, build_instruments(cfg), ledger,
               ledger_path, j, params, scanner_cfg=scfg)


# =============================================================================
# Step 0: data
# =============================================================================

def load_data(ctx: Ctx) -> dict:
    loader = DataLoader(ctx.cfg, ctx.base, demo=ctx.demo, asof=ctx.asof, scanner_cfg=ctx.scanner_cfg)
    status = {}
    try:
        for sym, inst in ctx.insts.items():
            s = loader.load(inst)
            if s.bars.empty and inst.is_fx and inst.base != "USD" and inst.quote != "USD":
                ctx.notes.append(f"{sym}: no feed, will be built from its dollar legs")
            ctx.series[sym] = s
            status[sym] = {"rows": len(s.bars), "years": round(s.years, 1), "notes": s.notes,
                           "last": None if s.bars.empty else str(s.bars.index[-1].date())}
    finally:
        loader.close()
    status["_sources"] = loader.status
    # solo currency indices
    usd_pairs = {k: v.close for k, v in ctx.series.items() if ctx.insts[k].is_fx and "USD" in (ctx.insts[k].base, ctx.insts[k].quote)
                 and not v.bars.empty}
    logv = currency_log_values(usd_pairs, MAJORS)
    ctx.logv = logv  # type: ignore[attr-defined]
    if logv.shape[1] == len(MAJORS):
        c = solo_indices(logv)
        for ccy in MAJORS:
            sym = f"{ccy}_IDX"
            lvl = np.exp(c[ccy].fillna(0).cumsum()) * 100
            df = clean_daily(pd.DataFrame({"close": lvl}), "solo index", close_only=True)
            ctx.series[sym] = Series(sym, df, [f"{ccy} against the other {len(MAJORS) - 1} majors (equal weight)"])
            ctx.insts[sym] = Inst(sym, "solo", base=ccy, profile=True, commodity=_ccy_commodity(ctx.cfg, ccy),
                                  label=f"{ccy} solo index")
        ctx.solo = c  # type: ignore[attr-defined]
    else:
        ctx.notes.append("solo currency indices unavailable: need all seven dollar pairs")
        ctx.solo = pd.DataFrame()  # type: ignore[attr-defined]
    for sym, inst in list(ctx.insts.items()):
        s = ctx.series.get(sym)
        if inst.is_fx and (s is None or s.bars.empty) and inst.base in logv and inst.quote in logv:
            df = clean_daily(pd.DataFrame({"close": cross_from_legs(logv, inst.base, inst.quote)}), "synthetic", close_only=True)
            ctx.series[sym] = Series(sym, df, ["synthetic from dollar legs (closes only)"])
    for sym, s in ctx.series.items():
        if not s.bars.empty:
            ctx.rets[sym] = returns(s, ctx.insts[sym].additive)
    return status


def _ccy_commodity(cfg: dict, ccy: str) -> Optional[str]:
    return (cfg.get("currency_commodity") or {}).get(ccy)


# =============================================================================
# Step 1-2: regime and scoreboard
# =============================================================================

def regime_step(ctx: Ctx) -> dict:
    rc = ctx.cfg.get("regimes", {})
    dollar = ctx.series.get(rc.get("dollar", "DXY"))
    if dollar is None or dollar.bars.empty:
        dollar = ctx.series.get("USD_IDX")
    y2, vix = ctx.series.get(rc.get("rate", "US02Y")), ctx.series.get(rc.get("fear", "VIX"))
    if any(x is None or x.bars.empty for x in (dollar, y2, vix)):
        ctx.regimes = pd.Series(dtype=object)  # type: ignore[attr-defined]
        return {"regime": None, "why": "missing dollar, 2y yield or VIX history"}
    fr = regime_frame(dollar.close, y2.close, vix.close, rc)
    ctx.regimes = fr["regime"]  # type: ignore[attr-defined]
    last = fr.dropna(subset=["regime"])
    if last.empty:
        return {"regime": None, "why": "not enough history for the regime axes"}
    now = last.iloc[-1]
    prev = last["regime"].iloc[-2] if len(last) > 1 else None
    since = last.index[-1]
    for t, v in last["regime"].iloc[::-1].items():
        if v != now["regime"]:
            break
        since = t
    hist = ctx.journal.read("regime_history.csv")
    if hist.empty or hist["regime"].iloc[-1] != now["regime"]:
        ctx.journal.append("regime_history.csv", [{"date": str(ctx.asof.date()), "regime": now["regime"],
                                                   "since": str(since.date()), "previous": prev or ""}])
    counts = last["regime"].iloc[-HIST_DAYS:].value_counts(normalize=True).round(3).to_dict()
    return {"regime": now["regime"], "text": REGIME_TEXT.get(now["regime"], ""), "since": str(since.date()),
            "changed_today": prev is not None and prev != now["regime"], "liquidity": now["liquidity"],
            "liquidity_mixed": bool(now["liquidity_mixed"]), "risk": now["risk"],
            "vix": round(float(now["vix"]), 2), "vix_median_5y": round(float(now["vix_median"]), 2),
            "dollar_change_6m": round(float(now["dollar_change"]), 4), "y2_change_6m": round(float(now["y2_change"]), 3),
            "share_20y": counts}


def scoreboard_step(ctx: Ctx) -> dict:
    out = {}
    for sym in ("DXY", "USD_IDX"):
        s = ctx.series.get(sym)
        if s is None or s.bars.empty or len(s.close) < 25:
            out[sym] = None
            continue
        c = s.close
        out[sym] = {"chg_20d": round(float(np.log(c.iloc[-1] / c.iloc[-21])), 4), "dir": int(np.sign(c.iloc[-1] - c.iloc[-21]))}
    a, b = out.get("DXY"), out.get("USD_IDX")
    out["agree"] = None if not a or not b else a["dir"] == b["dir"]
    out["reading"] = ("unknown" if out["agree"] is None else "agree: a dollar story" if out["agree"]
                      else "disagree: the dollar index is telling a euro story, not a dollar story")
    return out


# =============================================================================
# Step 3: calendar
# =============================================================================

def calendar_archive(ctx: Ctx) -> pd.DataFrame:
    try:
        import news
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    snap = ctx.base / (ctx.scanner_cfg.get("paths", {}).get("data_dir", "data")) / "calendar_snapshots"
    try:
        df = news.load_archive(snap)
    except Exception as exc:  # noqa: BLE001
        ctx.notes.append(f"calendar archive unreadable: {exc}")
        return pd.DataFrame()
    return df


def event_days_by_ccy(ctx: Ctx, archive: pd.DataFrame, owner: pd.DataFrame) -> dict[str, pd.DatetimeIndex]:
    from .data import trade_dates
    out: dict[str, set] = {}
    if not archive.empty:
        red = archive[archive["impact"] == "High"]
        for ccy, g in red.groupby("currency"):
            out.setdefault(ccy, set()).update(trade_dates(g["event_time"]).tolist())
    if not owner.empty:
        for _, e in owner.iterrows():
            if e.get("currency"):
                out.setdefault(e["currency"], set()).add(e["date"])
    if ctx.cfg.get("traits", {}).get("nfp_first_friday_rule", True):
        # [IMPL] approximation until the calendar archive holds enough history: NFP on the first Friday
        start = ctx.asof - pd.DateOffset(years=21)
        fridays = pd.date_range(start, ctx.asof, freq="WOM-1FRI")
        out.setdefault("USD", set()).update(fridays.tolist())
    return {k: pd.DatetimeIndex(sorted(v)) for k, v in out.items()}


def window_view(ctx: Ctx, archive: pd.DataFrame, inst: Inst) -> dict:
    wc = ctx.cfg.get("windows", {})
    now = pd.Timestamp(ctx.asof).tz_localize("UTC") + pd.Timedelta(hours=22)
    ccys = inst.currencies if inst.kind != "solo" else [inst.base]
    reasons = []
    inside = []
    if not archive.empty:
        red = archive[(archive["impact"] == "High") & archive["currency"].isin(ccys)]
        soon = red[(red["event_time"] > now) & (red["event_time"] <= now + pd.Timedelta(days=int(wc.get("scheduled_days", 5))))]
        inside = red[(red["event_time"] > now) & (red["event_time"] <= now + pd.Timedelta(hours=float(wc.get("stop_hours", 24))))]
        if not soon.empty:
            reasons.append("scheduled: " + ", ".join(f"{r.currency} {r.title} {r.event_time:%a %d %b %H:%M}Z" for r in soon.head(4).itertuples()))
    bd = pd.bdate_range(ctx.asof, ctx.asof + pd.Timedelta(days=7))[:4]
    if any(d.month != (d + pd.offsets.BDay(1)).month for d in bd):
        reasons.append("month end rebalancing" + (" (quarter end)" if any(d.month in (3, 6, 9, 12) and d.month != (d + pd.offsets.BDay(1)).month for d in bd) else ""))
    for fw in ctx.ledger.raw.get("forced_windows") or []:
        try:
            d = pd.Timestamp(fw["date"])
        except Exception:  # noqa: BLE001
            continue
        if set(fw.get("currencies", ["USD"])) & set(ccys) and 0 <= (d - ctx.asof).days <= int(wc.get("forced_days", 10)):
            reasons.append(f"forced: {fw.get('label')} {d.date()}")
    inside_txt = "" if len(inside) == 0 else "; ".join(f"{r.currency} {r.title} {r.event_time:%a %H:%M}Z" for r in inside.itertuples())
    return {"open": bool(reasons), "event_inside_stop": bool(inside_txt), "why": "; ".join(reasons) or "no push window open",
            "inside_stop": inside_txt}


# =============================================================================
# Step 4: events and pressure
# =============================================================================

def events_step(ctx: Ctx, archive: pd.DataFrame) -> dict:
    ecfg = ctx.cfg.get("events", {})
    ev_path = ctx.base / ctx.cfg["paths"]["events"]
    owner = load_owner_events((load_yaml(ev_path) or {}).get("events", []) if ev_path.exists() else [])
    cal = load_calendar_events(archive[archive["impact"] == "High"] if not archive.empty else archive,
                               bool(ecfg.get("auto_confirm_scheduled", True)))
    events = pd.concat([x for x in (owner, cal) if not x.empty], ignore_index=True) if (not owner.empty or not cal.empty) else owner
    if not events.empty:
        events = events[events["date"] <= ctx.asof].sort_values("date").reset_index(drop=True)
    ctx.owner_events = owner  # type: ignore[attr-defined]
    basket = list(ecfg.get("basket", []))
    templates = dict(ecfg.get("templates", {}))
    F = _factor_frame(ctx)
    cache_p = ctx.journal.path("events_measured.json")
    cache = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
    lookback = ctx.asof - pd.Timedelta(days=int(ecfg.get("lookback_days", 730)))
    profiled = [s for s, i in ctx.insts.items() if i.profile and s in ctx.rets]
    measured_all = {}
    for _, ev in events.iterrows():
        if ev["date"] < lookback:
            continue
        key = ev["id"]
        c = cache.get(key)
        if c and c.get("complete") and c.get("engine") == ENGINE_VERSION:
            measured_all[key] = c
            continue
        shocks, pers, pre, complete = {}, {}, {}, True
        for sym in sorted(set(profiled) | set(basket)):
            if sym not in ctx.rets:
                continue
            Fx = F.drop(columns=[c for c in F.columns if c == sym], errors="ignore")
            es = event_study(ctx.rets[sym], Fx, ev["date"], ecfg)
            if es is None:
                continue
            shocks[sym] = es["shock"]
            pers[sym] = es["persistence"]
            pre[sym] = es["pre_drift"]
            complete = complete and es["complete"]
        if not shocks:
            continue
        fp = fingerprint(shocks, basket, templates)
        smax_on = max(shocks, key=lambda k: abs(shocks[k]))
        vix = ctx.series.get("VIX")
        vshock = None
        if vix is not None and not vix.bars.empty:
            v = vix.close
            i0 = v.index.searchsorted(ev["date"])
            if 1 <= i0 < len(v) - 1:
                vshock = round(float(v.iloc[i0 + 1] - v.iloc[i0 - 1]), 2)
        m = {"shocks": shocks, "persistence": pers, "pre_drift": pre, "shock_max": shocks[smax_on], "shock_max_on": smax_on,
             "vol_shock": vshock, "best": fp["best"], "best_sim": fp["best_sim"], "fingerprint": fp, "complete": complete,
             "engine": ENGINE_VERSION, "date": str(ev["date"].date()), "type": ev["type"]}
        cache[key] = m
        measured_all[key] = m
    cache_p.write_text(json.dumps(cache, default=str), encoding="utf-8")
    # library by type: median shock and persistence -> pressure half lives
    lib = _event_library(events, measured_all)
    ctx.event_library = lib  # type: ignore[attr-defined]
    # needle test, weights, exposures
    alignment_now = alignment_scores(ctx.ledger, events if not events.empty else pd.DataFrame(columns=["date", "plan_effects"]), ctx.asof)
    lever_scores = []
    from .players import leverage_score
    for lv in ctx.ledger.levers:
        s = leverage_score(lv)
        if s is not None:
            lever_scores.append(s / 5.0)
    top_q = float(np.percentile(lever_scores, 75)) if len(lever_scores) >= 4 else float(ecfg.get("lever_top_quartile_default", 0.75))
    regimes = getattr(ctx, "regimes", pd.Series(dtype=object))
    contribs: dict[str, list] = {}
    rows = []
    hl_defaults = ctx.p("event_half_life", {}) or {}
    for _, ev in events.iterrows():
        m = measured_all.get(ev["id"])
        if m is None:
            continue
        evd = ev.to_dict()
        reg_change = False
        if not regimes.empty:
            w = regimes[(regimes.index >= ev["date"] - pd.Timedelta(days=7)) & (regimes.index <= ev["date"] + pd.Timedelta(days=7))].dropna()
            reg_change = w.nunique() > 1
        flip = _alignment_flip(ctx, events, ev)
        nd = needle_test(evd, m, ctx.ledger, {}, flip, reg_change, top_q, ecfg.get("needle", {}))
        wt = event_weight(evd, m, ctx.ledger, nd)
        ok, why = counts_for_pressure(evd, nd, wt)
        own_h = ev.get("half_life_days")
        if own_h is not None and not pd.isna(own_h):
            h = float(own_h)
        else:   # refit default for the type, then the library's measured value, then the config default
            h = float(hl_defaults.get(ev["type"], (lib.get(ev["type"]) or {}).get("half_life", ecfg.get("half_life_default", 10))))
        exposures = {}
        for sym in profiled:
            if ev["exposures"] and sym in ev["exposures"]:
                exposures[sym] = float(ev["exposures"][sym])
            elif sym in m["shocks"] and abs(m["shocks"][sym]) >= float(ecfg.get("exposure_min_shock", 1.0)):
                exposures[sym] = float(np.clip(m["shocks"][sym] / 3.0, -1, 1))
        if ok:
            for sym, x in exposures.items():
                contribs.setdefault(sym, []).append({"id": ev["id"], "date": ev["date"], "E": wt["E"], "x": x, "h": h,
                                                     "headline": ev["headline"]})
        rows.append({"id": ev["id"], "date": str(ev["date"].date()), "headline": ev["headline"], "type": ev["type"],
                     "tier": int(ev["tier"]), "auto": bool(ev["auto"]), "player": ev["player"], "lever": ev["lever"],
                     "shock_max": m["shock_max"], "shock_on": m["shock_max_on"], "fingerprint": m["best"],
                     "fingerprint_sim": m["best_sim"], "needle": nd, "weight": wt, "counts": ok, "why_not": why,
                     "half_life": round(h, 1), "exposures": {k: round(v, 2) for k, v in exposures.items()},
                     "verdict": ev["verdict"], "persistence": _median(m["persistence"]), "pre_drift": _median(m["pre_drift"])})
    # owner events without answers to the qualifying questions
    pending_q = [{"id": e["id"], "headline": e["headline"]} for _, e in owner.iterrows()
                 if e["verdict"] == "unanswered"] if not owner.empty else []
    return {"events": rows, "contribs": contribs, "alignment": alignment_now, "library": lib, "pending_questions": pending_q,
            "questions": QUALIFYING_QUESTIONS, "n_owner": int(len(owner)), "n_calendar": int(len(cal))}


def _median(d: dict) -> Optional[float]:
    v = [x for x in (d or {}).values() if x is not None and np.isfinite(x)]
    return None if not v else round(float(np.median(v)), 2)


def _event_library(events: pd.DataFrame, measured: dict) -> dict:
    by: dict[str, dict] = {}
    for _, ev in events.iterrows():
        m = measured.get(ev["id"])
        if not m:
            continue
        b = by.setdefault(ev["type"], {"n": 0, "shock": [], "pers": []})
        b["n"] += 1
        b["shock"].append(abs(m["shock_max"]))
        p = _median(m["persistence"])
        if p is not None:
            b["pers"].append(p)
    out = {}
    for t, b in by.items():
        pers = float(np.median(b["pers"])) if b["pers"] else None
        out[t] = {"n": b["n"], "median_shock": round(float(np.median(b["shock"])), 2),
                  "median_persistence": None if pers is None else round(pers, 2),
                  "half_life": round(persistence_half_life(pers, 10.0), 1)}
    return out


def _alignment_flip(ctx: Ctx, events: pd.DataFrame, ev: pd.Series) -> bool:
    eff = ev.get("plan_effects") or {}
    if not eff:
        return False
    before = events[(events["date"] < ev["date"])]
    upto = events[(events["date"] <= ev["date"])]
    a0 = alignment_scores(ctx.ledger, before, ev["date"] - pd.Timedelta(days=1))
    a1 = alignment_scores(ctx.ledger, upto, ev["date"])
    for g in eff:
        x, y = (a0.get(g) or {}).get("A"), (a1.get(g) or {}).get("A")
        if x is not None and y is not None and np.sign(x) != np.sign(y) and y != 0:
            return True
    return False


def _factor_frame(ctx: Ctx) -> pd.DataFrame:
    fc = ctx.cfg.get("events", {}).get("factors", ["SPX", "US02Y", "VIX"])
    cols = {f: ctx.rets[f] for f in fc if f in ctx.rets}
    return pd.DataFrame(cols)


# =============================================================================
# Step 5-7: character, gap, map (per instrument)
# =============================================================================

def drivers_for(ctx: Ctx, inst: Inst) -> pd.DataFrame:
    dc = ctx.cfg.get("drivers_map", {})
    cols: dict[str, pd.Series] = {}
    fear = ctx.rets.get(dc.get("fear", "VIX"))
    if fear is not None and inst.symbol != dc.get("fear", "VIX"):
        cols["fear"] = fear
    rate = _rate_change(ctx, inst)
    if rate is not None:
        cols["rate"] = rate
    com = inst.commodity
    if inst.is_fx and not com:
        cb, cq = _ccy_commodity(ctx.cfg, inst.base), _ccy_commodity(ctx.cfg, inst.quote)
        com = cb or cq
    if com and com in ctx.rets and com != inst.symbol:
        cols["resource"] = ctx.rets[com]
    return pd.DataFrame(cols)


def _yield(ctx: Ctx, ccy: str) -> Optional[pd.Series]:
    sym = (ctx.cfg.get("rates") or {}).get(ccy)
    s = ctx.series.get(sym) if sym else None
    return None if s is None or s.bars.empty else s.close


def _rate_change(ctx: Ctx, inst: Inst) -> Optional[pd.Series]:
    if inst.kind in ("yield", "vol", "spread"):
        return None
    if inst.is_fx:
        a, b = _yield(ctx, inst.base), _yield(ctx, inst.quote)
        if a is None or b is None:
            return None
        return (a - b).diff()
    if inst.kind == "solo":
        own = _yield(ctx, inst.base)
        others = [y for c in MAJORS if c != inst.base and (y := _yield(ctx, c)) is not None]
        if own is None or not others:
            return None
        return (own - pd.concat(others, axis=1).mean(axis=1)).diff()
    us = _yield(ctx, "USD")
    return None if us is None else us.diff()


def character_cause(ctx: Ctx, sym: str, contribs: dict, flips: dict, regime: dict) -> Optional[str]:
    causes = []
    for c in contribs.get(sym, []):
        if (ctx.asof - pd.Timestamp(c["date"])).days <= 3 * c["h"] * 1.4:
            causes.append(f"event {c['headline']}")
    for k, f in flips.items():
        if sym in k and f.get("flag"):
            causes.append(f"relationship flip {k}")
    if regime.get("changed_today") or (regime.get("since") and (ctx.asof - pd.Timestamp(regime["since"])).days <= 20):
        causes.append(f"regime change to {regime.get('regime')}")
    return "; ".join(causes[:3]) or None


def cot_view(ctx: Ctx, inst: Inst, cot: dict) -> dict:
    """Weekly positioning: net share, 3 year positioning index, crowding, price and open interest reading."""
    if not cot:
        return {"status": "COT unavailable"}
    flip = 1
    mkt = None
    if inst.is_fx:
        if inst.quote == "USD":
            mkt = inst.base
        elif inst.base == "USD":
            mkt, flip = inst.quote, -1
    elif inst.kind == "solo" and inst.base != "USD":
        mkt = inst.base
    else:
        mkt = inst.cot
    s = cot.get(mkt) if mkt else None
    if s is None:
        return {"status": "no COT market"}
    f = s.frame[pd.to_datetime(s.frame["publication_ts"], utc=True) <= pd.Timestamp(ctx.asof).tz_localize("UTC") + pd.Timedelta(hours=23)]
    if len(f) < 10:
        return {"status": "too little COT history"}
    weeks = int(ctx.cfg.get("positioning", {}).get("index_weeks", 156))
    w = f.iloc[-weeks:]
    net = w["net"] * flip
    lo, hi = net.min(), net.max()
    pi = None if hi == lo else float(100 * (net.iloc[-1] - lo) / (hi - lo))
    share = float(w["net"].iloc[-1] / w["open_interest"].iloc[-1]) * flip if w["open_interest"].iloc[-1] else None
    crowd = None if pi is None else ("crowded long" if pi > 90 else "crowded short" if pi < 10 else "")
    reading = ""
    sb = ctx.series.get(inst.symbol)
    if sb is not None and len(f) >= 2 and not sb.bars.empty:
        d1, d0 = pd.Timestamp(f["report_date"].iloc[-1]), pd.Timestamp(f["report_date"].iloc[-2])
        c = sb.close
        try:
            p1, p0 = c.asof(d1), c.asof(d0)
            oi1, oi0 = f["open_interest"].iloc[-1], f["open_interest"].iloc[-2]
            pu, ou = p1 > p0, oi1 > oi0
            reading = {(True, True): "new buying: fresh commitment", (True, False): "short covering: may run out",
                       (False, True): "new selling: fresh commitment", (False, False): "long liquidation: may exhaust"}[(pu, ou)]
        except Exception:  # noqa: BLE001
            reading = ""
    age = (ctx.asof - pd.Timestamp(f["report_date"].iloc[-1])).days
    return {"status": "ok", "market": mkt, "index": None if pi is None else round(pi, 1), "net_share": None if share is None else round(share, 3),
            "crowding": crowd, "price_oi": reading, "report_date": str(pd.Timestamp(f["report_date"].iloc[-1]).date()),
            "age_days": age, "weekly_change": round(float((f["net"].iloc[-1] - f["net"].iloc[-2]) * flip), 0)}


def load_cot(ctx: Ctx) -> dict:
    if ctx.demo or not ctx.scanner_cfg.get("cot"):
        return {}
    try:
        import copy

        import scanner
        sc = copy.deepcopy(ctx.scanner_cfg)
        sc["cot"]["fetch_weeks"] = max(int(sc["cot"].get("fetch_weeks", 70)), int(ctx.cfg.get("positioning", {}).get("index_weeks", 156)) + 10)
        return scanner.CotSource(sc, ctx.base).load()
    except Exception as exc:  # noqa: BLE001
        ctx.notes.append(f"COT unavailable: {exc}")
        return {}


def personality_changes(ctx: Ctx, sym: str, drift: pd.DataFrame, cause: Optional[str]) -> list[dict]:
    """Drift beyond +/-2 for more than 60 days is a personality change. One instrument has one personality, so
    it has at most one open change: a single row whose trait column is the mix of traits drifting together
    (gold: energy, energy_long, atr_pct). The row is rewritten when the mix changes and closed when every
    trait in it is back inside the band. Older per-trait rows for the instrument are folded into it."""
    th = float(ctx.cfg["traits"].get("drift_threshold", 2.0))
    need = int(ctx.cfg["traits"].get("drift_days", 60))
    today = str(ctx.asof.date())
    unknown = "not identified: investigate"
    log = ctx.journal.read("personality_changes.csv")
    mask = ((log["symbol"] == sym) & (log["status"] == "open")) if not log.empty else pd.Series(dtype=bool)
    prior = [t.strip() for x in (log.loc[mask, "trait"] if mask.any() else []) for t in str(x).split(",") if t.strip()]
    streaks = {t: drift_streak(drift[t], th) for t in drift.columns}
    # a trait stays in the mix until its drift is back inside; a new one joins once it passes 60 days
    mix = [t for t in drift.columns if streaks[t][0] > need or (t in prior and streaks[t][0] > 0)]
    if not mix and not prior:
        return []
    if mix:
        last = {t: float(drift[t].dropna().iloc[-1]) for t in mix}
        dirs = {"higher" if streaks[t][1] > 0 else "lower" for t in mix}
        row = {"date": str(log.loc[mask, "date"].min()) if prior else today, "symbol": sym, "trait": ", ".join(mix),
               "direction": dirs.pop() if len(dirs) == 1 else "mixed: " + ", ".join(
                   f"{t} {'higher' if streaks[t][1] > 0 else 'lower'}" for t in mix),
               "drift": round(max(last.values(), key=abs), 2), "days": max(streaks[t][0] for t in mix), "status": "open",
               "suspected_cause": next((c for c in ([str(x) for x in log.loc[mask, "suspected_cause"]] if prior else [])
                                        if c and c != unknown), None) or cause or unknown, "closed": ""}
    else:
        row = {"date": str(log.loc[mask, "date"].min()), "symbol": sym, "trait": ", ".join(prior), "direction": "",
               "drift": "", "days": 0, "status": "closed", "suspected_cause": "drift back inside the band", "closed": today}
    if prior and row["trait"] == ", ".join(prior) and row["status"] == "open" and mask.sum() == 1:
        return []  # same mix still open: nothing new to log today
    if prior:
        log = log[~mask]
    log = pd.concat([log, pd.DataFrame([row])], ignore_index=True) if not log.empty else pd.DataFrame([row])
    ctx.journal.rewrite("personality_changes.csv", log)
    return [row]


def flips_step(ctx: Ctx) -> dict:
    fc = ctx.cfg.get("flips", {})
    out = {}
    log = ctx.journal.read("flips.csv")
    new_rows = []
    for a, b in fc.get("relationships", []):
        if a not in ctx.rets or b not in ctx.rets:
            out[f"{a}~{b}"] = {"status": "missing data", "flag": False}
            continue
        st = relationship_status(ctx.rets[a], ctx.rets[b], int(fc.get("window", 60)), int(fc.get("median_days", 1260)),
                                 int(fc.get("hold_days", 20)))
        key = f"{a}~{b}"
        st.pop("history", None)
        out[key] = st
        was = (not log.empty) and ((log["pair"] == key) & (log["status"] == "open")).any()
        if st["flag"] and not was:
            new_rows.append({"date": str(ctx.asof.date()), "pair": key, "corr": st["corr"], "median_5y": st["median"],
                             "status": "open", "closed": ""})
        elif not st["flag"] and was:
            log.loc[(log["pair"] == key) & (log["status"] == "open"), ["status", "closed"]] = ["closed", str(ctx.asof.date())]
            ctx.journal.rewrite("flips.csv", log)
    ctx.journal.append("flips.csv", new_rows)
    return out


def instrument_view(ctx: Ctx, sym: str, ev: dict, flips: dict, regime: dict, archive: pd.DataFrame, cot: dict,
                    ev_days: dict) -> Optional[dict]:
    inst = ctx.insts[sym]
    s = ctx.series.get(sym)
    r = ctx.rets.get(sym)
    if s is None or s.bars.empty or r is None or len(r.dropna()) < 300:
        return {"symbol": sym, "label": inst.label, "error": "not enough history", "notes": [] if s is None else s.notes}
    tcfg = ctx.cfg["traits"]
    drv = drivers_for(ctx, inst)
    days = None
    for c in (inst.currencies if inst.kind != "solo" else [inst.base]):
        d = ev_days.get(c)
        if d is not None:
            days = d if days is None else days.union(d)
    bars = None if inst.kind == "solo" else s.bars
    traits = compute_traits(r, bars, s.close, inst.additive, drv, days, tcfg)
    bands = trait_bands(traits, HIST_DAYS, tcfg.get("bands", {}))
    dist = character_distance(traits, DISTANCE_TRAITS, HIST_DAYS, float(tcfg.get("distance_flag_pct", 95)))
    dist_hist = dist.pop("history", None)
    drift = ewma_drift(traits, int(tcfg.get("drift_half_life", 60)), HIST_DAYS)
    regimes = getattr(ctx, "regimes", pd.Series(dtype=object))
    faces = regime_faces(traits, regimes) if not regimes.empty else {}
    # [Doc: one number for breaking character] the band comes from the single distance D, so linked traits
    # are not double counted: below the 75th percentile of its own history core, 75-95 stretched, top 5% out.
    dpct = dist.get("pct")
    if dpct is not None:
        overall = ("out of character" if dpct >= float(tcfg.get("distance_flag_pct", 95)) else
                   "stretched" if dpct >= float(tcfg.get("distance_stretched_pct", 75)) else "core")
    else:
        overall = bands["energy"]["band"]
    cause = character_cause(ctx, sym, ev["contribs"], flips, regime) if overall == "out of character" else None
    changes = personality_changes(ctx, sym, drift, cause)
    price = float(s.close.iloc[-1])
    sigma = bands["energy"]["value"]
    atr = atr_series(s.bars, 20) if inst.kind != "solo" else (r.abs().rolling(20).mean() * (1 if inst.additive else price))
    rng = {}
    if sigma:
        for h in (5, 20):
            lo1, hi1 = vol_range_band(price, sigma, h, 1, inst.additive)
            lo2, hi2 = vol_range_band(price, sigma, h, 2, inst.additive)
            mb = measured_range_band(r, regimes if not regimes.empty else None, regime.get("regime"), price, h, inst.additive)
            rng[str(h)] = {"normal": [lo1, hi1], "stress": [lo2, hi2], "measured": mb}
    beyond_stress = False
    if sigma and len(s.close) > 2:
        prev = float(s.close.iloc[-2])
        lo2, hi2 = vol_range_band(prev, sigma, 1, 2, inst.additive)
        beyond_stress = not (lo2 <= price <= hi2)
    # pressure now and its own history
    contribs = ev["contribs"].get(sym, [])
    hist_idx = s.close.index[-int(ctx.cfg.get("events", {}).get("pressure_history_days", 756)):]
    pser = pressure_series(contribs, hist_idx) if contribs else pd.Series(0.0, index=hist_idx)
    p_now = float(pser.iloc[-1]) if len(pser) else 0.0
    p_pct = float((pser.abs() < abs(p_now)).mean() * 100) if contribs and abs(p_now) > 0 else 0.0
    active = [c for c in contribs if abs(c["E"] * c["x"] * 2 ** (-max(np.busday_count(pd.Timestamp(c["date"]).date(), ctx.asof.date()), 0) / c["h"])) > 1e-4]
    view: dict[str, Any] = {
        "symbol": sym, "label": inst.label, "kind": inst.kind, "price": price, "last_date": str(s.close.index[-1].date()),
        "years": round(s.years, 1), "notes": s.notes, "bands": bands, "distance": dist, "overall_band": overall,
        "cause": cause, "beyond_stress_range": beyond_stress, "range_bands": rng, "faces": faces,
        "regime_face": faces.get(regime.get("regime") or "", {}), "personality_changes": changes,
        "drift_now": {k: (None if pd.isna(v) else round(float(v), 2)) for k, v in drift.iloc[-1].items()} if len(drift) else {},
        "pressure": round(p_now, 4), "pressure_pct": round(p_pct, 1), "active_events": [
            {"id": c["id"], "headline": c["headline"], "x": round(c["x"], 2), "E": c["E"]} for c in active],
        "usd_sign": usd_sign(inst) if inst.is_fx else _beta_sign(ctx, sym), "cot": cot_view(ctx, inst, cot),
    }
    if dist_hist is not None:
        view["distance_series_tail"] = [round(float(x), 2) for x in dist_hist.iloc[-20:]]
    ctx.traits_cache[sym] = traits  # type: ignore[attr-defined]
    # liquidity cost
    if s.spread is not None and atr is not None and len(atr.dropna()):
        a20 = float(atr.dropna().iloc[-1])
        view["liquidity_cost"] = round(s.spread / a20, 4) if a20 > 0 else None
        ctx.journal.append("liquidity.csv", [{"date": str(ctx.asof.date()), "symbol": sym, "spread": s.spread,
                                              "atr20": a20, "cost": view["liquidity_cost"]}])
    # pair math
    if inst.is_fx and not getattr(ctx, "solo", pd.DataFrame()).empty and inst.base in ctx.solo and inst.quote in ctx.solo:
        view["pair"] = pair_view(ctx, inst, r, traits)
    # level map and likelihood
    if inst.grid:
        view.update(map_and_story(ctx, inst, s, r, atr, traits, bands, view, regime, archive))
    view["window"] = window_view(ctx, archive, inst)
    return view


def _beta_sign(ctx: Ctx, sym: str) -> Optional[int]:
    d = ctx.rets.get("DXY")
    r = ctx.rets.get(sym)
    if d is None or r is None:
        return None
    j = pd.concat([r, d], axis=1, join="inner").dropna().iloc[-250:]
    if len(j) < 100:
        return None
    b = j.cov().iloc[0, 1] / j.iloc[:, 1].var()
    return int(np.sign(b)) if abs(b) > 0.1 else 0


def pair_view(ctx: Ctx, inst: Inst, r: pd.Series, traits: pd.DataFrame) -> dict:
    N = len(MAJORS)
    c = ctx.solo
    w = int(ctx.cfg["traits"].get("long_window", 250))
    D_A = dominance(c[inst.base], r, N, w)
    out: dict[str, Any] = {"dominance_base": None if D_A is None else round(D_A, 3)}
    if D_A is not None:
        lead = inst.base if D_A >= 0.65 else inst.quote if D_A <= 0.35 else None
        out["driver"] = f"{lead} runs the pair: read {lead}'s players first" if lead else "shared: both sides matter"
    # predicted versus measured betas (a data pipeline check)
    tA, tB = ctx.traits_cache.get(f"{inst.base}_IDX"), ctx.traits_cache.get(f"{inst.quote}_IDX")
    checks = {}
    for b in ("fear_beta", "resource_beta"):
        pa = None if tA is None or tA[b].dropna().empty else float(tA[b].dropna().iloc[-1])
        pb = None if tB is None or tB[b].dropna().empty else float(tB[b].dropna().iloc[-1])
        pred = predicted_pair_beta(pa, pb, N)
        meas = None if traits[b].dropna().empty else float(traits[b].dropna().iloc[-1])
        checks[b] = {"predicted": None if pred is None else round(pred, 5), "measured": None if meas is None else round(meas, 5)}
    out["beta_check"] = checks
    j = c[[inst.base, inst.quote]].dropna().iloc[-w:]
    if len(j) > 100:
        sa, sb = j[inst.base].std() * math.sqrt(252), j[inst.quote].std() * math.sqrt(252)
        rho = j.corr().iloc[0, 1]
        out["energy_predicted"] = round(predicted_pair_energy(sa, sb, rho, N), 4)
        out["rho"] = round(float(rho), 3)
    yA, yB = _yield(ctx, inst.base), _yield(ctx, inst.quote)
    sig = None if traits["energy_long"].dropna().empty else float(traits["energy_long"].dropna().iloc[-1])
    out["carry_appeal"] = carry_appeal(None if yA is None else float(yA.iloc[-1]), None if yB is None else float(yB.iloc[-1]), sig)
    if out["carry_appeal"] is not None:
        out["carry_appeal"] = round(out["carry_appeal"], 2)
    drv = {}
    if yA is not None and yB is not None:
        drv["yield_gap"] = yA - yB
    ca, cb = _ccy_commodity(ctx.cfg, inst.base), _ccy_commodity(ctx.cfg, inst.quote)
    tot = None
    for cc, sg in ((ca, 1), (cb, -1)):
        if cc and cc in ctx.series and not ctx.series[cc].bars.empty:
            x = np.log(ctx.series[cc].close) * sg
            tot = x if tot is None else tot.add(x, fill_value=0)
    if tot is not None:
        drv["terms_of_trade"] = tot
    vix = ctx.series.get("VIX")
    if vix is not None and not vix.bars.empty:
        drv["risk"] = np.log(vix.close)
    s = ctx.series[inst.symbol]
    gap = incentive_gap(np.log(s.close), pd.DataFrame(drv), int(ctx.cfg.get("pairs", {}).get("gap_window", 630)),
                        history_days=1)
    G_hist = gap.pop("history", None)
    ctx.gap_hist[inst.symbol] = G_hist  # type: ignore[attr-defined]
    out["gap"] = gap
    return out


def _zones_near(lm, price: float) -> tuple[Optional[Zone], Optional[Zone], Optional[Zone]]:
    """Zone containing price (if any), nearest zone fully above, nearest fully below."""
    inside = None
    above = below = None
    for z in lm.zones:
        lo, hi = z.level - lm.half_width_now, z.level + lm.half_width_now
        if lo <= price <= hi:
            if inside is None or abs(z.level - price) < abs(inside.level - price):
                inside = z
        elif lo > price and (above is None or z.level < above.level):
            above = z
        elif hi < price and (below is None or z.level > below.level):
            below = z
    return inside, above, below


def _next_beyond(lm, z: Zone, direction: int) -> Optional[Zone]:
    cands = [x for x in lm.zones if (x.level - z.level) * direction > 2 * lm.half_width_now]
    if not cands:
        return None
    return min(cands, key=lambda x: abs(x.level - z.level))


def map_and_story(ctx: Ctx, inst: Inst, s: Series, r: pd.Series, atr: pd.Series, traits: pd.DataFrame, bands: dict,
                  view: dict, regime: dict, archive: pd.DataFrame) -> dict:
    lcfg = dict(ctx.cfg["levels"])
    grid = ctx.cfg["grids"][inst.grid]
    regimes = getattr(ctx, "regimes", pd.Series(dtype=object))
    pain = [p for p in ctx.ledger.pain_for(inst.symbol)]
    lm = build_map(inst.symbol, s.bars, atr, grid, lcfg, None if regimes.empty else regimes, pain,
                   seed=sum(map(ord, inst.symbol)))
    if lm is None:
        return {"map_error": "not enough bars for the level map"}
    if not ctx.p("round_levels_enabled", True):
        lm.zones = [z for z in lm.zones if not z.family.startswith("round")]
    edge_z = float(lcfg.get("edge_z", 2.0))
    tau = float(lcfg.get("strength_half_life_years", 4.0)) * 252 / math.log(2)
    h = int(ctx.cfg["likelihood"].get("horizon_days", 20))
    sigma = bands["energy"]["value"]
    exc = forward_excursions(s.bars, traits["energy"], h, inst.additive)
    price = lm.price
    inside, above, below = _zones_near(lm, price)
    W = incentive_weights(ctx.ledger, inst.symbol)
    gap = (view.get("pair") or {}).get("gap") or {}
    G = gap.get("G") if gap.get("status") == "trusted" else None
    strengths = [z.strength(lm.now_i, tau) for z in lm.zones]
    smax = max(strengths) if strengths and max(strengths) > 0 else 1.0
    used_I = [row["I"] for row in W["rows"] if row["used"] and row["I"]]
    I_norm = (max(used_I) / 125.0) if used_I else 0.0

    def zone_row(z: Zone) -> dict:
        lo_c, hi_c = z.beta_interval()
        resp, scope = z.respect_in(regime.get("regime"), int(lcfg.get("regime_min_touches", 10)))
        Sz = z.strength(lm.now_i, tau)
        aoi = (Sz / smax + (I_norm if z.family == "pain" or used_I else 0.0) + (0 if G is None else min(abs(G) / 3, 1))) / 3
        u = distance_units(price, z.level, sigma, h, inst.additive) if sigma else None
        pt = touch_probability(exc, max(u - 0.0, 0)) if u is not None else None
        ze = zone_edge_ok(z, lm.p0, lm.family_edge, edge_z)
        return {"level": z.level, "family": z.family, "label": z.label, "touches": z.n, "reactions": z.r, "breaks": z.breaks,
                "respect": round(z.respect, 3), "respect_regime": round(resp, 3), "respect_scope": scope,
                "ci90": [round(lo_c, 3), round(hi_c, 3)], "edge_z": None if z.edge_z(lm.p0) is None else round(z.edge_z(lm.p0), 2),
                "edge_ok": ze, "strength": round(Sz, 2), "aoi": round(aoi, 3), "p_touch": None if pt is None else round(pt, 3),
                "p_touch_formula": None if u is None else round(touch_probability_formula(u), 3),
                "attention": None if pt is None else round(pt * aoi, 3), "player": z.player, "evidence": z.evidence,
                "memory_or_pain": (ze and Sz / smax >= 0.5) or z.family == "pain"}

    near = [z for z in lm.zones if abs(z.level - price) <= max(6 * lm.atr_now if np.isfinite(lm.atr_now) else 0, 4 * lm.half_width_now)
            or z in (inside, above, below)]
    rows = {id(z): zone_row(z) for z in near}
    # zone classes and EV of acting at each nearby zone (reaction trades)
    cost_default = float(lcfg.get("cost_r_default", 0.05))
    push_sig = push_signals(s.bars, atr, [z for z in (inside, above, below) if z is not None], ctx.cfg.get("push", {}))
    ps = push_score(push_sig)
    buffer = float(lcfg.get("stop_buffer_atr", 0.25)) * (lm.atr_now if np.isfinite(lm.atr_now) else 0)
    risk_px = lm.half_width_now + buffer
    for z in near:
        row = rows[id(z)]
        direction = -1 if z.level > price else +1 if z.level < price else (-1 if ps < 0 else +1)
        nxt = _next_beyond(lm, z, direction)
        reward = None if nxt is None else (abs(nxt.level - z.level) - lm.half_width_now) / risk_px
        p = row["respect_regime"]
        p += 0.05 * np.sign(ps) * direction if ps else 0
        cot = view.get("cot") or {}
        crowd = cot.get("crowding") or ""
        if crowd:
            leaning = +1 if "long" in crowd else -1
            p += -0.05 if leaning == -direction else 0.05   # the crowd leaning on the zone lowers its hold odds
        p = float(min(max(p, 0.01), 0.99))
        cost = (view.get("liquidity_cost") or 0) * (lm.atr_now / risk_px) if view.get("liquidity_cost") else cost_default
        ev = None if reward is None else p * reward - (1 - p) * 1.0 - cost
        stops_cluster = _stops_cluster(lm, z, direction, risk_px)
        if ev is None:
            cls = "unknown"
        elif ev <= 0 or stops_cluster:
            cls = "unfavorable"
        else:
            cls = "favorable" if ps * direction > 0 else "watch"
        row.update({"trade_dir": "long" if direction > 0 else "short", "p_hold_adj": round(p, 3),
                    "reward_r": None if reward is None else round(reward, 2), "ev": None if ev is None else round(ev, 3),
                    "class": cls, "stops_cluster": stops_cluster})
    zone_table = sorted(rows.values(), key=lambda x: -(x.get("attention") or 0))
    top3 = zone_table[:3]
    # outcomes: next zone up, next zone down, or stay in range
    out: dict[str, Any] = {"level_edge": lm.family_edge, "p0": None if lm.p0 is None else round(lm.p0, 3),
                           "half_width": lm.half_width_now, "atr20": lm.atr_now, "zones_top3": top3,
                           "zones_near": sorted(rows.values(), key=lambda x: x["level"]), "push": {"score": ps, "signals": push_sig},
                           "efficiency_10": efficiency_ratio(s.close, 10)}
    if above is None or below is None or not sigma:
        out["story_error"] = "no zone on one side or no energy reading: no forecast"
        out["incentives"] = W
        return out
    # [IMPL] outcome zones must be worth a 20 day forecast: the nearest zones whose near edge sits at least
    # outcome_min_atr x ATR20 from price (adjacent zones inside one day's range would make the call trivial)
    min_d = float(ctx.cfg["likelihood"].get("outcome_min_atr", 1.0)) * (lm.atr_now if np.isfinite(lm.atr_now) else 0)
    ups = [z for z in lm.zones if z.level - lm.half_width_now >= price + min_d]
    dns = [z for z in lm.zones if z.level + lm.half_width_now <= price - min_d]
    if not ups or not dns:
        out["story_error"] = f"no zone at least {min_d:.6g} away on one side: no forecast"
        out["incentives"] = W
        return out
    above, below = min(ups, key=lambda z: z.level), max(dns, key=lambda z: z.level)
    for z in (above, below):
        if id(z) not in rows:
            rows[id(z)] = zone_row(z)
    up_edge, dn_edge = above.level - lm.half_width_now, below.level + lm.half_width_now
    u_up = distance_units(price, up_edge, sigma, h, inst.additive)
    u_dn = distance_units(price, dn_edge, sigma, h, inst.additive)
    pi = outcome_base_rates(exc, u_up, u_dn)
    if pi is None:
        out["story_error"] = "no base rates"
        out["incentives"] = W
        return out
    pi_ = {k: pi[k] for k in DIR}
    lik = likelihood(pi_, W["W"], view.get("pressure"), float(ctx.p("lambda")), float(ctx.p("T")))
    P = lik["P"]
    favored = max(P, key=P.get)
    # The story is what the incentives and pressure add to the base rates, not which zone is nearer.
    tilt = (P["up"] / pi_["up"]) - (P["down"] / pi_["down"])
    story_dir = 0 if abs(tilt) < float(ctx.cfg["likelihood"].get("story_tilt_min", 0.02)) else int(np.sign(tilt))
    fav_dir = DIR[favored]
    # entry zone: the zone price is in, else the pullback zone opposite the favored direction
    if fav_dir == 0:
        entry, target = inside, None
    else:
        entry = inside or (below if fav_dir > 0 else above)
        target = above if fav_dir > 0 else below
    p_reach = 1.0 if entry is inside and inside is not None else (
        rows.get(id(entry), {}).get("p_touch") if entry is not None else None)
    p_hold = rows.get(id(entry), {}).get("respect_regime") if entry is not None else None
    reward_r = None
    if entry is not None and target is not None:
        tgt_edge = target.level - lm.half_width_now if fav_dir > 0 else target.level + lm.half_width_now
        reward_r = abs(tgt_edge - entry.level) / risk_px
    cost_r = cost_default if not view.get("liquidity_cost") else view["liquidity_cost"] * (lm.atr_now / risk_px)
    ev_e = None
    if fav_dir != 0 and p_hold is not None and reward_r is not None:
        ev_e = ev_entry(P[favored], p_hold, reward_r, 1.0, cost_r)
    p_opp = None if None in (p_reach, p_hold) or fav_dir == 0 else P[favored] * p_reach * p_hold
    top = next((row for row in W["rows"] if row["used"]), None)
    goal = ctx.ledger.goals.get(top.get("goal")) if top and top.get("goal") else None
    trade_type = "alignment" if goal and (goal.credibility or 0) > 0.5 else "weakness"
    recent_break = _recent_break(s.bars, lm, int(lcfg.get("break_lookback", 5)))
    in_proven = bool(inside is not None and rows[id(inside)]["edge_ok"])
    leaving_vac = False
    if recent_break:
        nz = above if recent_break["dir"] > 0 else below
        leaving_vac = bool(nz is not None and vacuum_between(lm.vacuum, price, nz.level))
    loc_why = (f"inside {inside.label} (edge {'proven' if in_proven else 'not proven'})" if inside is not None else
               "between zones") + (f"; broke {recent_break['label']} into a vacuum" if leaving_vac else "")
    out.update({
        "outcomes": {"up": {"zone": above.label, "level": above.level, "edge": up_edge},
                     "down": {"zone": below.label, "level": below.level, "edge": dn_edge}, "range": {}},
        "base_rates": pi, "incentives": W, "W_total": lik["W_total"], "P": P, "favored": favored, "story_dir": story_dir,
        "p_dir": P[favored], "p_reach": p_reach, "p_hold": p_hold, "p_opp": p_opp, "reward_r": reward_r, "ev_entry": ev_e,
        "cost_r": cost_r, "entry_zone": None if entry is None else entry.label, "entry_level": None if entry is None else entry.level,
        "target_zone": None if target is None else target.label, "top_incentive": top, "trade_type": trade_type,
        "goal": None if goal is None else {"id": goal.id, "goal": goal.goal, "credibility": goal.credibility},
        "at_zone": inside is not None, "zone_now": None if inside is None else rows[id(inside)],
        "location": {"in_proven_zone": in_proven, "leaving_into_vacuum": leaving_vac, "why": loc_why},
        "recent_break": recent_break, "story_push_agree": fav_dir != 0 and story_dir == fav_dir and ps * fav_dir > 0,
        "push_opposes": fav_dir != 0 and ps * fav_dir < 0, "tilt": round(float(tilt), 4), "incentive_to_hold": bool(
            inside is not None and top and DIR.get(top["gains_from"], 0) == (1 if inside.level < price else -1 if inside.level > price else fav_dir)),
        "invalidation": _invalidation(entry, fav_dir, lm.half_width_now, inst),
    })
    return out


def _stops_cluster(lm, z: Zone, direction: int, risk_px: float) -> bool:
    """A stop for a reaction trade at z would sit just beyond the zone. If a major round level or another
    zone's far edge sits within half a risk unit of that stop, obvious stops cluster there."""
    stop = z.level - direction * risk_px
    for x in lm.zones:
        if x is z:
            continue
        if x.family in ("round_major", "market") and abs(x.level - stop) < 0.5 * risk_px:
            return True
    return False


def _recent_break(bars: pd.DataFrame, lm, lookback: int) -> Optional[dict]:
    c = bars["close"].values
    if len(c) < lookback + 3:
        return None
    for z in lm.zones:
        top, bot = z.level + lm.half_width_now, z.level - lm.half_width_now
        seg = c[-(lookback + 2):]
        for i in range(2, len(seg)):
            if seg[i - 2] <= top and seg[i - 1] > top and seg[i] > top:
                return {"label": z.label, "level": z.level, "dir": +1}
            if seg[i - 2] >= bot and seg[i - 1] < bot and seg[i] < bot:
                return {"label": z.label, "level": z.level, "dir": -1}
    return None


def _invalidation(entry: Optional[Zone], story_dir: int, w: float, inst: Inst) -> str:
    if entry is None or story_dir == 0:
        return "no directional story"
    side = "below" if story_dir > 0 else "above"
    lvl = entry.level - w if story_dir > 0 else entry.level + w
    return f"2 daily closes {side} {lvl:.6g} (far side of {entry.label})"


# =============================================================================
# Step 8: cards
# =============================================================================

def build_card(ctx: Ctx, v: dict, scanners: list[dict], ev: dict) -> dict:
    sym = v["symbol"]
    band = v.get("overall_band")
    card: dict[str, Any] = {"symbol": sym, "label": v.get("label"), "price": v.get("price"), "usd_sign": v.get("usd_sign")}
    card.update({k: v.get(k) for k in ("favored", "story_dir", "P", "p_dir", "p_reach", "p_hold", "p_opp", "reward_r",
                                       "ev_entry", "trade_type", "top_incentive", "goal", "at_zone", "zone_now", "location",
                                       "recent_break", "story_push_agree", "push_opposes", "incentive_to_hold",
                                       "invalidation", "entry_zone", "target_zone", "outcomes", "base_rates", "window")})
    card["character"] = {"band": band, "cause": v.get("cause"), "distance": (v.get("distance") or {}).get("D")}
    card["gap"] = (v.get("pair") or {}).get("gap")
    card["pressure"] = v.get("pressure")
    card["pressure_pct"] = v.get("pressure_pct")
    card["needle_pass"] = bool(v.get("active_events"))
    gates = gate_results(card, {})
    card["gates"] = gates
    st = setup_type(card)
    card["setup_type"] = st
    cells = {}
    for sc in scanners:
        cells[sc["name"]] = alignment_cell(card.get("story_dir") or 0, sc, sym)
    card["alignment"] = cells
    card["story_technical_conflict"] = any(c["cell"] == "conflict" for c in cells.values())
    gp = {k: ctx.p(k) for k in ("p_dir_a", "p_dir_b", "ev_min", "reward_min", "pressure_pct_min")}
    gr = grade(card, gp)
    if st in (ctx.p("retired_setups") or []) and gr["grade"] in ("A", "B"):
        gr = {"grade": "C", "missed": gr["missed"] + ["setup retired"], "action": "monitor only",
              "why": f"setup '{st}' retired after failing out of sample"}
    card["grade"] = gr["grade"]
    card["grade_detail"] = gr
    card["long_vs_short"] = _long_vs_short(ctx, sym, v)
    card["who_gains"] = card.get("top_incentive")
    player = (card.get("top_incentive") or {}).get("player")
    pl = ctx.ledger.players.get(player) or {}
    card["what_they_need"] = {"levers": pl.get("levers"), "pushes_when": pl.get("pushes_when"), "sits_when": pl.get("sits_when")}
    return card


def _long_vs_short(ctx: Ctx, sym: str, v: dict) -> dict:
    """Does today's behavior serve the player's long term goal? Goal market direction vs the 20 day move."""
    goals = [g for g in ctx.ledger.goals.values() if sym in g.markets]
    s = ctx.series.get(sym)
    if not goals or s is None or len(s.close) < 25:
        return {"status": "no plan goal mapped to this instrument"}
    move = int(np.sign(s.close.iloc[-1] - s.close.iloc[-21]))
    rows = []
    for g in goals:
        want = int(np.sign(float(g.markets[sym])))
        rows.append({"goal": g.id, "player": g.player, "wants": "up" if want > 0 else "down",
                     "status": "aligned" if want == move else "diverging"})
    div = any(r["status"] == "diverging" for r in rows)
    return {"status": "diverging: either quiet repositioning or the long term thesis is wrong" if div else "aligned",
            "goals": rows, "move_20d": "up" if move > 0 else "down"}


def write_forecasts(ctx: Ctx, cards: list[dict], views: dict, regime: dict) -> int:
    fc = ctx.journal.read("forecasts.csv")
    existing = set(fc["forecast_id"]) if not fc.empty else set()
    h = int(ctx.cfg["likelihood"].get("horizon_days", 20))
    expiry = (ctx.asof + pd.offsets.BDay(h)).normalize()
    rows = []
    for c in cards:
        v = views[c["symbol"]]
        if not v.get("P") or not v.get("outcomes"):
            continue
        fid = f"{ctx.asof.date()}_{c['symbol']}"
        if fid in existing:
            continue
        W = v["incentives"]["W"]
        excluded = [r["id"] for r in v["incentives"]["rows"] if not r["used"]]
        rows.append({"forecast_id": fid, "made_date": str(ctx.asof.date()), "run_type": ctx.run_type, "instrument": c["symbol"],
                     "horizon_days": h, "expiry_date": str(expiry.date()), "price": v["price"],
                     "zone_up": v["outcomes"]["up"]["level"], "zone_down": v["outcomes"]["down"]["level"],
                     "zone_up_edge": v["outcomes"]["up"]["edge"], "zone_down_edge": v["outcomes"]["down"]["edge"],
                     "pi_up": v["base_rates"]["up"], "pi_down": v["base_rates"]["down"], "pi_range": v["base_rates"]["range"],
                     "W_up": W["up"], "W_down": W["down"], "W_range": W["range"], "pressure": v.get("pressure"),
                     "T": ctx.p("T"), "lambda": ctx.p("lambda"), "P_up": v["P"]["up"], "P_down": v["P"]["down"],
                     "P_range": v["P"]["range"], "favored": v["favored"], "p_dir": v["p_dir"], "p_reach": v.get("p_reach"),
                     "p_hold": v.get("p_hold"), "p_opp": v.get("p_opp"), "grade": c["grade"], "setup_type": c["setup_type"],
                     "trade_type": c.get("trade_type"), "gates": {k: g["pass"] for k, g in c["gates"].items() if not k.startswith("_")},
                     "gate_status": c["gates"]["_status"], "regime": regime.get("regime"),
                     "alignment_cells": {k: x["cell"] for k, x in c["alignment"].items()}, "story_dir": c.get("story_dir"),
                     "excluded_inputs": excluded, "config_version": ctx.cfg["config_version"],
                     "ledger_version": ctx.ledger.version, "params_version": ctx.params["version"],
                     "source": "demo" if ctx.demo else "live", "status": "open"})
    ctx.journal.append("forecasts.csv", rows, FORECAST_FIELDS)
    return len(rows)


def yesterday_check(ctx: Ctx, resolved: list[dict], cards: list[dict]) -> dict:
    prev_dir = ctx.base / ctx.cfg["paths"]["output_dir"]
    if ctx.demo:
        prev_dir = prev_dir / "demo"
    prev = sorted(prev_dir.glob("daily_*.json"))
    prev = [p for p in prev if p.stem < f"daily_{ctx.asof.date()}"]
    diffs = []
    if prev:
        try:
            old = json.loads(prev[-1].read_text(encoding="utf-8"))
            oc = {c["symbol"]: c for c in old.get("cards", [])}
            for c in cards:
                o = oc.get(c["symbol"])
                if not o:
                    continue
                ch = []
                if o.get("grade") != c.get("grade"):
                    ch.append(f"grade {o.get('grade')} -> {c.get('grade')}")
                if (o.get("character") or {}).get("band") != c["character"]["band"]:
                    ch.append(f"character {(o.get('character') or {}).get('band')} -> {c['character']['band']}")
                if o.get("favored") != c.get("favored"):
                    ch.append(f"favored {o.get('favored')} -> {c.get('favored')}")
                if o.get("gates", {}).get("_status") != c["gates"]["_status"]:
                    ch.append(f"gates {o.get('gates', {}).get('_status')} -> {c['gates']['_status']}")
                if ch:
                    diffs.append({"symbol": c["symbol"], "changes": ch})
        except (OSError, ValueError):
            pass
    return {"previous_report": prev[-1].name if prev else None, "card_changes": diffs, "resolved": [
        {k: r.get(k) for k in ("forecast_id", "instrument", "favored", "p_dir", "outcome", "resolved_date", "brier", "hit")}
        for r in resolved]}


# =============================================================================
# Run
# =============================================================================

def run_daily(ctx: Ctx) -> dict:
    ctx.traits_cache = {}  # type: ignore[attr-defined]
    ctx.gap_hist = {}  # type: ignore[attr-defined]
    led = ctx.journal.ledger_snapshot(ctx.ledger.raw, ctx.ledger_path, str(ctx.asof.date()))
    data_status = load_data(ctx)
    regime = regime_step(ctx)
    scoreboard = scoreboard_step(ctx)
    archive = calendar_archive(ctx)
    ev = events_step(ctx, archive)
    ev_days = event_days_by_ccy(ctx, archive, getattr(ctx, "owner_events", pd.DataFrame()))
    flips = flips_step(ctx)
    cot = load_cot(ctx)
    scanners = [load_scanner(sc, ctx.base, ctx.asof) for sc in ctx.cfg.get("scanners", [])]
    # solo indices first so pair checks can use their traits
    order = sorted([s for s, i in ctx.insts.items() if i.profile], key=lambda s: (ctx.insts[s].kind != "solo", s))
    views = {}
    for sym in order:
        try:
            views[sym] = instrument_view(ctx, sym, ev, flips, regime, archive, cot, ev_days)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("%s failed", sym)
            views[sym] = {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}
    # resolve open forecasts before writing today's
    fc = ctx.journal.read("forecasts.csv")
    bars = {s: ctx.series[s].bars for s in ctx.series if not ctx.series[s].bars.empty}
    fc, resolved = resolve_forecasts(fc, bars, ctx.asof)
    if resolved:
        ctx.journal.rewrite("forecasts.csv", fc)
    cards = [build_card(ctx, v, scanners, ev) for s, v in views.items() if not v.get("error") and v.get("P")]
    cards.sort(key=lambda c: ("ABC".find(c["grade"]) if c["grade"] in "ABC" else 9, -(c.get("p_dir") or 0)))
    n_fc = write_forecasts(ctx, cards, views, regime)
    exp = exposure_check(cards, float(ctx.cfg.get("risk", {}).get("net_usd_cap", 2.0)),
                         float(ctx.cfg.get("risk", {}).get("single_player_cap", 2.0)))
    gaps = [{"symbol": s, **(v.get("pair") or {}).get("gap", {})} for s, v in views.items()
            if (v.get("pair") or {}).get("gap", {}).get("G") is not None and abs(v["pair"]["gap"]["G"]) > 2]
    yc = yesterday_check(ctx, resolved, cards)
    _log_daily(ctx, views, regime)
    report = {"meta": {"date": str(ctx.asof.date()), "stamp": ctx.stamp, "run_type": "daily", "engine": ENGINE_VERSION,
                       "config_version": ctx.cfg["config_version"], "ledger_version": ctx.ledger.version,
                       "ledger_hash": led["hash"], "ledger_changed": led["changed"][:40] if led["new"] else [],
                       "params_version": ctx.params["version"], "params": ctx.params["values"], "demo": ctx.demo,
                       "data": data_status, "notes": ctx.notes},
              "regime": regime, "scoreboard": scoreboard, "events": {k: ev[k] for k in ev if k != "contribs"},
              "flips": flips, "views": views, "cards": cards, "exposure": exp, "gaps": gaps, "yesterday": yc,
              "scanners": [{k: sc[k] for k in sc if k != "rows"} | {"n_rows": len(sc["rows"])} for sc in scanners],
              "forecasts_written": n_fc, "cycle_staleness": cycle_staleness(ctx.ledger, ctx.asof)}
    ctx.journal.append("runs.csv", [{"date": str(ctx.asof.date()), "stamp": ctx.stamp, "run_type": "daily",
                                     "config_version": ctx.cfg["config_version"], "ledger_version": ctx.ledger.version,
                                     "params_version": ctx.params["version"], "cards": len(cards), "forecasts": n_fc,
                                     "resolved": len(resolved), "regime": regime.get("regime"), "demo": ctx.demo}])
    return report


def _log_daily(ctx: Ctx, views: dict, regime: dict) -> None:
    rows, prs = [], []
    for sym, v in views.items():
        if v.get("error"):
            continue
        for t, b in (v.get("bands") or {}).items():
            rows.append({"date": str(ctx.asof.date()), "symbol": sym, "trait": t, "value": b.get("value"), "pct": b.get("pct"),
                         "band": b.get("band"), "z": b.get("z"), "drift": (v.get("drift_now") or {}).get(t),
                         "regime": regime.get("regime")})
        prs.append({"date": str(ctx.asof.date()), "symbol": sym, "pressure": v.get("pressure"), "pct": v.get("pressure_pct"),
                    "band": v.get("overall_band"), "D": (v.get("distance") or {}).get("D")})
    # one snapshot per date: drop earlier rows for today before appending
    for name, data in (("traits_daily.csv", rows), ("pressure_daily.csv", prs)):
        old = ctx.journal.read(name)
        if not old.empty and (old["date"] == str(ctx.asof.date())).any():
            ctx.journal.rewrite(name, old[old["date"] != str(ctx.asof.date())])
        ctx.journal.append(name, data)
