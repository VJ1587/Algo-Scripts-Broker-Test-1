"""HTML reports for the daily monitor and the monthly, quarterly and annual reviews (plus JSON copies)."""
from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any, Optional

CSS = """
:root{--bg:#fbfbf9;--fg:#1d1d1b;--mut:#6b6b66;--line:#e2e1dc;--card:#fff;--a:#1f7a4d;--b:#2f6db3;--c:#b07a12;--bad:#b3392f;--hl:#f3f1ea}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--mut:#a3a29b;--line:#34332f;--card:#1e1e1c;--hl:#262622}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1280px;margin:0 auto;padding:16px}h1{font-size:22px;margin:4px 0}h2{font-size:17px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
h3{font-size:15px;margin:14px 0 6px}.mut{color:var(--mut)}.small{font-size:12px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;margin:6px 0 12px;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top;white-space:nowrap}td.w{white-space:normal;min-width:220px}
th{font-weight:600;color:var(--mut);font-size:12px;background:var(--hl)}
.g{display:inline-block;min-width:22px;text-align:center;border-radius:4px;padding:0 6px;font-weight:700;color:#fff}
.gA{background:var(--a)}.gB{background:var(--b)}.gC{background:var(--c)}.gnone{background:#888}
.ok{color:var(--a);font-weight:600}.bad{color:var(--bad);font-weight:600}.warn{color:var(--c);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:10px 0}
.kv{display:grid;grid-template-columns:220px 1fr;gap:3px 12px}.kv div:nth-child(odd){color:var(--mut)}
.banner{background:#b3392f;color:#fff;padding:8px 12px;border-radius:6px;margin:8px 0;font-weight:600}
.pill{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 7px;margin:1px;font-size:12px}
details>summary{cursor:pointer;color:var(--mut)}
@media (max-width:700px){.kv{grid-template-columns:1fr}main{padding:12px}}
"""


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def f(x: Any, nd: int = 3) -> str:
    if x is None:
        return '<span class="mut">unknown</span>'
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (int,)) and not isinstance(x, bool):
        return str(x)
    try:
        v = float(x)
    except (TypeError, ValueError):
        return e(x)
    if math.isnan(v):
        return '<span class="mut">unknown</span>'
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if v != 0 and abs(v) < 10 ** -nd:
        return f"{v:.{nd}g}"
    s = f"{v:.{nd}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def pct(x: Any) -> str:
    return '<span class="mut">unknown</span>' if x is None else f"{float(x) * 100:.0f}%"


def table(headers: list[str], rows: list[list[str]], wide: Optional[set] = None) -> str:
    wide = wide or set()
    h = "".join(f"<th>{e(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f'<td class="w">{c}</td>' if i in wide else f"<td>{c}</td>" for i, c in enumerate(r)) + "</tr>"
                   for r in rows)
    return f'<div class="wrap"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>{body}"
            '<p class="mut small">Analytical framework, not investment advice. Public sources only. Figures labelled '
            "working assumption, unverified or unknown must be checked at a primary source before use.</p></main></body></html>")


def grade_badge(g: str) -> str:
    return f'<span class="g g{e(g)}">{e(g)}</span>'


def band_cls(b: Optional[str]) -> str:
    return {"core": "ok", "stretched": "warn", "out of character": "bad"}.get(b or "", "mut")


def yes(b: Any) -> str:
    return '<span class="ok">pass</span>' if b else '<span class="bad">fail</span>'


# =============================================================================
# Daily
# =============================================================================

