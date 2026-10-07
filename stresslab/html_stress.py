"""HTML report stress layer.

Report generator contract: ``render(trades_df, meta) -> html_str``.
Trades columns: time (UTC), symbol, side, entry, exit, units, pnl, r_multiple,
strategy, notes. Meta: title, start_equity.

Page tagging contract (what the checks read):

* ``<x data-metric="NAME">`` for n_trades, net_pnl, win_rate (percent), avg_r,
  max_drawdown (percent), profit_factor, excluded_rows.
* ``<table data-table="trades" data-total="N">`` (data-total when paginated).
* ``<script type="application/json" data-chart="equity">[{"t": ..., "equity": ...}]</script>``.

``expected_metrics`` is an INDEPENDENT oracle: it shares no code with any
report generator.
"""
from __future__ import annotations

import glob
import json
import math
import os
import re
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction

from stresslab.result import FAIL, PASS, WARN, worst

MARKER = "XSSMARK"
METRICS = ("n_trades", "net_pnl", "win_rate", "avg_r", "max_drawdown", "profit_factor", "excluded_rows")
TRADE_COLUMNS = ["time", "symbol", "side", "entry", "exit", "units", "pnl", "r_multiple", "strategy", "notes"]
BAD_TOKENS_RE = re.compile(r"(?<![\w.])(-inf|nan|NaN|inf|Infinity|None|NaT|undefined|null)(?![\w])|\[object Object\]")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track",
        "wbr", "keygen"}
OPTIONAL_END = {"p", "li", "td", "th", "tr", "thead", "tbody", "tfoot", "option", "optgroup", "dt", "dd",
                "colgroup", "caption", "rp", "rt", "html", "head", "body"}
LOCAL_PATH_RE = re.compile(r"(file://|/home/[\w.-]+/|/Users/[\w.-]+/|[A-Za-z]:\\(?:Users|Documents and Settings)\\)")


