"""Validated explicit multi-timeframe inputs; no invented higher-timeframe history."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd


def load_bars(path, seconds, timezone=None):
    raw = pd.read_csv(path)
    raw.columns = [str(c).strip().lower() for c in raw.columns]
    stamp = next(
        (c for c in ("time", "timestamp", "datetime", "date") if c in raw), None
    )
    if stamp is None:
        raise ValueError(f"{path}: timestamp/time column missing")
    if pd.api.types.is_numeric_dtype(raw[stamp]):
        values = raw[stamp].to_numpy(float)
        idx = pd.DatetimeIndex(
            pd.to_datetime(values, unit="ms" if max(values) > 1e11 else "s", utc=True)
        )
    else:
        idx = pd.DatetimeIndex(pd.to_datetime(raw[stamp], format="mixed"))
        if idx.tz is None:
            if not timezone:
                raise ValueError(f"{path}: naive timestamps require --timezone")
            idx = idx.tz_localize(timezone, ambiguous="raise", nonexistent="raise")
        idx = idx.tz_convert("UTC")
    frame = raw[["open", "high", "low", "close"]].astype(float)
    frame.index = idx
    frame.index.name = "time"
    if "time_close" in raw:
        col = raw["time_close"]
        if pd.api.types.is_numeric_dtype(col):
            values = col.to_numpy(float)
            close = pd.DatetimeIndex(
                pd.to_datetime(
                    values, unit="ms" if max(values) > 1e11 else "s", utc=True
                )
            )
        else:
            close = pd.DatetimeIndex(pd.to_datetime(col, format="mixed"))
            if close.tz is None:
                if not timezone:
                    raise ValueError("naive time_close requires --timezone")
                close = close.tz_localize(
                    timezone, ambiguous="raise", nonexistent="raise"
                )
            close = close.tz_convert("UTC")
        frame["time_close"] = close
    else:
        frame["time_close"] = idx + pd.Timedelta(seconds=seconds)
    validate(frame)
    return frame


def validate(frame):
    if (
        frame.empty
        or frame.index.tz is None
        or frame.index.has_duplicates
        or not frame.index.is_monotonic_increasing
    ):
        raise ValueError("nonempty, sorted, unique, timezone-aware bars required")
    values = frame[["open", "high", "low", "close"]].to_numpy(float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("OHLC prices must be finite and positive")
    if (
        (frame.high < frame[["open", "low", "close"]].max(axis=1))
        | (frame.low > frame[["open", "high", "close"]].min(axis=1))
    ).any():
        raise ValueError("invalid OHLC relationships")
    end = pd.DatetimeIndex(frame.time_close)
    if (
        end.tz is None
        or (end <= frame.index).any()
        or not end.is_monotonic_increasing
        or end.has_duplicates
    ):
        raise ValueError("invalid/unordered bar close times")
    if (end[:-1] > frame.index[1:]).any():
        raise ValueError("overlapping bars")


def dataset_hash(frames):
    h = hashlib.sha256()
    for name, frame in sorted(frames.items()):
        h.update(name.encode())
        h.update(frame.index.asi8.tobytes())
        h.update(pd.DatetimeIndex(frame.time_close).asi8.tobytes())
        h.update(
            frame[["open", "high", "low", "close"]].to_numpy(dtype="<f8").tobytes()
        )
    return h.hexdigest()