def render_daily(rep: dict) -> str:
    m = rep["meta"]
    out = [f"<h1>Empire Game monitor · {e(m['date'])}</h1>",
           f'<p class="mut small">engine {e(m["engine"])} · config {e(m["config_version"])} · ledger {e(m["ledger_version"])} '
           f'({e(m["ledger_hash"])}) · params {e(m["params_version"])} (T {f(m["params"].get("T"))}, λ {f(m["params"].get("lambda"))})</p>']
    if m.get("demo"):
        out.append('<div class="banner">DEMO DATA: synthetic prices. Plumbing check only, never a basis for decisions.</div>')
    if m.get("ledger_changed"):
        out.append('<p class="warn">Ledger edited since the last run: ' + e("; ".join(m["ledger_changed"][:12])) + "</p>")
    # 1-2 regime and scoreboard
    r = rep["regime"]
    sb = rep["scoreboard"]
    out.append("<h2>1. Regime and scoreboard</h2>")
    if r.get("regime"):
        out.append(f'<div class="kv"><div>Regime</div><div><b>{e(r["regime"])}</b> since {e(r["since"])}'
                   f'{" <span class=warn>(changed today)</span>" if r.get("changed_today") else ""}: {e(r.get("text"))}</div>'
                   f'<div>Dollar liquidity</div><div>{e(r["liquidity"])}{" (axes disagree; leaning)" if r.get("liquidity_mixed") else ""} · '
                   f'dollar 6m {f(r["dollar_change_6m"])} · 2y 6m {f(r["y2_change_6m"])} pts</div>'
                   f'<div>Risk appetite</div><div>{e(r["risk"])} · VIX {f(r["vix"])} vs 5y median {f(r["vix_median_5y"])}</div>'
                   f'<div>Share of last 20 years</div><div>{", ".join(f"{e(k)} {pct(v)}" for k, v in (r.get("share_20y") or {}).items())}</div></div>')
    else:
        out.append(f'<p class="bad">Regime unknown: {e(r.get("why"))}</p>')
    out.append(f'<p>Scoreboard: dollar index vs equal weight dollar, 20 days: <b>{e(sb.get("reading"))}</b> '
               f'(DXY {f((sb.get("DXY") or {}).get("chg_20d"))}, equal weight {f((sb.get("USD_IDX") or {}).get("chg_20d"))})</p>')
    # cards summary
    cards = rep["cards"]
    scn = [s["name"] for s in rep["scanners"]]
    out.append("<h2>2. Story cards: summary</h2>")
    out.append('<p class="mut small">Grades: A full planned risk at the zone · B half · C monitor only · none log only. '
               "Gates: 4 = candidate (still needs your confirmation pattern inside the zone), 3 = watch.</p>")
    rows = []
    for c in cards:
        P = c.get("P") or {}
        rows.append([f"<b>{e(c['symbol'])}</b>", grade_badge(c["grade"]), f"{c['gates']['_count']}/4 {e(c['gates']['_status'])}",
                     f"{e(c.get('favored'))} {pct(c.get('p_dir'))}", f"↑{pct(P.get('up'))} ↓{pct(P.get('down'))} ↔{pct(P.get('range'))}",
                     pct(c.get("p_reach")), pct(c.get("p_hold")), pct(c.get("p_opp")), f(c.get("ev_entry")), f(c.get("reward_r")),
                     e(c.get("setup_type")), e(c.get("trade_type")),
                     f'<span class="{band_cls(c["character"]["band"])}">{e(c["character"]["band"])}</span>',
                     f(c.get("pressure"), 4)] + [e(c["alignment"].get(s, {}).get("cell")) for s in scn])
    out.append(table(["Instrument", "Grade", "Gates", "Favored", "Likelihoods", "Reach", "Hold", "P opp", "EV entry (R)",
                      "Reward (R)", "Setup", "Trade type", "Character", "Pressure"] + [f"vs {s}" for s in scn], rows))
    ex = rep["exposure"]
    out.append(f'<p>Exposure check (graded A/B only): net dollar {f(ex["net_usd"])} risk units. '
               + ("".join(f'<span class="bad">{e(x)}</span> ' for x in ex["flags"]) or '<span class="ok">within caps</span>') + "</p>")
    # yesterday
    y = rep["yesterday"]
    out.append("<h2>3. Yesterday's calls against what happened</h2>")
    if y["resolved"]:
        out.append(table(["Forecast", "Instrument", "Favored", "P", "Outcome", "Resolved", "Brier", "Hit"],
                         [[e(x["forecast_id"]), e(x["instrument"]), e(x["favored"]), f(x["p_dir"]), f'<b>{e(x["outcome"])}</b>',
                           e(x["resolved_date"]), f(x["brier"]), f(x["hit"])] for x in y["resolved"]]))
    else:
        out.append('<p class="mut">No forecasts resolved today.</p>')
    if y["card_changes"]:
        out.append(table(["Instrument", "Changed since " + e(y.get("previous_report"))],
                         [[e(c["symbol"]), e("; ".join(c["changes"]))] for c in y["card_changes"]], {1}))
    # detailed cards
    out.append("<h2>4. Story cards</h2>")
    for c in cards:
        v = rep["views"].get(c["symbol"], {})
        out.append(render_card(c, v, scn))
    # character
    out.append("<h2>5. Character: who is acting like itself</h2>")
    crow = []
    for s, v in rep["views"].items():
        if v.get("error"):
            crow.append([e(s), f'<span class="bad">{e(v["error"])}</span>'] + [""] * 9)
            continue
        b = v.get("bands", {})

        def bb(t):
            x = b.get(t) or {}
            return f'<span class="{band_cls(x.get("band"))}">{f(x.get("value"))}</span> <span class="mut small">p{f(x.get("pct"), 2)}</span>'
        flags = [k for k, d in (v.get("drift_now") or {}).items() if d is not None and abs(d) > 2]
        crow.append([f"<b>{e(s)}</b>", f'<span class="{band_cls(v.get("overall_band"))}">{e(v.get("overall_band"))}</span>',
                     f((v.get("distance") or {}).get("D")) + (" ⚑" if (v.get("distance") or {}).get("flag") else ""),
                     bb("energy"), bb("conviction"), bb("patience"), bb("fear_beta"), bb("rate_beta"), bb("resource_beta"),
                     bb("asymmetry"), e(", ".join(flags)) + (" · beyond stress range" if v.get("beyond_stress_range") else "")])
    out.append(table(["Instrument", "Band", "Distance D", "Energy", "Conviction", "Patience (log10 d)", "Fear β", "Rate β",
                      "Resource β", "Asymmetry", "Drift beyond ±2 / footprints"], crow))
    pcs = [(s, x) for s, v in rep["views"].items() for x in (v.get("personality_changes") or [])]
    if pcs:
        out.append("<h3>Personality changes logged today</h3>" + table(
            ["Instrument", "Trait", "Status", "Direction", "Drift", "Days", "Suspected cause"],
            [[e(s), e(x["trait"]), e(x["status"]), e(x["direction"]), f(x["drift"]), e(x["days"]), e(x["suspected_cause"])] for s, x in pcs], {6}))
    fl = rep["flips"]
    out.append("<h3>Relationship flips (60 day correlation against its 5 year median)</h3>" + table(
        ["Relationship", "Corr 60d", "5y median", "Status"],
        [[e(k), f(x.get("corr")), f(x.get("median")), f'<span class="{"bad" if x.get("flag") else "mut"}">{e(x.get("status"))}</span>']
         for k, x in fl.items()]))
    # gaps
    out.append("<h2>6. Incentive gaps beyond ±2</h2>")
    if rep["gaps"]:
        out.append(table(["Pair", "G", "Trend", "Days beyond 2", "Status", "Drivers"],
                         [[e(g["symbol"]), f(g.get("G")), e(g.get("trend")), e(g.get("days_beyond_2")), e(g.get("status")),
                           e(", ".join(g.get("drivers", [])))] for g in rep["gaps"]]))
    else:
        out.append('<p class="mut">No pair sits more than 2 units from its drivers.</p>')
    # events
    ev = rep["events"]
    out.append("<h2>7. Ledger and events</h2>")
    counted = [x for x in ev["events"] if x["counts"]]
    out.append(f'<p>{len(ev["events"])} events measured ({ev["n_owner"]} from your events file, {ev["n_calendar"]} red-folder releases); '
               f"{len(counted)} pass every test and add pressure.</p>")
    recent = sorted(ev["events"], key=lambda x: x["date"], reverse=True)[:25]
    out.append(table(["Date", "Event", "Type", "Tier", "Shock", "On", "Fingerprint", "Needle A", "Needle B", "Weight", "Counts", "Why not"],
                     [[e(x["date"]), e(x["headline"]), e(x["type"]), e(x["tier"]), f(x["shock_max"]), e(x["shock_on"]),
                       f'{e(x["fingerprint"])} {f(x["fingerprint_sim"])}', e("; ".join(x["needle"]["part_a"])) or "-",
                       e("; ".join(x["needle"]["part_b"])) or "-", f(x["weight"]["E"], 4), yes(x["counts"]), e(x["why_not"])]
                      for x in recent], {1, 7, 8, 11}))
    if ev.get("pending_questions"):
        out.append("<h3>Your events still missing answers to the qualifying questions</h3><ul>" +
                   "".join(f"<li>{e(p['id'])}: {e(p['headline'])}</li>" for p in ev["pending_questions"]) + "</ul>"
                   "<details><summary>The 14 questions</summary><ol>" + "".join(f"<li>{e(q)}</li>" for q in ev["questions"]) + "</ol></details>")
    al = ev.get("alignment") or {}
    if al:
        out.append("<h3>Plan alignment (rolling 90 days)</h3>" + table(
            ["Goal", "Player", "Credibility", "Events", "Sum", "A"],
            [[e(k), e(a["player"]), f(a["credibility"]), e(a["events"]), e(a["sum"]), f(a["A"], 4)] for k, a in al.items()]))
    lib = ev.get("library") or {}
    if lib:
        out.append("<h3>Event library by type</h3>" + table(["Type", "Events", "Median |shock|", "Median persistence", "Half life (d)"],
                                                              [[e(t), e(x["n"]), f(x["median_shock"]), f(x["median_persistence"]), f(x["half_life"])] for t, x in lib.items()]))
    st = rep.get("cycle_staleness") or []
    if st:
        out.append(f'<h3>Cycle scorecard inputs unknown or stale ({len(st)})</h3><details><summary>show</summary>' + table(
            ["Player", "Indicator", "Value", "As of", "Status"],
            [[e(x["player"]), e(x["indicator"]), f(x["value"]), e(x["as_of"]), e(x["status"])] for x in st]) + "</details>")
    # data
    out.append("<h2>8. Data and sources</h2>")
    d = m["data"]
    out.append("<p class=small>" + e(" · ".join(f"{k}: {v}" for k, v in (d.get("_sources") or {}).items())) + "</p>")
    out.append(table(["Series", "Rows", "Years", "Last", "Notes"],
                     [[e(k), e(v["rows"]), e(v["years"]), e(v["last"]), e("; ".join(v["notes"]))] for k, v in d.items() if not k.startswith("_")], {4}))
    out.append("<p>Scanners compared: " + (", ".join(f'{e(s["name"])} ({e(s["status"])}, {s["n_rows"]} setups{", " + e(s["file"]) if s.get("file") else ""})'
                                                    for s in rep["scanners"]) or "none configured") + "</p>")
    if m.get("notes"):
        out.append("<ul>" + "".join(f"<li>{e(n)}</li>" for n in m["notes"]) + "</ul>")
    return page(f"Empire monitor {m['date']}", "".join(out))