# =================================================================== datasets
def _base_trades(n: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    gaps = rng.integers(1, 48, n)
    t = pd.Timestamp("2024-01-02 08:00", tz="UTC") + pd.to_timedelta(np.cumsum(gaps), unit="h")
    symbols = np.array(["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US500"])
    strategies = np.array(["breakout", "pullback", "news fade"])
    notes = np.array(["clean break", "late entry", "stopped at noise", "trailed", ""])
    entry = np.round(1.10 + rng.normal(0, 0.01, n), 5)
    side = rng.choice(np.array(["long", "short"]), n)
    move = rng.normal(0.0002, 0.003, n)
    exit_ = np.round(entry * (1 + move), 5)
    units = rng.choice(np.array([1000.0, 5000.0, 10000.0, 20000.0]), n)
    pnl = np.round(rng.normal(15, 160, n), 2)
    return pd.DataFrame({"time": t, "symbol": rng.choice(symbols, n), "side": side, "entry": entry, "exit": exit_,
                         "units": units, "pnl": pnl, "r_multiple": np.round(pnl / 100.0, 4),
                         "strategy": rng.choice(strategies, n), "notes": rng.choice(notes, n)})[TRADE_COLUMNS]


def datasets(seed: int = 0) -> dict:
    """Twelve named ``(trades, meta)`` pairs covering the ways a journal page breaks."""
    def meta(name):
        return {"title": f"Trade journal: {name}", "start_equity": 100_000.0}

    rng = np.random.default_rng(seed + 99)
    out = {"normal_200": _base_trades(200, seed + 1), "empty": _base_trades(0, seed + 2)}

    single = _base_trades(1, seed + 3)
    single.loc[0, ["pnl", "r_multiple"]] = [-87.25, -0.8725]
    out["single_trade"] = single

    losers = _base_trades(25, seed + 4)
    losers["pnl"] = -(losers["pnl"].abs() + 1.0)
    losers["r_multiple"] = losers["pnl"] / 100.0
    out["all_losers"] = losers

    winners = _base_trades(25, seed + 5)
    winners["pnl"] = winners["pnl"].abs() + 1.0
    winners["r_multiple"] = winners["pnl"] / 100.0
    out["all_winners"] = winners

    missing = _base_trades(60, seed + 6)
    missing.loc[[5, 17, 42], "pnl"] = np.nan
    missing.loc[30, "r_multiple"] = np.inf
    missing.loc[[8, 9], "notes"] = None
    out["missing_values"] = missing

    hostile = _base_trades(12, seed + 7)
    payloads = [f"<script>window.{MARKER}=1</script>",
                f"<img src=x onerror=\"window.{MARKER}=1\">",
                f"</script><script>window.{MARKER}=1</script>",
                f"\"><b>{MARKER}</b>",
                f"<a href=\"javascript:window.{MARKER}=1\">click</a>",
                f"' onmouseover='window.{MARKER}=1",
                f"<svg onload=window.{MARKER}=1>",
                f"<style>body{{background:url(https://evil.invalid/{MARKER})}}</style>"]
    hostile["notes"] = [payloads[i % len(payloads)] for i in range(len(hostile))]
    hostile["symbol"] = [f"EUR<b>{MARKER}</b>USD" if i % 3 == 0 else s for i, s in enumerate(hostile["symbol"])]
    hostile["strategy"] = [f"breakout\"><img src=x onerror=window.{MARKER}=1>" if i % 4 == 1 else s
                           for i, s in enumerate(hostile["strategy"])]
    out["hostile_text"] = hostile

    uni = _base_trades(10, seed + 8)
    uni["notes"] = ["rocket \U0001F680 to the moon \U0001F4C8", "\u0635\u0641\u0642\u0629 \u0631\u0627\u0628\u062d\u0629",
                    "\u5229\u76ca\u78ba\u5b9a \u6b62\u640d", "zero\u200bwidth\u200djoiner", "cafe\u0301 re\u0301sume\u0301",
                    "x" * 5000, "\U0001F1EF\U0001F1F5 flags", "mixed \u0627\u0644\u0639\u0631\u0628\u064a\u0629 and English",
                    "\u00a0nbsp\u00a0", "tab\tseparated"]
    uni["symbol"] = ["EUR\u2215USD" if i == 0 else s for i, s in enumerate(uni["symbol"])]
    out["unicode_long_text"] = uni

    ext = _base_trades(6, seed + 9)
    ext["pnl"] = [-9.9e11, 1.5e12, 1e-9, -0.0, 250.0, -120.0]
    ext["r_multiple"] = [-9.9e9, 1.5e10, 1e-11, -0.0, 2.5, -1.2]
    ext["units"] = [1e9, 1e9, 1e-6, 1.0, 1000.0, 1000.0]
    out["extreme_values"] = ext

    dup = _base_trades(80, seed + 10)
    for i in range(5, 80, 9):
        dup.loc[i, "time"] = dup.loc[i - 1, "time"]
    out["unsorted_duplicates"] = dup.iloc[rng.permutation(len(dup))].reset_index(drop=True)

    out["scale_5k"] = _base_trades(5_000, seed + 11)
    out["scale_50k"] = _base_trades(50_000, seed + 12)
    return {k: (v, meta(k)) for k, v in out.items()}


# =================================================================== oracle
def expected_metrics(trades: pd.DataFrame, start_equity: float) -> dict:
    """Independent oracle for the headline metrics.

    Non finite pnl rows are dropped and counted as ``excluded_rows``. Trades are
    sorted by time (stable). ``win_rate`` counts pnl > 0 only and is a percent.
    The drawdown peak INCLUDES the starting equity. ``profit_factor`` is inf
    when there are gains and no losses, None when there is nothing to divide.
    """
    pnl_all = pd.to_numeric(trades["pnl"], errors="coerce").to_numpy(dtype=float)
    ok = np.isfinite(pnl_all)
    valid = trades.loc[ok].copy()
    valid["_pnl"] = pnl_all[ok]
    valid = valid.sort_values("time", kind="mergesort")
    p = valid["_pnl"].to_numpy(dtype=float)
    n = int(len(p))
    r = pd.to_numeric(valid["r_multiple"], errors="coerce").to_numpy(dtype=float)
    r = r[np.isfinite(r)]
    equity = float(start_equity) + np.cumsum(p)
    peak = np.maximum.accumulate(np.concatenate([[float(start_equity)], equity]))[1:] if n else np.array([])
    dd = float(np.max((peak - equity) / peak) * 100.0) if n else 0.0
    gains = math.fsum(x for x in p if x > 0)
    losses = -math.fsum(x for x in p if x < 0)
    if losses > 0:
        pf = gains / losses
    elif gains > 0:
        pf = math.inf
    else:
        pf = None
    return {
        "n_trades": n,
        "net_pnl": math.fsum(p),
        "win_rate": (100.0 * float((p > 0).sum()) / n) if n else None,
        "avg_r": float(math.fsum(r) / len(r)) if len(r) else None,
        "max_drawdown": max(dd, 0.0),
        "profit_factor": pf,
        "excluded_rows": int((~ok).sum()),
        "final_equity": float(start_equity) + math.fsum(p),
    }


# =================================================================== number parsing
_NA = {"n/a", "na", "\u2014", "\u2013", "-", ""}
_SUFFIX = {"k": 3, "K": 3, "M": 6, "bn": 9, "BN": 9, "B": 9}
_NUM_RE = re.compile(r"(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?")


def parse_number(text) -> tuple:
    """Parse a displayed number. Returns ``(value, decimals)``.

    ``decimals`` is the power of ten of the last displayed digit, negated, so
    the display resolution is ``10 ** -decimals`` (suffixes and scientific
    exponents included). Handles $ \u00a3 \u20ac, thousands commas, accounting
    parentheses, unicode minus, %, scientific notation, k/M/bn suffixes,
    the infinity sign and n/a. Raises ValueError for anything else
    (for example "nan" or "inf").
    """
    s = str(text).strip()
    for ch in (" ", "\xa0", "\u202f", "\u2009", "\u200b"):
        s = s.replace(ch, "")
    s = s.replace("\u2212", "-").replace("\u2012", "-").replace("\u2013", "-") if len(s) > 1 else s
    if s.lower() in _NA:
        return None, None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    for _ in range(2):  # sign and currency in either order: -$5, $-5
        if s[:1] in ("+", "-"):
            neg ^= s[0] == "-"
            s = s[1:]
        if s[:1] in ("$", "\u00a3", "\u20ac"):
            s = s[1:]
    if s.endswith("%"):
        s = s[:-1]
    if s in ("\u221e",):
        return (-math.inf if neg else math.inf), None
    exp10 = 0
    for suf in ("bn", "BN", "k", "K", "M", "B"):
        if s.endswith(suf) and len(s) > len(suf):
            exp10 = _SUFFIX[suf]
            s = s[: -len(suf)]
            break
    s = s.replace(",", "")
    m = _NUM_RE.fullmatch(s)
    if not m:
        raise ValueError(f"not a displayed number: {text!r}")
    dec = len(m.group(2) or "")
    e = int(m.group(3) or 0)
    value = float(s) * (10 ** exp10)
    return (-value if neg else value), dec - e - exp10


# =================================================================== soup helpers
def _soup(page) -> BeautifulSoup:
    return page if isinstance(page, BeautifulSoup) else BeautifulSoup(page, "html.parser")


def _visible_text(soup: BeautifulSoup) -> str:
    """Text a reader sees: no scripts, styles, templates or comments (the soup is not modified)."""
    parts = []
    for s in soup.find_all(string=True):
        if isinstance(s, (Comment, Doctype, Declaration, ProcessingInstruction)):
            continue
        if s.parent is not None and s.parent.name in ("script", "style", "template", "noscript"):
            continue
        parts.append(str(s))
    return " ".join(parts)


def _is_exec_script(tag) -> bool:
    typ = (tag.get("type") or "").strip().lower()
    return typ in ("", "text/javascript", "application/javascript", "module", "text/ecmascript")


# =================================================================== checks
class _Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        return None

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        names = [t for t, _ in self.stack]
        if tag not in names:
            self.problems.append(f"stray </{tag}> at line {self.getpos()[0]}")
            return
        while self.stack:
            t, line = self.stack.pop()
            if t == tag:
                break
            if t not in OPTIONAL_END:
                self.problems.append(f"<{t}> opened at line {line} is not closed before </{tag}>")


def check_structure(page) -> list:
    """Returns [(status, message)]: tag balance and duplicate ids FAIL; missing title/charset WARN."""
    raw = page if isinstance(page, str) else str(page)
    out = []
    bal = _Balance()
    bal.feed(raw)
    bal.close()
    out += [(FAIL, p) for p in bal.problems[:10]]
    out += [(FAIL, f"<{t}> opened at line {ln} is never closed") for t, ln in bal.stack if t not in OPTIONAL_END][:10]
    soup = _soup(page)
    counts = Counter(t.get("id") for t in soup.find_all(id=True))
    dups = sorted(i for i, c in counts.items() if c > 1)
    if dups:
        out.append((FAIL, f"duplicate ids: {dups[:5]}"))
    title = soup.find("title")
    if title is None or not title.get_text(strip=True):
        out.append((WARN, "missing or empty <title>"))
    has_charset = soup.find("meta", attrs={"charset": True}) is not None or any(
        "charset" in (m.get("content") or "").lower() for m in soup.find_all("meta", attrs={"http-equiv": True}))
    if not has_charset:
        out.append((WARN, "no <meta charset>; browsers may guess the encoding"))
    return out


def check_bad_tokens(page) -> list[str]:
    text = _visible_text(_soup(page))
    hits = BAD_TOKENS_RE.findall(text)
    found = sorted({h if isinstance(h, str) else h for h in hits} - {""})
    if "[object Object]" in text:
        found.append("[object Object]")
    return [f"visible text contains {tok!r}" for tok in found]


def check_injection(page, marker: str = MARKER) -> list[str]:
    soup = _soup(page)
    problems = []
    for s in soup.find_all("script"):
        body = s.string or s.get_text() or ""
        if _is_exec_script(s):
            if marker in body:
                problems.append("marker inside an executable <script>")
        else:
            if "</script" in body.lower():
                problems.append("'</script' inside a JSON data block")
            if (s.get("type") or "").lower().endswith("json") and body.strip():
                try:
                    json.loads(body)
                except ValueError:
                    problems.append(f"JSON data block {dict(s.attrs).get('data-chart', '')!s} does not parse "
                                    "(broken out of its <script>?)")
    for tag in soup.find_all(True):
        for k, v in tag.attrs.items():
            val = " ".join(v) if isinstance(v, list) else str(v)
            if marker not in val:
                continue
            kl = k.lower()
            if kl.startswith("on") or kl in ("href", "src", "action", "style", "formaction", "srcdoc", "xlink:href"):
                problems.append(f"marker in <{tag.name} {k}=...>")
            elif tag.name in ("img", "script", "iframe", "svg", "object", "embed", "video", "audio", "link"):
                problems.append(f"live <{tag.name}> element carries the marker")
    for tag in soup.find_all(["b", "img", "iframe", "svg", "object", "embed", "style"]):
        if tag.name == "b" and marker in tag.get_text():
            problems.append("marker rendered inside a real <b> element (markup was not escaped)")
        elif tag.name == "style" and marker in (tag.string or ""):
            problems.append("marker inside an injected <style> element")
    return sorted(set(problems))


def check_offline(page, allow_external: bool = False) -> list[str]:
    soup = _soup(page)
    probs = []

    def external(u):
        u = (u or "").strip().lower()
        return u.startswith(("http:", "https:", "//", "ftp:"))
    for tag, attr in (("script", "src"), ("img", "src"), ("iframe", "src"), ("audio", "src"), ("video", "src"),
                      ("source", "src"), ("embed", "src"), ("object", "data"), ("track", "src")):
        probs += [f"external <{tag} {attr}={t.get(attr)}>" for t in soup.find_all(tag) if external(t.get(attr))]
    for t in soup.find_all("link"):
        rel = " ".join(t.get("rel") or []).lower()
        if ("stylesheet" in rel or "preload" in rel or "icon" in rel or "import" in rel) and external(t.get("href")):
            probs.append(f"external <link rel={rel} href={t.get('href')}>")
    css = " ".join(s.get_text() for s in soup.find_all("style")) + " " + " ".join(
        t.get("style", "") for t in soup.find_all(style=True))
    probs += [f"external css url({u})" for u in re.findall(r"url\(\s*['\"]?([^)'\"]+)", css) if external(u)]
    raw = str(soup)
    m = LOCAL_PATH_RE.search(raw)
    if m:
        probs.append(f"leaked local file path near {raw[max(0, m.start() - 20):m.end() + 30]!r}")
    if allow_external:
        return []
    return probs


def _metric_problem(name, shown, exp):
    try:
        v, dec = parse_number(shown)
    except ValueError:
        return f"{name}: cannot parse displayed {shown!r} (expected {exp})"
    if exp is None:
        return None if v is None else f"{name}: shows {shown!r} but the metric is undefined (expected n/a)"
    if v is None:
        return f"{name}: shows {shown!r} but expected {exp:.6g}"
    if math.isinf(exp) or math.isinf(v):
        return None if v == exp else f"{name}: shows {shown!r} but expected {exp}"
    tol = 0.5 * 10.0 ** (-dec) + 1e-9 * abs(exp)
    if abs(v - exp) > tol:
        return f"{name}: shows {shown!r} = {v:.10g}, expected {exp:.10g} (tolerance {tol:.3g})"
    return None


def check_reconcile(page, expected: dict):
    """Returns (problems, n_checked, untagged). Untagged metrics are not failures, except
    excluded rows that exist but are not disclosed."""
    soup = _soup(page)
    problems, untagged, n_checked = [], [], 0
    for name in METRICS:
        els = soup.find_all(attrs={"data-metric": name})
        if not els:
            untagged.append(name)
            if name == "excluded_rows" and expected.get("excluded_rows", 0) > 0:
                problems.append(f"{expected['excluded_rows']} rows with non finite P&L were dropped but the page "
                                "does not disclose it (no data-metric=excluded_rows)")
            continue
        for el in els:
            n_checked += 1
            p = _metric_problem(name, el.get_text(" ", strip=True), expected.get(name))
            if p:
                problems.append(p)
    return problems, n_checked, untagged


def _trade_rows(table):
    rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
    body = [r for r in rows if r.find("th") is None and r.find_parent("thead") is None]
    if table.find("th") is None and body and not re.search(r"\d", body[0].get_text()):
        body = body[1:]  # a <td> header row: treat it as the header (a11y flags the missing <th>)
    return body


def check_table(page, expected_n: int):
    """Returns (status, message)."""
    soup = _soup(page)
    table = soup.find("table", attrs={"data-table": "trades"})
    if table is None:
        return WARN, "no <table data-table=\"trades\">; row count not verified"
    shown = len(_trade_rows(table))
    total = table.get("data-total")
    if total is not None:
        try:
            total_n = int(str(total).replace(",", ""))
        except ValueError:
            return FAIL, f"data-total={total!r} is not an integer"
        if total_n != expected_n:
            return FAIL, f"data-total={total_n} but there are {expected_n} valid trades"
        if shown > total_n:
            return FAIL, f"{shown} rows shown but data-total={total_n}"
        return PASS, f"{shown} of {total_n} rows shown, total disclosed"
    if shown != expected_n:
        return FAIL, f"table shows {shown} rows, expected {expected_n} valid trades (silent truncation or padding)"
    return PASS, f"{shown} rows"


def _raise_constant(c):
    raise ValueError(f"non standard JSON constant {c} (JSON.parse rejects it)")


def check_chart(page, expected: dict):
    """Returns (status, message)."""
    soup = _soup(page)
    el = soup.find("script", attrs={"data-chart": "equity"})
    if el is None:
        return WARN, "no <script data-chart=\"equity\"> block; chart not verified"
    try:
        data = json.loads(el.string or el.get_text() or "", parse_constant=_raise_constant)
    except ValueError as e:
        return FAIL, f"chart JSON invalid for a browser: {e}"
    if not isinstance(data, list):
        return FAIL, "chart JSON is not a list"
    n = expected["n_trades"]
    if len(data) != n:
        return FAIL, f"chart has {len(data)} points, expected {n}"
    if not n:
        return PASS, "empty chart for empty journal"
    try:
        ts = pd.to_datetime([d["t"] for d in data], utc=True, format="ISO8601")
        eq = [float(d["equity"]) for d in data]
    except (KeyError, TypeError, ValueError) as e:
        return FAIL, f"chart points need t and equity: {e}"
    if not ts.is_monotonic_increasing:
        return FAIL, "chart timestamps are not in time order"
    final = expected["final_equity"]
    if not math.isclose(eq[-1], final, rel_tol=1e-9, abs_tol=0.01):
        return FAIL, f"last equity {eq[-1]:.6g} != start + net P&L {final:.6g}"
    return PASS, f"{n} points, ordered, ends at start + net P&L"


def check_timezone(page) -> list[str]:
    text = _visible_text(_soup(page))
    return [] if re.search(r"\bUTC\b", text) else ["page never states the timezone (expected 'UTC')"]


def check_a11y(page) -> list[str]:
    soup = _soup(page)
    probs = []
    html_tag = soup.find("html")
    if html_tag is None or not html_tag.get("lang"):
        probs.append("<html> has no lang attribute")
    for t in soup.find_all("table"):
        if t.find("th") is None:
            probs.append("a table has no <th> header cells")
            break
    if any(not img.has_attr("alt") for img in soup.find_all("img")):
        probs.append("<img> without alt text")
    if soup.find("meta", attrs={"name": "viewport"}) is None:
        probs.append("no <meta name=viewport>; unreadable on phones")
    return probs


def _strip_generated(page: str) -> str:
    soup = BeautifulSoup(page, "html.parser")
    for t in soup.find_all(attrs={"data-generated": True}):
        t.decompose()
    return str(soup)


# =================================================================== browser
def _launch_chromium(p):
    try:
        return p.chromium.launch()
    except Exception as first:  # noqa: BLE001  fall back to a pre-installed Chromium
        roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""), str(Path.home() / ".cache" / "ms-playwright")]
        cands = [os.environ.get("STRESSLAB_CHROMIUM", "")]
        for r in roots:
            if r:
                cands += sorted(glob.glob(os.path.join(r, "chromium-*", "chrome-linux*", "chrome")), reverse=True)
        for c in cands:
            if c and os.path.exists(c):
                return p.chromium.launch(executable_path=c)
        raise first


