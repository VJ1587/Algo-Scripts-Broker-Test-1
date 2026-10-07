#!/usr/bin/env python3
"""
Forex Factory red-folder news for the scanner, the trade gate and the backtest
==============================================================================

Source
    Forex Factory calendar, https://www.forexfactory.com/calendar. Red folders are "High" impact.
    The HTML page blocks scripted access (Cloudflare challenge), so every script reads Forex
    Factory's machine readable export of the same calendar (scanner_config.yaml calendar.url):
    title, currency, date and time, impact, forecast, previous. It covers the current week only and
    has no "actual" value, so a beat or miss against forecast cannot be measured from it.

History
    The scanner saves every fetch to data/calendar_snapshots/. load_archive() merges them into one
    event history; published_at is the first snapshot that contained the event, so a backtest never
    knows an event before it was announced.

Reaction tracker  [OWNER cfg-0.6.0]
    For each past red event, the move of every tracked pair that contains the event's currency from
    the last 5 minute close before the release to 15 minutes, 1 hour and 4 hours after it. A pair's
    move counts for the currency with sign +1 when the currency is the base and -1 when it is the
    quote, so "currency move" is the average strength change of that currency across its pairs.
    Results append to logs/news_reactions.csv.

Usage
    python news.py --upcoming                     # red events for the rest of this week
    python news.py --export-backtest data/news/ff_high_impact.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

SNAP_RE = re.compile(r"ff_\d{4}-W\d{2}_(\d{8}T\d{4}Z)\.json$")
REACTION_FIELDS = ["event_time", "currency", "title", "impact", "forecast", "previous", "pair", "side",
                   "ref_price", "pip"] + [f"{k}_{h}" for h in ("15m", "1h", "4h") for k in ("ret_pct", "pips")] + ["range_1h_pips", "measured_utc"]
HORIZONS = {"15m": 15, "1h": 60, "4h": 240}


def parse_events(raw: list[dict], seen_at: pd.Timestamp) -> pd.DataFrame:
    """Feed rows -> event_time (UTC), currency, title, impact, forecast, previous, published_at."""
    rows = []
    for e in raw or []:
        try:
            t = pd.Timestamp(e["date"]).tz_convert("UTC")
        except (KeyError, ValueError, TypeError):
            continue
        rows.append({"event_time": t, "currency": str(e.get("country", "")).upper(), "title": str(e.get("title", "")),
                     "impact": str(e.get("impact", "")), "forecast": str(e.get("forecast", "") or ""),
                     "previous": str(e.get("previous", "") or ""), "published_at": seen_at})
    cols = ["event_time", "currency", "title", "impact", "forecast", "previous", "published_at"]
    return pd.DataFrame(rows, columns=cols)


def load_archive(snap_dir: Path, current: Optional[list[dict]] = None, now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Every saved snapshot (plus an optional live fetch) merged into one event table, deduplicated on
    (event_time, currency, title). published_at = first time the event was seen; forecast and previous
    keep the latest values."""
    frames = []
    for p in sorted(snap_dir.glob("ff_*.json")) if snap_dir.exists() else []:
        m = SNAP_RE.search(p.name)
        if not m:
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        frames.append(parse_events(raw, pd.Timestamp(m.group(1)).tz_convert("UTC")))   # name ends in Z: already UTC
    if current is not None:
        frames.append(parse_events(current, now or pd.Timestamp.now(tz="UTC")))
    if not frames:
        return parse_events([], pd.Timestamp.now(tz="UTC"))
    df = pd.concat(frames, ignore_index=True).sort_values("published_at")
    key = ["event_time", "currency", "title"]
    first = df.groupby(key, as_index=False)["published_at"].min()
    last = df.drop_duplicates(key, keep="last").drop(columns="published_at")
    return last.merge(first, on=key).sort_values("event_time").reset_index(drop=True)


def red(df: pd.DataFrame, impacts=("High",)) -> pd.DataFrame:
    return df[df["impact"].isin(impacts)]