def render_card(c: dict, v: dict, scn: list[str]) -> str:
    g = c["gates"]
    rows = v.get("incentives", {}).get("rows", [])
    inc = "".join(f'<span class="pill">{e(r["player"])} {e(r["gains_from"])} I={f(r["I"])}'
                  f'{"" if r["used"] else " (excluded: " + e(r["excluded"]) + ")"} · {e(r["evidence"])}</span>' for r in rows) or \
        '<span class="mut">no ledger entry for this instrument</span>'
    cot = v.get("cot") or {}
    cot_txt = (f'index {f(cot.get("index"))} {e(cot.get("crowding") or "")} · {e(cot.get("price_oi"))} · report {e(cot.get("report_date"))} '
               f'({e(cot.get("age_days"))} days old)') if cot.get("status") == "ok" else e(cot.get("status"))
    zones = v.get("zones_top3") or []
    ztab = table(["Zone", "Family", "Touches", "Respect (regime)", "90% CI", "Edge z", "P touch", "Attention", "Trade", "EV (R)", "Class"],
                 [[e(z["label"]), e(z["family"]), f"{z['touches']}/{z['reactions']}r", f(z["respect_regime"]),
                   f"{f(z['ci90'][0])}–{f(z['ci90'][1])}", f(z["edge_z"]), pct(z["p_touch"]), f(z["attention"]),
                   e(z.get("trade_dir")), f(z.get("ev")), e(z.get("class")) + (" (stops cluster)" if z.get("stops_cluster") else "")]
                  for z in zones])
    gl = "".join(f'<div>{e(k.title())} gate</div><div>{yes(x["pass"])} {e(x["why"])}</div>' for k, x in g.items() if not k.startswith("_"))
    al = "".join(f'<div>vs {e(s)}</div><div><b>{e(c["alignment"][s]["cell"])}</b>: {e(_align_text(c["alignment"][s]["cell"]))} '
                 f'{e(c["alignment"][s].get("detail"))}</div>' for s in scn)
    lvs = c.get("long_vs_short") or {}
    gd = c.get("grade_detail") or {}
    return (f'<div class="card"><h3>{grade_badge(c["grade"])} {e(c["symbol"])} <span class="mut">{e(c.get("label"))} · {f(c.get("price"), 5)}</span></h3>'
            f'<div class="kv"><div>Grade</div><div>{e(gd.get("action"))}: {e(gd.get("why"))}</div>'
            f'<div>Who gains</div><div>{inc}</div>'
            f'<div>What they need</div><div>levers {e(", ".join((c.get("what_they_need") or {}).get("levers") or []))}; pushes when '
            f'{e((c.get("what_they_need") or {}).get("pushes_when"))}; sits when {e((c.get("what_they_need") or {}).get("sits_when"))}</div>'
            f'<div>Window</div><div>{e((c.get("window") or {}).get("why"))}'
            f'{" · <span class=bad>event inside stop: " + e(c["window"]["inside_stop"]) + "</span>" if (c.get("window") or {}).get("event_inside_stop") else ""}</div>'
            f'<div>Character</div><div><span class="{band_cls(c["character"]["band"])}">{e(c["character"]["band"])}</span>'
            f' · distance {f(c["character"].get("distance"))}{" · cause: " + e(c["character"]["cause"]) if c["character"].get("cause") else ""}</div>'
            f'<div>Incentive gap</div><div>{_gap_txt(c.get("gap"))}</div>'
            f'<div>Pressure</div><div>{f(c.get("pressure"), 4)} (percentile {f(c.get("pressure_pct"))}) '
            f'{"".join("<span class=pill>" + e(a["headline"]) + " x=" + f(a["x"]) + "</span>" for a in v.get("active_events", []))}</div>'
            f'<div>Positioning (COT)</div><div>{cot_txt}</div>'
            f'<div>Map</div><div>up: {e((c.get("outcomes") or {}).get("up", {}).get("zone"))} · down: {e((c.get("outcomes") or {}).get("down", {}).get("zone"))}'
            f' · level edge: round {f(((v.get("level_edge") or {}).get("round_major") or {}).get("z"))}, market {f(((v.get("level_edge") or {}).get("market") or {}).get("z"))} (z vs random, p0 {f(v.get("p0"))})</div>'
            f'<div>Forecast</div><div>base rates ↑{pct((c.get("base_rates") or {}).get("up"))} ↓{pct((c.get("base_rates") or {}).get("down"))} '
            f'↔{pct((c.get("base_rates") or {}).get("range"))} → tilted ↑{pct((c.get("P") or {}).get("up"))} ↓{pct((c.get("P") or {}).get("down"))} '
            f'↔{pct((c.get("P") or {}).get("range"))} over {e(v.get("horizon", 20))} days; entry {e(c.get("entry_zone"))} → target {e(c.get("target_zone"))}</div>'
            f'<div>Invalidation</div><div>{e(c.get("invalidation"))}</div>'
            f'<div>Push</div><div>score {e((v.get("push") or {}).get("score"))} '
            f'{"".join("<span class=pill>" + e(s["date"]) + " " + e(s["type"]) + (" ↑" if s["dir"] > 0 else " ↓" if s["dir"] < 0 else "") + "</span>" for s in (v.get("push") or {}).get("signals", []))}'
            f'{" · <span class=bad>push opposes the story: the market may be saying the story is wrong</span>" if c.get("push_opposes") else ""}</div>'
            f'<div>Long term goal vs short term behavior</div><div>{e(lvs.get("status"))} '
            f'{"".join("<span class=pill>" + e(r["goal"]) + " wants " + e(r["wants"]) + ": " + e(r["status"]) + "</span>" for r in lvs.get("goals", []))}</div>'
            f'{gl}{al}</div><details><summary>Zones by attention</summary>{ztab}</details></div>')