class BrowserSession:
    """One headless Chromium for many pages. Use as a context manager."""

    def __init__(self):
        self._pw = self._browser = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = _launch_chromium(self._pw)
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def check(self, page_html: str, timeout_ms: int = 30_000) -> dict:
        ctx = self._browser.new_context(viewport={"width": 390, "height": 844})
        pg = ctx.new_page()
        console, errors, blocked = [], [], []
        pg.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))

        def route(r):
            url = r.request.url
            if url.startswith(("http:", "https:")):
                blocked.append(url)
                r.abort()
            else:
                r.continue_()
        pg.route("**/*", route)
        try:
            pg.set_content(page_html, wait_until="load", timeout=timeout_ms)
            pg.wait_for_timeout(150)
            xss = bool(pg.evaluate(f"() => !!window.{MARKER}"))
            overflow = bool(pg.evaluate(
                "() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"))
        finally:
            ctx.close()
        console = [c for c in console if not c.startswith("Failed to load resource")]
        return {"xss_executed": xss, "page_errors": errors, "console_errors": console, "blocked_requests": blocked,
                "horizontal_overflow": overflow}


def browser_checks(page_html: str) -> dict:
    """Render one page in headless Chromium at 390x844 with all http(s) requests blocked.

    Returns {"skipped": reason} if Playwright or Chromium is unavailable.
    """
    try:
        with BrowserSession() as b:
            return b.check(page_html)
    except ImportError:
        return {"skipped": "playwright not installed (pip install playwright)"}
    except Exception as e:  # noqa: BLE001  missing browser binary and similar
        return {"skipped": f"browser unavailable: {type(e).__name__}: {str(e).splitlines()[0][:150]}"}


