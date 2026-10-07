"""Demo trade journal report generators for the HTML stress layer.

SafeJournalReport: the reference for a trustworthy, self contained report.
NaiveJournalReport: PLANTED BUGS. Every shortcut that makes a journal page
lie or break; the harness and codeguard must catch each one.

Both follow the contract ``render(trades_df, meta) -> html``. The metric code
here is deliberately independent of ``stresslab.html_stress.expected_metrics``.
"""
from __future__ import annotations

import html
import json
import math

import numpy as np
import pandas as pd

from stresslab.html_stress import escape_json_for_script

_CSS = """
:root{--bg:#ffffff;--fg:#1d2330;--muted:#5f6b7a;--card:#f4f6f9;--line:#d9dee6;--pos:#16794c;--neg:#b42318;--accent:#2f5bd3}
@media (prefers-color-scheme: dark){:root{--bg:#11151c;--fg:#e6e9ef;--muted:#9aa5b4;--card:#1a202a;--line:#2c3441;--pos:#4cc38a;--neg:#ff7a6e;--accent:#7aa2ff}}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow-wrap:anywhere}
main{max-width:1100px;margin:0 auto}
h1{font-size:1.35rem;margin:0 0 4px}
.muted{color:var(--muted);font-size:.85rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:16px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.kpi .label{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}
.kpi .value{font-size:1.2rem;font-variant-numeric:tabular-nums;margin-top:2px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;margin-bottom:16px}
.chart svg{display:block;width:100%;height:120px}
.chart polyline{fill:none;stroke:var(--accent);stroke-width:1.5;vector-effect:non-scaling-stroke}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
th{background:var(--card);position:sticky;top:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.notes{white-space:normal;min-width:12ch;max-width:40ch;overflow-wrap:anywhere}
.pos{color:var(--pos)}.neg{color:var(--neg)}
"""

_SCRIPT = ("(function(){var el=document.querySelector('script[data-chart=\"equity\"]');"
           "var out=document.getElementById('chart-points');if(!el||!out){return;}"
           "try{var d=JSON.parse(el.textContent);out.textContent=String(d.length);}"
           "catch(e){out.textContent='unavailable';}})();")


