"""Standalone scaling strategy CLI and read-only MT5 feed; no broker SDK."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from .data import dataset_hash, load_bars
from .strategy import STRATEGY_ID, VERSION, Config, Profile, ScalingStrategy

COLUMNS = (
    "schema",
    "strategy_id",
    "strategy_version",
    "broker_symbol",
    "instrument_id",
    "timeframe",
    "bar_open_utc",
    "available_utc",
    "generated_utc",
    "expires_utc",
    "synthetic",
    "replay",
    "status",
    "signal",
    "entry",
    "stop",
    "target",
    "trail_stop",
    "time_stop_bars",
    "exit_long",
    "exit_short",
    "grade",
    "reason",
    "payload_json",
    "config_hash",
    "dataset_hash",
)


def clean(value):
    if isinstance(value, dict):
        return {key: clean(v) for key, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_feed(path, record):
    def text(v):
        return (
            ""
            if v is None
            else str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
        )

    atomic_text(
        path,
        "\t".join(COLUMNS)
        + "\r\n"
        + "\t".join(text(record.get(c, "")) for c in COLUMNS)
        + "\r\n",
    )


def feed_record(
    history, frames, cfg, *, symbol, now, synthetic=False, replay=False, ttl=300
):
    if (
        not history
        or not symbol
        or any(c in symbol for c in "\t\r\n")
        or not 1 <= ttl <= 86400
    ):
        raise ValueError("invalid empty history, broker symbol or TTL")
    latest = history[-1]
    start = pd.Timestamp(latest["time_open"])
    end = pd.Timestamp(latest["time_close"])
    now = pd.Timestamp(now)
    if now.tzinfo is None or end > now:
        raise ValueError(
            "timezone-aware decision time must follow the completed chart bar"
        )
    if not replay and (now - end).total_seconds() > 2 * cfg.chart_seconds:
        raise ValueError(
            "latest chart bar stale; update CSV data or explicitly use replay"
        )
    events = latest["events"]
    entries = [event for event in events if event["event"] == "ENTRY"]
    primary = entries[0] if entries else None
    trade = primary["trade"] if primary else None
    exits = [
        event
        for event in events
        if event["event"] in ("STOP_LOSS", "BREAKEVEN_STOP", "STRUCTURE_EXIT")
    ]
    payload = clean(
        {
            "strategy_id": STRATEGY_ID,
            "strategy_version": VERSION,
            "latest": latest,
            "config": asdict(cfg),
            "source": "owner Pine Gold / ES Scaling Scanner v2.8.0",
            "label": "RESEARCH TRACKER: simulated limits, stop/BE/TP and equity; no broker orders or capital authorization",
        }
    )
    reason = (
        "; ".join(
            event["event"]
            + (f" {event['setup']}" if event.get("setup") else "")
            + (f" #{event['trade']['number']}" if event.get("trade") else "")
            for event in events
        )
        or "No new scaling/management events; see tracked positions in payload"
    )
    if events:
        reason = (
            "New York " + end.tz_convert("America/New_York").isoformat() + "; " + reason
        )
    return dict(
        schema="its-mt5-bridge-1",
        strategy_id=STRATEGY_ID,
        strategy_version=VERSION,
        broker_symbol=symbol,
        instrument_id=symbol,
        timeframe=f"{cfg.chart_seconds // 60}m",
        bar_open_utc=int(start.timestamp()),
        available_utc=int(end.timestamp()),
        generated_utc=int(now.timestamp()),
        expires_utc=int((now + timedelta(seconds=ttl)).timestamp()),
        synthetic=int(synthetic),
        replay=int(replay),
        status="SIGNAL" if primary else "FLAT",
        signal=trade["side"] if trade else 0,
        entry=trade["entry"] if trade else float(frames["chart"].loc[start, "close"]),
        stop=trade["stop"] if trade else None,
        target=trade["targets"][0] if trade and trade["targets"] else None,
        exit_long=int(any(e["trade"]["side"] > 0 for e in exits)),
        exit_short=int(any(e["trade"]["side"] < 0 for e in exits)),
        grade=trade["kind"] if trade else "",
        reason=reason,
        payload_json=json.dumps(payload, separators=(",", ":"), allow_nan=False),
        config_hash=cfg.fingerprint(),
        dataset_hash=dataset_hash(frames),
    )


def load_config(path, profile, symbol="XAUUSD"):
    values = json.loads(Path(path).read_text()) if path else {}
    configured = values.pop("profile_name", "Auto")
    selected = profile or configured
    if selected == "Auto":
        ticker = symbol.upper()
        selected = (
            "Gold"
            if "XAU" in ticker or "GOLD" in ticker or ticker.startswith("GC")
            else "ES"
        )
    if selected not in ("Gold", "ES"):
        raise ValueError("profile_name must be Auto, Gold or ES")
    p = Profile.gold() if selected == "Gold" else Profile.es()
    overrides = values.pop("profile", {})
    p = Profile(**(asdict(p) | overrides))
    for key in ("tier_growth", "tier_risk"):
        if key in values:
            values[key] = tuple(values[key])
    return Config(profile=p, **values)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Gold/ES scaling v2.8.0: separate Python strategy and MT5 bridge"
    )
    for name in ("chart", "a", "s", "daily", "weekly"):
        parser.add_argument(
            "--" + name, required=True, help="CSV of completed " + name + " OHLC bars"
        )
    parser.add_argument("--profile", choices=["Auto", "Gold", "ES"])
    parser.add_argument(
        "--config", help="JSON config overrides; defaults retain Pine inputs"
    )
    parser.add_argument("--timezone", help="Explicit timezone for naive CSV timestamps")
    parser.add_argument("--broker-symbol", required=True)
    parser.add_argument(
        "--output", required=True, help="Full history/state JSON report"
    )
    parser.add_argument("--mt5-feed", help="Optional TSV destination inside MQL5/Files")
    parser.add_argument(
        "--asof", help="Timezone-aware timestamp; explicit replay/diagnostic mode"
    )
    parser.add_argument("--ttl", type=int, default=300)
    parser.add_argument("--synthetic", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config, args.profile, args.broker_symbol)
        now = pd.Timestamp(args.asof) if args.asof else pd.Timestamp(datetime.now(UTC))
        if now.tzinfo is None:
            raise ValueError("--asof requires UTC offset")
        lengths = {
            "chart": cfg.chart_seconds,
            "a": cfg.a_seconds,
            "s": cfg.s_seconds,
            "daily": 86400,
            "weekly": 604800,
        }
        frames = {
            key: load_bars(getattr(args, key), seconds, args.timezone)
            for key, seconds in lengths.items()
        }
        frames = {
            key: frame.loc[frame.time_close <= now].copy()
            for key, frame in frames.items()
        }
        if any(frame.empty for frame in frames.values()):
            raise ValueError(
                "each timeframe needs completed bars at --asof/current time"
            )
        history = ScalingStrategy(cfg).evaluate(frames)
        inferred = any(
            "synthetic" in getattr(args, key).lower()
            or "fixture" in getattr(args, key).lower()
            for key in lengths
        )
        record = feed_record(
            history,
            frames,
            cfg,
            symbol=args.broker_symbol,
            now=now,
            synthetic=args.synthetic or inferred,
            replay=bool(args.asof),
            ttl=args.ttl,
        )
        report = clean(
            {
                "strategy_id": STRATEGY_ID,
                "strategy_version": VERSION,
                "synthetic": bool(record["synthetic"]),
                "replay": bool(args.asof),
                "config_hash": cfg.fingerprint(),
                "dataset_hash": dataset_hash(frames),
                "config": asdict(cfg),
                "history": history,
            }
        )
        atomic_text(args.output, json.dumps(report, indent=2, allow_nan=False) + "\n")
        if args.mt5_feed:
            write_feed(args.mt5_feed, record)
        count = sum(
            event["event"] == "ENTRY" for row in history for event in row["events"]
        )
        print(
            f"{STRATEGY_ID} v{VERSION}: {len(history)} closed chart bars, {count} tracked entries; latest {record['reason']}"
        )
        return 0
    except (ValueError, TypeError, OverflowError, OSError, KeyError) as exc:
        if args.mt5_feed:
            write_feed(
                args.mt5_feed,
                dict(
                    schema="its-mt5-bridge-1",
                    strategy_id=STRATEGY_ID,
                    strategy_version=VERSION,
                    broker_symbol=args.broker_symbol,
                    status="ERROR",
                    signal=0,
                    reason=str(exc),
                ),
            )
        print(f"ERROR: {exc}")
        return 2