def _browser_rows(res: dict, allow_external: bool) -> list[tuple]:
    if "skipped" in res:
        return [("browser", WARN, res["skipped"])]
    rows = [("browser:xss", FAIL, "INJECTED SCRIPT EXECUTED (window.XSSMARK set)") if res["xss_executed"]
            else ("browser:xss", PASS, "no injected script ran")]
    errs = res["page_errors"] + res["console_errors"]
    rows.append(("browser:js_errors", FAIL, "; ".join(errs)[:300]) if errs else ("browser:js_errors", PASS, "no errors"))
    if res["blocked_requests"]:
        rows.append(("browser:network", WARN if allow_external else FAIL,
                     f"{len(res['blocked_requests'])} external request(s) blocked: {res['blocked_requests'][0]}"))
    else:
        rows.append(("browser:network", PASS, "no external requests"))
    rows.append(("browser:overflow", WARN, "page scrolls horizontally at 390px") if res["horizontal_overflow"]
                else ("browser:overflow", PASS, "fits 390px"))
    return rows


# =================================================================== runner
@dataclass(frozen=True)
class ReportBudget:
    max_seconds_5k: float = 2.0
    max_seconds_50k: float = 15.0
    max_mb_50k: float = 40.0
    max_growth_exponent: float = 1.3