def affected_map(instruments: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """currency -> instruments whose calendar currencies include it (USD also covers gold, S&P, oil)."""
    out: dict[str, list[str]] = {}
    for sym, ccys in instruments:
        for c in ccys:
            out.setdefault(c, []).append(sym)
    return out


def upcoming(df: pd.DataFrame, now: pd.Timestamp, hours: float = 168, impacts=("High",)) -> pd.DataFrame:
    d = red(df, impacts)
    return d[(d["event_time"] >= now - pd.Timedelta(minutes=30)) & (d["event_time"] <= now + pd.Timedelta(hours=hours))]


def events_near(df: pd.DataFrame, currencies: list[str], t: pd.Timestamp, window_min: float,
                impacts=("High",)) -> pd.DataFrame:
    """[v1.0 Section 9] Red events for these currencies within +/- window_min of t."""
    d = red(df, impacts)
    w = pd.Timedelta(minutes=window_min)
    return d[d["currency"].isin(currencies) & (d["event_time"] >= t - w) & (d["event_time"] <= t + w)]


def export_backtest(df: pd.DataFrame, path: Path, impacts=("High",)) -> int:
    """CSV for broker_v11_algo.py --news: event_time, currency, impact, published_at (UTC), title."""
    d = red(df, impacts)
    out = pd.DataFrame({"event_time": d["event_time"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                        "currency": d["currency"], "impact": d["impact"].str.lower(),
                        "published_at": d["published_at"].dt.strftime("%Y-%m-%d %H:%M:%S+00:00"), "title": d["title"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return len(out)


# ---------------------------------------------------------------- reaction tracker

def pip_of(pair: str) -> float:
    return 0.01 if "JPY" in pair else 0.0001


def measure_event(ev: pd.Series, pairs: list[str], bars: Callable[[str], pd.DataFrame], now: pd.Timestamp) -> list[dict]:
    """Moves of each pair holding the event currency. bars(pair) returns 5 minute UTC bars indexed by open time."""
    t = ev["event_time"]
    rows = []
    for pair in pairs:
        side = 1 if pair[:3] == ev["currency"] else -1
        try:
            b = bars(pair)
        except Exception:  # noqa: BLE001  # guard: ignore[TG301] skips a pair whose bars fail to load; failures are not logged, review
            continue
        before = b[b.index + pd.Timedelta(minutes=5) <= t]
        if before.empty or (t - (before.index[-1] + pd.Timedelta(minutes=5))) > pd.Timedelta(hours=1):
            continue   # no fresh bar right before the release (market closed or history gap)
        ref = float(before["close"].iloc[-1])
        row = {"event_time": t.strftime("%Y-%m-%d %H:%M"), "currency": ev["currency"], "title": ev["title"],
               "impact": ev["impact"], "forecast": ev.get("forecast", ""), "previous": ev.get("previous", ""),
               "pair": pair, "side": side, "ref_price": ref, "pip": pip_of(pair),
               "measured_utc": now.strftime("%Y-%m-%d %H:%M")}
        complete = True
        for name, mins in HORIZONS.items():
            upto = b[(b.index >= t) & (b.index + pd.Timedelta(minutes=5) <= t + pd.Timedelta(minutes=mins))]
            if upto.empty or upto.index[-1] + pd.Timedelta(minutes=5) < t + pd.Timedelta(minutes=mins) - pd.Timedelta(minutes=10):
                complete = False
                break
            px = float(upto["close"].iloc[-1])
            row[f"ret_pct_{name}"] = round((px / ref - 1) * 100, 4)
            row[f"pips_{name}"] = round((px - ref) / pip_of(pair), 1)
            if name == "1h":
                row["range_1h_pips"] = round((float(upto["high"].max()) - float(upto["low"].min())) / pip_of(pair), 1)
        if complete:
            rows.append(row)
    return rows


def update_reactions(events: pd.DataFrame, fx_pairs: list[str], bars: Callable[[str], pd.DataFrame], log_path: Path,
                     now: pd.Timestamp, lookback_days: float = 10, impacts=("High",)) -> int:
    """Measure red events whose 4 hour window has closed and that are not logged yet. Returns rows added."""
    done = set()
    if log_path.exists():
        old = pd.read_csv(log_path, dtype=str)
        done = set(zip(old["event_time"], old["currency"], old["title"]))
    d = red(events, impacts)
    d = d[(d["event_time"] + pd.Timedelta(minutes=max(HORIZONS.values()) + 10) <= now)
          & (d["event_time"] >= now - pd.Timedelta(days=lookback_days))]
    cache: dict[str, pd.DataFrame] = {}

    def cached(p: str) -> pd.DataFrame:
        if p not in cache:
            cache[p] = bars(p)
        return cache[p]
    new_rows = []
    for _, ev in d.iterrows():
        if (ev["event_time"].strftime("%Y-%m-%d %H:%M"), ev["currency"], ev["title"]) in done:
            continue
        pairs = [p for p in fx_pairs if ev["currency"] in (p[:3], p[3:])]
        new_rows += measure_event(ev, pairs, cached, now)
    if new_rows:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        new = not log_path.exists()
        with open(log_path, "a", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=REACTION_FIELDS)
            if new:
                wr.writeheader()
            wr.writerows(new_rows)
    return len(new_rows)


def summarize_reactions(log_path: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(per event, per currency). Currency move = mean over its pairs of side x return (strength change)."""
    if not log_path.exists():
        return pd.DataFrame(), pd.DataFrame()
    r = pd.read_csv(log_path)
    if r.empty:
        return pd.DataFrame(), pd.DataFrame()
    for h in HORIZONS:
        r[f"ccy_{h}"] = r["side"] * r[f"ret_pct_{h}"]
    g = r.groupby(["event_time", "currency", "title"], sort=False)
    ev = g.agg(pairs=("pair", "count"), forecast=("forecast", "first"), previous=("previous", "first"),
               **{f"move_{h}": (f"ccy_{h}", "mean") for h in HORIZONS}).reset_index()
    agree = g.apply(lambda x: int((np.sign(x["ccy_1h"]) == np.sign(x["ccy_1h"].mean())).sum()), include_groups=False)
    ev["agree_1h"] = agree.to_numpy()
    top = r.loc[r.groupby(["event_time", "currency", "title"], sort=False)["pips_1h"].apply(lambda s: s.abs().idxmax())]
    ev["biggest_1h"] = (top["pair"] + " " + top["pips_1h"].map(lambda v: f"{v:+.1f}") + " pips").to_numpy()
    ev = ev.sort_values("event_time", ascending=False).reset_index(drop=True)
    cur = ev.groupby("currency").agg(events=("title", "count"), avg_abs_1h=("move_1h", lambda s: s.abs().mean()),
                                     avg_abs_4h=("move_4h", lambda s: s.abs().mean()),
                                     last_event=("event_time", "max")).reset_index()
    return ev, cur.sort_values("avg_abs_1h", ascending=False).reset_index(drop=True)


def main(argv: Optional[list[str]] = None) -> int:
    import yaml
    base = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Forex Factory red-folder news tools")
    ap.add_argument("--config", default=str(base / "scanner_config.yaml"))
    ap.add_argument("--upcoming", action="store_true", help="list red events for the rest of this week")
    ap.add_argument("--export-backtest", metavar="CSV", help="write the broker_v11_algo.py --news file")
    a = ap.parse_args(argv)
    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    ccfg = cfg.get("calendar", {})
    impacts = tuple(ccfg.get("impacts_tracked", ["High"]))
    snap = base / cfg["paths"]["data_dir"] / "calendar_snapshots"
    now = pd.Timestamp.now(tz="UTC")
    current = None
    if a.upcoming:
        import requests
        current = requests.get(ccfg["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0 scanner"}).json()
    df = load_archive(snap, current, now)
    if a.upcoming:
        up = upcoming(df, now, 24 * 8, impacts)
        for _, e in up.iterrows():
            print(f"{e['event_time']:%a %Y-%m-%d %H:%M} UTC  {e['currency']}  {e['title']}  "
                  f"forecast {e['forecast'] or '-'}  previous {e['previous'] or '-'}")
        print(f"{len(up)} red-folder event(s)")
    if a.export_backtest:
        n = export_backtest(df, Path(a.export_backtest), impacts)
        first = df["published_at"].min()
        print(f"wrote {n} events to {a.export_backtest}. Archive starts {first:%Y-%m-%d}: a backtest before that "
              "date has no news filter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