def _align_text(cell: str) -> str:
    from .stories import ALIGN_TEXT
    return ALIGN_TEXT.get(cell, "")


def _gap_txt(g: Optional[dict]) -> str:
    if not g:
        return '<span class="mut">not a pair</span>'
    if g.get("G") is None:
        return e(g.get("status"))
    mr = g.get("mean_reversion") or {}
    return (f'G {f(g["G"])} ({e(g.get("trend"))}, {e(g.get("days_beyond_2"))} days beyond 2) · {e(g.get("status"))} '
            f'(DF t {f(mr.get("t"))}, half life {f(mr.get("half_life"))} d)')


# =============================================================================
# Reviews
# =============================================================================

def _score_html(sc: dict) -> str:
    if not sc or not sc.get("n"):
        return '<p class="mut">No resolved forecasts in this window yet.</p>'
    out = [f'<p>{sc["n"]} resolved · Brier {f(sc["brier"])} (base rates alone {f(sc.get("brier_base_rates"))}, skill {f(sc.get("skill_vs_base"))}) · '
           f'log loss {f(sc["logloss"])}</p>']
    for key, lab in (("by_setup", "setup_type"), ("by_grade", "grade"), ("by_gate_status", "gate_status"), ("by_trade_type", "trade_type")):
        rows = sc.get(key) or []
        if rows:
            out.append(table([lab, "n", "hit rate", "mean P", "note"],
                             [[e(r[lab]), e(r["n"]), f(r["hit_rate"]), f(r["mean_p_dir"]), e(r["judge"])] for r in rows]))
    if sc.get("calibration"):
        out.append("<h3>Calibration: do 70 percent calls happen 70 percent of the time?</h3>" + table(
            ["P bucket", "n", "predicted", "actual"], [[e(r["bucket"]), e(r["n"]), f(r["predicted"]), f(r["actual"])] for r in sc["calibration"]]))
    for scn, cells in (sc.get("alignment") or {}).items():
        out.append(f"<h3>Alignment matrix results vs {e(scn)}</h3>" + table(
            ["Cell", "n", "hit rate"], [[e(k), e(x["n"]), f(x["hit_rate"])] for k, x in cells.items()]))
    return "".join(out)