def _render_fn(generator):
    return generator.render if hasattr(generator, "render") else generator


def check_page(page: str, trades: pd.DataFrame, meta: dict, expected: dict | None = None,
               allow_external: bool = False) -> list[tuple]:
    """All static checks on one rendered page. Returns [(check, status, detail)]."""
    exp = expected or expected_metrics(trades, meta["start_equity"])
    soup = BeautifulSoup(page, "html.parser")
    rows = []
    st = check_structure(page)
    rows.append(("structure", worst(s for s, _ in st), "; ".join(m for _, m in st)[:300] or "balanced, title, charset"))
    bt = check_bad_tokens(soup)
    rows.append(("bad_tokens", FAIL if bt else PASS, "; ".join(bt) or "no nan/inf/None/undefined in visible text"))
    inj = check_injection(soup)
    rows.append(("injection", FAIL if inj else PASS, "; ".join(inj) or "hostile text inert"))
    off = check_offline(soup, allow_external)
    rows.append(("offline", FAIL if off else PASS, "; ".join(off)[:300] or "self contained"))
    probs, n_checked, untagged = check_reconcile(soup, exp)
    rows.append(("reconcile", FAIL if probs else PASS, "; ".join(probs)[:400] or f"{n_checked} tagged values match"))
    rows.append(("metric_coverage", WARN if untagged else PASS,
                 f"not tagged, so not verified: {untagged}" if untagged else "all metrics tagged"))
    rows.append(("table", *check_table(soup, exp["n_trades"])))
    rows.append(("chart", *check_chart(soup, exp)))
    tz = check_timezone(soup)
    rows.append(("timezone", WARN if tz else PASS, "; ".join(tz) or "states UTC"))
    a11 = check_a11y(soup)
    rows.append(("a11y", WARN if a11 else PASS, "; ".join(a11) or "lang, th, alt, viewport"))
    return rows