def _esc(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return html.escape(str(v), quote=True)


def _num(x, fmt: str) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(x):
        return "n/a"
    if x == 0:
        x = 0.0  # never show -0
    return format(x, fmt)


def _money(x: float) -> str:
    if round(x, 2) == 0:
        return "$0.00"
    return ("-$" if x < 0 else "$") + format(abs(x), ",.2f")


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


class SafeJournalReport:
    """Escapes all text, discloses excluded rows, sorts by time, paginates honestly, works offline."""

    max_rows = 2000
    max_svg_points = 1000

    def _metrics(self, p: np.ndarray, r: np.ndarray, start: float) -> dict:
        n = len(p)
        equity = start + np.cumsum(p)
        if n:
            peak = np.maximum(np.maximum.accumulate(equity), start)
            dd = max(0.0, float(np.max((peak - equity) / peak)) * 100.0)
        else:
            dd = 0.0
        gains = math.fsum(p[p > 0])
        losses = -math.fsum(p[p < 0])
        pf = gains / losses if losses > 0 else (math.inf if gains > 0 else None)
        rf = r[np.isfinite(r)]
        return {"n": n, "net": math.fsum(p), "win_rate": 100.0 * float(np.mean(p > 0)) if n else None,
                "avg_r": float(np.mean(rf)) if len(rf) else None, "dd": dd, "pf": pf, "equity": equity}

    def _kpis(self, m: dict, excluded: int) -> str:
        pf = m["pf"]
        pf_txt = "n/a" if pf is None else ("∞" if math.isinf(pf) else format(pf, ",.3f"))
        items = [
            ("Trades", "n_trades", format(m["n"], ","), ""),
            ("Net P&amp;L", "net_pnl", _money(m["net"]), "pos" if m["net"] > 0 else ("neg" if m["net"] < 0 else "")),
            ("Win rate", "win_rate", "n/a" if m["win_rate"] is None else format(m["win_rate"], ".2f") + "%", ""),
            ("Average R", "avg_r", "n/a" if m["avg_r"] is None else format(m["avg_r"], "+,.3f"), ""),
            ("Max drawdown", "max_drawdown", format(m["dd"], ",.2f") + "%", ""),
            ("Profit factor", "profit_factor", pf_txt, ""),
            ("Excluded rows", "excluded_rows", format(excluded, ","), "neg" if excluded else ""),
        ]
        return "".join(
            f'<div class="kpi"><div class="label">{label}</div>'
            f'<div class="value {cls}" data-metric="{key}">{_esc(val)}</div></div>'
            for label, key, val, cls in items)

    def _svg(self, equity: np.ndarray) -> str:
        n = len(equity)
        if n < 2:
            return '<p class="muted">Not enough trades to draw an equity curve.</p>'
        idx = np.unique(np.linspace(0, n - 1, min(n, self.max_svg_points)).astype(int))
        y = equity[idx]
        lo, hi = float(np.min(y)), float(np.max(y))
        span = (hi - lo) or 1.0
        xs = idx / (n - 1) * 600.0
        ys = 115.0 - (y - lo) / span * 110.0
        pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in zip(xs, ys, strict=True))
        return ('<svg viewBox="0 0 600 120" preserveAspectRatio="none" role="img" aria-label="Equity curve">'
                f'<title>Equity curve</title><polyline points="{pts}"></polyline></svg>')

    def _rows(self, df: pd.DataFrame) -> str:
        recs = df.tail(self.max_rows).iloc[::-1].to_dict("records")
        return "".join(
            "<tr>"
            f"<td>{_esc(_utc(r['time']).strftime('%Y-%m-%d %H:%M'))}</td>"
            f"<td>{_esc(r['symbol'])}</td><td>{_esc(r['side'])}</td>"
            f"<td class=\"num\">{_num(r['entry'], ',.5f')}</td><td class=\"num\">{_num(r['exit'], ',.5f')}</td>"
            f"<td class=\"num\">{_num(r['units'], ',.6g')}</td>"
            f"<td class=\"num {_pnl_cls(r['pnl'])}\">{_num(r['pnl'], ',.2f')}</td>"
            f"<td class=\"num\">{_num(r['r_multiple'], '+,.2f')}</td>"
            f"<td>{_esc(r['strategy'])}</td><td class=\"notes\">{_esc(r['notes'])}</td>"
            "</tr>" for r in recs)

    def render(self, trades: pd.DataFrame, meta: dict) -> str:
        start = float(meta.get("start_equity", 0.0))
        pnl = pd.to_numeric(trades["pnl"], errors="coerce").to_numpy(dtype=float)
        keep = np.isfinite(pnl)
        excluded = int((~keep).sum())
        df = trades.loc[keep].copy()
        df["pnl"] = pnl[keep]
        df = df.sort_values("time", kind="mergesort").reset_index(drop=True)
        p = df["pnl"].to_numpy(dtype=float)
        r = pd.to_numeric(df["r_multiple"], errors="coerce").to_numpy(dtype=float)
        m = self._metrics(p, r, start)
        n = m["n"]
        times = [_utc(t).strftime("%Y-%m-%dT%H:%M:%SZ") for t in df["time"]]
        chart = escape_json_for_script([{"t": t, "equity": float(e)} for t, e in zip(times, m["equity"], strict=True)])
        total_attr = f' data-total="{n}"' if n > self.max_rows else ""
        shown = min(n, self.max_rows)
        note = (f"Showing the latest {shown:,} of {n:,} trades." if n > self.max_rows else f"{n:,} trades.")
        excl_note = (f" {excluded:,} row(s) with missing or non-finite P&amp;L are excluded from every figure."
                     if excluded else "")
        title = _esc(meta.get("title", "Trade journal"))
        head = ("<tr><th scope=\"col\">Time (UTC)</th><th scope=\"col\">Symbol</th><th scope=\"col\">Side</th>"
                "<th scope=\"col\">Entry</th><th scope=\"col\">Exit</th><th scope=\"col\">Units</th>"
                "<th scope=\"col\">P&amp;L</th><th scope=\"col\">R</th><th scope=\"col\">Strategy</th>"
                "<th scope=\"col\">Notes</th></tr>")
        body = self._rows(df) if n else ""
        empty = "" if n else '<p class="muted">No trades to show.</p>'
        return (
            "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{title}</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{title}</h1><p class=\"muted\">All times UTC. Starting equity {_esc(_money(start))}.{excl_note}</p>"
            f"<section class=\"kpis\" aria-label=\"Key figures\">{self._kpis(m, excluded)}</section>"
            f"<section class=\"chart\" aria-label=\"Equity\">{self._svg(m['equity'])}"
            "<p class=\"muted\">Chart points: <span id=\"chart-points\">0</span></p></section>"
            f"<p class=\"muted\">{note}</p>"
            f"<div class=\"table-wrap\"><table data-table=\"trades\"{total_attr}><thead>{head}</thead>"
            f"<tbody>{body}</tbody></table></div>{empty}"
            f"<script type=\"application/json\" data-chart=\"equity\">{chart}</script>"
            f"<script>{_SCRIPT}</script>"
            "</main></body></html>\n")