def render_review(rv: dict) -> str:
    m = rv["meta"]
    kind = m["kind"].title()
    out = [f"<h1>{e(kind)} review · {e(m['period'])}</h1>",
           f'<p class="mut small">{e(m["start"])} to {e(m["end"])} · run {e(m["date"])} · config {e(m["config_version"])} · ledger {e(m["ledger_version"])}</p>']
    if m.get("demo"):
        out.append('<div class="banner">DEMO DATA: synthetic prices. Plumbing check only.</div>')
    out.append("<h2>1. Outcomes: forecasts scored</h2><h3>This period</h3>" + _score_html(rv["scorecard_period"]) +
               "<h3>To date</h3>" + _score_html(rv["scorecard_to_date"]))
    hy = rv["hypotheses"]
    out.append("<h2>2. Behaviors: validated or invalidated</h2>")
    if hy["changes"]:
        out.append("<h3>Status changes since the last review</h3>" + table(
            ["Hypothesis", "From", "To", "Behavior"], [[e(c["id"]), e(c["from"]), f'<b>{e(c["to"])}</b>', e(c["text"])] for c in hy["changes"]], {3}))
    out.append(table(["Hypothesis", "Behavior", "Subject", "Metric", "Expect", "Now", "Long run", "By regime", "Status"],
                     [[e(r["id"]), e(r["text"]), e(r["subject"]), e(r["metric"]), e(r["expect"]), f(r["now"], 4), f(r["long_run"], 4),
                       e(", ".join(f"{k}: {'yes' if v else 'no'}" for k, v in (r.get("regimes") or {}).items())),
                       f'<span class="{ {"validated": "ok", "invalidated": "bad", "regime-dependent": "warn"}.get(r["status"], "mut") }">{e(r["status"])}</span>']
                      for r in hy["results"]], {1, 7}))
    pr = rv["profiles"]
    out.append("<h2>3. Personality: what changed since the last snapshot</h2>")
    if pr["changes"]:
        out.append(table(["Instrument", "Trait", "From pct", "To pct", "Reading"],
                         [[e(c["symbol"]), e(c["trait"]), f(c["from_pct"]), f(c["to_pct"]), e(c["reading"])] for c in pr["changes"]]))
    else:
        out.append('<p class="mut">No trait moved 25 percentile points or changed sign (or this is the first snapshot).</p>')
    if rv.get("personality_changes"):
        out.append("<h3>Personality changes (drift beyond ±2 for 60+ days)</h3>" + table(
            ["Date", "Instrument", "Trait", "Direction", "Status", "Suspected cause"],
            [[e(x["date"]), e(x["symbol"]), e(x["trait"]), e(x["direction"]), e(x["status"]), e(x["suspected_cause"])] for x in rv["personality_changes"]], {5}))
    if rv.get("flips"):
        out.append("<h3>Relationship flips log</h3>" + table(["Opened", "Relationship", "Corr", "5y median", "Status", "Closed"],
                                                             [[e(x["date"]), e(x["pair"]), f(x["corr"]), f(x["median_5y"]), e(x["status"]), e(x["closed"])] for x in rv["flips"]]))
    faces = rv.get("faces") or {}
    rows = []
    for s, x in faces.items():
        for reg, tr in (x.get("all") or {}).items():
            rows.append([e(s), e(reg), e(tr.get("_days")), f(tr.get("energy")), f(tr.get("conviction")), f(tr.get("fear_beta"), 4),
                         f(tr.get("rate_beta"), 4), f(tr.get("resource_beta"), 4)])
    if rows:
        out.append("<details><summary>Regime faces: each instrument's traits inside each regime</summary>" + table(
            ["Instrument", "Regime", "Days", "Energy", "Conviction", "Fear β", "Rate β", "Resource β"], rows) + "</details>")
    al = rv["alignment"]
    out.append("<h2>4. Goals: plan alignment</h2>")
    if al["pivots"]:
        out.append("".join(f'<p class="bad">Pivot trigger: {e(p["goal"])} {e(p["text"])}</p>' for p in al["pivots"]))
    out.append(table(["Goal", "Player", "Credibility", "Events (90d)", "A"],
                     [[e(k), e(a["player"]), f(a["credibility"]), e(a["events"]), f(a["A"], 4)] for k, a in al["scores"].items()]))
    lib = rv.get("event_library") or {}
    if lib:
        out.append("<h3>Event library</h3>" + table(["Type", "n", "Median |shock|", "Median persistence", "Half life"],
                                                    [[e(t), e(x["n"]), f(x["median_shock"]), f(x["median_persistence"]), f(x["half_life"])] for t, x in lib.items()]))
    out.append("<h2>5. Change control</h2>")
    pc = rv.get("param_changes") or []
    out.append(table(["Version", "Date", "Parameter", "Old", "New", "Reason", "Evidence"],
                     [[e(x["version"]), e(x["date"]), e(x["key"]), e(x["old"]), e(x["new"]), e(x["reason"]), e(x["evidence"])] for x in pc], {5, 6})
               if pc else '<p class="mut">No parameter changed this period.</p>')
    lc = rv.get("ledger_changes") or []
    if lc:
        out.append("<h3>Ledger edits</h3>" + table(["Run", "Version", "Hash", "Changed"],
                                                   [[e(x["run"]), e(x["ledger_version"]), e(x["hash"]), e(x["changed"])] for x in lc], {3}))
    st = rv.get("cycle_staleness") or []
    if st:
        out.append(f'<p class="warn">{len(st)} cycle scorecard inputs are unknown or stale: update them at primary sources.</p>')
    if m["kind"] in ("quarterly", "annual"):
        ph = rv.get("personality_history") or {}
        unit = "year" if m["kind"] == "annual" else "quarter"
        out.append(f"<h2>6. Personality over time ({unit} by {unit})</h2>"
                   f'<p class="mut">Each trait is its median over the whole {unit}, ranked against the same instrument\'s own '
                   f'{unit}s since the data starts. Shift is the average move, in robust standard deviations of its own '
                   f'history, from the previous {unit}; it is flagged as a personality change when it is at or above that '
                   f'instrument\'s own 90th percentile of shifts. '
                   f'High and low mean the top or bottom fifth of its own history.</p>')
        out.append(table(["Instrument", unit.title(), "Shift", "Shift pct", "Change", "High (top fifth)", "Low (bottom fifth)",
                          f"Moved 2+ sd since last {unit}"],
                         [[f"<b>{e(r['symbol'])}</b>", e(r["period"]), f(r["shift"], 1), f(r["shift_pct"], 0),
                           '<span class="bad">yes</span>' if r["changed"] else '<span class="mut">no</span>',
                           e(", ".join(r["high"])), e(", ".join(r["low"])), e(", ".join(r["moved"]))]
                          for r in ph.get("latest") or []], {5, 6, 7}))
        if ph.get("biggest"):
            out.append(f"<h3>Largest personality shifts on record (match these {unit}s to events)</h3>" + table(
                ["Instrument"] + [f"#{i}" for i in (1, 2, 3)],
                [[f"<b>{e(s)}</b>"] + [f'{e(x["period"])} ({f(x["shift"], 1)}){": " + e(", ".join(x["moved"])) if x["moved"] else ""}'
                                       for x in rows] for s, rows in ph["biggest"].items()], {1, 2, 3}))
        if m["kind"] == "annual" and ph.get("history"):
            years = sorted({y for h in ph["history"].values() for y in h})[-20:]
            out.append("<h3>Shift by year (higher = personality moved more that year)</h3>" + table(
                ["Instrument"] + years,
                [[f"<b>{e(s)}</b>"] + [f(h.get(y), 1) for y in years] for s, h in ph["history"].items()]))
        va = rv["validation"]
        out.append("<h2>7. Validation tests</h2>")
        lv = va["levels"]
        out.append(f'<h3>Levels versus random</h3><p>Round zones: <b>{e(lv["round"])}</b> (median z {f(lv["round_median_z"])}) · '
                   f'market-made levels: <b>{e(lv["market"])}</b> (median z {f(lv["market_median_z"])})</p>' + table(
                       ["Instrument", "Round major z", "Round mid z", "Market z", "Pain z", "p0", "Round touches"],
                       [[e(r["symbol"]), f(r["round_major_z"]), f(r["round_mid_z"]), f(r["market_z"]), f(r["pain_z"]), f(r["p0"]), e(r["round_touches"])]
                        for r in lv["rows"]]))
        out.append("<h3>Trait rankings out of sample (first 14 years vs last 6)</h3>" + table(
            ["Trait", "Instruments", "Rank corr", "Pass"],
            [[e(t), e(x["instruments"]), f(x["rank_corr"]), "" if x["pass"] is None else yes(x["pass"])] for t, x in va["traits"].items()]))
        pm = va["pair_math"]
        out.append(f'<h3>Pair math identity</h3><p>Predicted vs measured pair betas: corr {f(pm["corr"])} '
                   f'{"" if pm["pass"] is None else yes(pm["pass"])} (checks the data pipeline)</p>')
        ig = va["incentive_gap"]
        out.append(f'<h3>Incentive gap closure</h3><p>Median share of gaps beyond 2 closing within 60 days: {f(ig["median_rate"])} '
                   f'{"" if ig["pass"] is None else yes(ig["pass"])} · {e(ig["note"])}</p>' + table(
                       ["Pair", "Excursions", "Closed", "Rate"], [[e(k), e(x["excursions"]), e(x["closed"]), f(x["rate"])] for k, x in ig["pairs"].items()]))
        out.append("<h2>8. Recalibration (applied automatically)</h2>" + (table(
            ["Parameter", "Old", "New", "Note"], [[e(x.get("key")), e(x.get("old")), e(x.get("new")), e(x.get("note") or x.get("hit_rate") or "")]
                                                 for x in rv["recalibration"]]) if rv["recalibration"] else
                   '<p class="mut">Parameters are tuned in the quarterly review; the annual review does not tune them again.</p>'))
        out.append("<h2>9. Pivot triggers</h2>" + ("".join(f'<p class="bad">{e(p["trigger"])}: {e(p["action"])}</p>' for p in rv["pivots"])
                                                  or '<p class="ok">None fired.</p>'))
        out.append("<h2>10. Ledger review due</h2><p>Player weights are judgment, not measured. Revisit each one:</p>" + table(
            ["Player", "Weight", "Evidence"], [[e(x["player"]), f(x["weight"]), e(x["evidence"])] for x in rv["ledger_review"]]))
        out.append(f'<p class="mut">{e(rv.get("independence_note"))}</p>')
    return page(f"{kind} review {m['period']}", "".join(out))


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=1, default=_json_default), encoding="utf-8")


def _json_default(o):
    import numpy as np
    import pandas as pd
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp,)):
        return str(o)
    if isinstance(o, (pd.Series, pd.DataFrame)):
        return None
    return str(o)