class HtmlReportStress:
    def __init__(self, generator, budget: ReportBudget | None = None, browser: bool = False,
                 allow_external: bool = False, seed: int = 0, only=None):
        self.render = _render_fn(generator)
        self.budget = budget or ReportBudget()
        self.browser = browser
        self.allow_external = allow_external
        self.seed = seed
        self.only = set(only) if only else None
        self.pages: dict[str, str] = {}

    def run(self) -> pd.DataFrame:
        rows = []
        timing, sizes = {}, {}
        for name, (trades, meta) in datasets(self.seed).items():
            if self.only and name not in self.only:
                continue
            exp = expected_metrics(trades, meta["start_equity"])
            t0 = time.perf_counter()
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)  # numeric warnings are judged via the page
                    page = self.render(trades.copy(), dict(meta))
            except Exception as e:  # noqa: BLE001  a crashing generator is a FAIL for that dataset
                rows.append((name, "render", FAIL, f"render raised {type(e).__name__}: {str(e)[:200]}"))
                continue
            timing[name] = time.perf_counter() - t0
            sizes[name] = len(page.encode("utf-8")) / 1e6
            self.pages[name] = page
            rows.append((name, "render", PASS, f"{timing[name]:.3f}s, {sizes[name]:.2f} MB"))
            rows += [(name, *r) for r in check_page(page, trades, meta, exp, self.allow_external)]
            if not name.startswith("scale"):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    again = self.render(trades.copy(), dict(meta))
                same = _strip_generated(again) == _strip_generated(page)
                rows.append((name, "determinism", PASS if same else FAIL,
                             "identical on re-render" if same else "output changes between identical renders"))
        rows += self._scale_rows(timing, sizes)
        if self.browser:
            rows += self._browser_pass()
        return pd.DataFrame(rows, columns=["dataset", "check", "status", "detail"])

    def _scale_rows(self, timing, sizes):
        b, out = self.budget, []
        if "scale_5k" in timing:
            t = timing["scale_5k"]
            out.append(("scale_5k", "scale", FAIL if t > b.max_seconds_5k else PASS,
                        f"{t:.3f}s (budget {b.max_seconds_5k}s), {sizes['scale_5k']:.2f} MB"))
        if "scale_50k" in timing:
            t, mb = timing["scale_50k"], sizes["scale_50k"]
            probs = []
            if t > b.max_seconds_50k:
                probs.append(f"{t:.2f}s over budget {b.max_seconds_50k}s")
            if mb > b.max_mb_50k:
                probs.append(f"{mb:.1f} MB over budget {b.max_mb_50k} MB")
            detail = f"{t:.3f}s, {mb:.2f} MB"
            if "scale_5k" in timing and timing["scale_5k"] > 0.02:
                g = math.log(t / timing["scale_5k"]) / math.log(10)
                detail += f", growth exponent {g:.2f}"
                if g > b.max_growth_exponent and t > 1.0:
                    probs.append(f"growth exponent {g:.2f} > {b.max_growth_exponent} (superlinear)")
            out.append(("scale_50k", "scale", FAIL if probs else PASS, "; ".join(probs) or detail))
        return out

    def _browser_pass(self):
        rows = []
        try:
            with BrowserSession() as b:
                for name, page in self.pages.items():
                    rows += [(name, *r) for r in _browser_rows(b.check(page), self.allow_external)]
        except ImportError:
            rows.append(("all", "browser", WARN, "playwright not installed; browser checks skipped"))
        except Exception as e:  # noqa: BLE001
            rows.append(("all", "browser", WARN, f"browser unavailable, checks skipped: {str(e).splitlines()[0][:150]}"))
        return rows


