"""Load bar and trade exports (MT5, TradingView, anything else) into harness schemas.

Always pass an explicit column map; no export layout is assumed.

WARNING about MT5 times: MetaTrader 5 bar and deal times are the BROKER
SERVER time, not UTC. Many brokers run their server a few hours ahead of
UTC and some shift it with daylight saving, but this varies by broker.
Confirm the offset (and whether it changes with DST) with your broker before
loading, and pass it as ``server_utc_offset_hours``. A wrong offset cannot be
detected from the data and silently shifts every session filter and news
window. TradingView exports usually carry the chart time zone; check the
export settings.
"""
from __future__ import annotations

import pandas as pd

from stresslab.adapters import validate_bars


def _parse_time(df: pd.DataFrame, spec, server_utc_offset_hours: float | None, time_format: str | None):
    if isinstance(spec, (list, tuple)):
        raw = df[list(spec)].astype(str).agg(" ".join, axis=1)
    else:
        raw = df[spec]
    t = pd.to_datetime(raw, format=time_format)
    if getattr(t.dt, "tz", None) is None:
        if server_utc_offset_hours is None:
            raise ValueError("timestamps are naive: pass server_utc_offset_hours explicitly "
                             "(0 if the export really is UTC)")
        t = (t - pd.Timedelta(hours=server_utc_offset_hours)).dt.tz_localize("UTC")
    else:
        t = t.dt.tz_convert("UTC")
    return t


def load_bars_csv(path, colmap: dict, server_utc_offset_hours: float | None, time_format: str | None = None,
                  sep: str = ",", validate: bool = True, **read_csv_kw) -> pd.DataFrame:
    """Load bars from a CSV export.

    ``colmap`` maps harness names to export column names, e.g.
    ``{"time": ["<DATE>", "<TIME>"], "open": "<OPEN>", "high": "<HIGH>",
    "low": "<LOW>", "close": "<CLOSE>", "volume": "<TICKVOL>", "spread": "<SPREAD>"}``.
    ``time`` may be one column or a list of columns joined with a space.
    ``spread`` must already be in price units (MT5 exports it in points; convert
    first). Raises ``ValueError`` if the result fails ``validate_bars``.
    """
    df = pd.read_csv(path, sep=sep, **read_csv_kw)
    if "time" not in colmap:
        raise ValueError("colmap must map 'time'")
    t = _parse_time(df, colmap["time"], server_utc_offset_hours, time_format)
    out = pd.DataFrame(index=pd.DatetimeIndex(t, name="time"))
    for k in ("open", "high", "low", "close", "volume", "spread"):
        if k in colmap:
            out[k] = pd.to_numeric(df[colmap[k]], errors="coerce").to_numpy()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    if validate:
        problems = validate_bars(out)
        if problems:
            raise ValueError(f"{path}: " + "; ".join(problems))
    return out


def load_trades_csv(path, colmap: dict, server_utc_offset_hours: float | None, time_cols=("time",),
                    time_format: str | None = None, side_map: dict | None = None, sep: str = ",",
                    **read_csv_kw) -> pd.DataFrame:
    """Load a trade or signal list export (for ``parity_check``).

    ``colmap`` maps harness names (time, side, entry, stop, target, exit, pnl...)
    to export columns. ``time_cols`` lists which harness names are times.
    ``side_map`` converts text sides, e.g. ``{"buy": 1, "sell": -1}``.
    """
    df = pd.read_csv(path, sep=sep, **read_csv_kw)
    out = pd.DataFrame(index=df.index)
    for k, src in colmap.items():
        if k in time_cols:
            out[k] = _parse_time(df, src, server_utc_offset_hours, time_format)
        else:
            out[k] = df[src]
    if "side" in out.columns:
        if side_map:
            out["side"] = out["side"].astype(str).str.strip().str.lower().map(
                {str(a).lower(): b for a, b in side_map.items()})
        out["side"] = pd.to_numeric(out["side"], errors="coerce")
    return out