def _pnl_cls(x) -> str:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return ""
    return "pos" if x > 0 else ("neg" if x < 0 else "")


class NaiveJournalReport:
    """PLANTED BUGS for harness self tests. Do not copy any of this."""

    def render(self, trades: pd.DataFrame, meta: dict) -> str:
        start = meta["start_equity"]
        pnl = trades["pnl"].to_numpy(dtype=float)
        with np.errstate(all="ignore"):
            net = np.sum(pnl)                                    # BUG: one NaN poisons the total
            win_rate = np.float64((pnl >= 0).sum()) / len(pnl) * 100   # BUG: breakeven is a win; NaN rows in denominator
            equity = pd.Series(start + np.cumsum(pnl))
            peak = equity.cummax()                               # BUG: peak ignores the starting equity
            dd = ((peak - equity) / peak).max() * 100
            wins = pnl[pnl > 0].sum()
            losses = abs(pnl[pnl < 0].sum())
            pf = np.float64(wins) / losses                       # BUG: divides by zero with no losses
            avg_r = np.mean(trades["r_multiple"].to_numpy(dtype=float))  # BUG: inf R poisons the mean
        rows = "<tr><td>Time</td><td>Symbol</td><td>Side</td><td>P&L</td><td>R</td><td>Strategy</td><td>Notes</td></tr>"
        for r in trades.head(1000).to_dict("records"):          # BUG: silent truncation, no data-total
            rows += (f"<tr><td>{r['time']}</td><td>{r['symbol']}</td><td>{r['side']}</td>"   # BUG: not escaped
                     f"<td>{r['pnl']:.2f}</td><td>{r['r_multiple']:.2f}</td><td>{r['strategy']}</td>"
                     f"<td>{r['notes']}</td></tr>")
        points = [{"t": str(t), "equity": e} for t, e in zip(trades["time"], equity, strict=True)]  # BUG: unsorted
        chart = json.dumps(points)                               # BUG: writes NaN, no </script> escaping
        return (f"<html><head><title>{meta['title']}</title>"                                     # BUG: no lang/charset/viewport
                "<script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script></head><body>"    # BUG: CDN dependency
                f"<h1>{meta['title']}</h1>"
                f"<p>Trades: <b data-metric=\"n_trades\">{len(trades)}</b> "
                f"Net P&L: <b data-metric=\"net_pnl\">{net:,.2f}</b> "
                f"Win rate: <b data-metric=\"win_rate\">{win_rate:.1f}%</b> "
                f"Avg R: <b data-metric=\"avg_r\">{avg_r:.2f}</b> "
                f"Max DD: <b data-metric=\"max_drawdown\">{dd:.2f}%</b> "
                f"PF: <b data-metric=\"profit_factor\">{pf:.2f}</b></p>"
                f"<table data-table=\"trades\">{rows}</table>"
                "<canvas id=\"c\"></canvas>"
                f"<script type=\"application/json\" data-chart=\"equity\">{chart}</script>"
                "<script>new Chart(document.getElementById('c'), {type: 'line'});</script>"  # BUG: throws offline
                "</body></html>")