# =================================================================== files not generated in Python
def _decode(raw: bytes) -> tuple[str, str, list]:
    issues = []
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16 (BOM)", [(WARN, "encoding", "UTF-16 file; most tools expect UTF-8")]
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8 (BOM)", issues
    try:
        return raw.decode("utf-8"), "utf-8", issues
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        return text, "utf-8 (invalid bytes replaced)", [(FAIL, "encoding", "file is not valid UTF-8")]


def _list_tables(text: str, soup) -> list[dict]:
    try:
        from io import StringIO
        frames = pd.read_html(StringIO(text))
        return [{"index": i, "rows": int(f.shape[0]), "cols": int(f.shape[1])} for i, f in enumerate(frames)]
    except (ImportError, ValueError):
        out = []
        for i, t in enumerate(soup.find_all("table")):
            rows = t.find_all("tr")
            out.append({"index": i, "rows": len(rows), "cols": max((len(r.find_all(["td", "th"])) for r in rows),
                                                                    default=0)})
        return out


def audit_html_file(path, expected: dict | None = None) -> dict:
    """Integrity checks for an HTML file produced outside Python (MT5, TradingView, a spreadsheet...)."""
    path = Path(path)
    text, enc, issues = _decode(path.read_bytes())
    soup = BeautifulSoup(text, "html.parser")
    issues += [(s, "structure", m) for s, m in check_structure(text)]
    issues += [(FAIL, "bad_tokens", m) for m in check_bad_tokens(soup)]
    issues += [(FAIL, "offline", m) for m in check_offline(soup)]
    issues += [(FAIL, "injection", m) for m in check_injection(soup)]
    issues += [(WARN, "a11y", m) for m in check_a11y(soup)]
    issues += [(WARN, "timezone", m) for m in check_timezone(soup)]
    if expected is not None:
        probs, _, untagged = check_reconcile(soup, expected)
        issues += [(FAIL, "reconcile", m) for m in probs]
        if untagged:
            issues.append((WARN, "metric_coverage", f"not tagged, so not verified: {untagged}"))
    return {"path": str(path), "encoding": enc, "status": worst(s for s, _, _ in issues),
            "issues": issues, "tables": _list_tables(text, soup)}


# =================================================================== safe embedding
def escape_json_for_script(obj) -> str:
    """JSON safe to place inside <script type="application/json">: no NaN/Infinity, and
    <, > and & escaped so the data can never close the script element."""
    s = json.dumps(obj, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
