"""Debug runner: run a script as __main__ and explain a crash in trading terms.

Captures warnings (with counts), wall time and peak memory. On an exception it
walks the traceback frames that belong to YOUR code (not site-packages, the
standard library or the codeguard package itself) and summarises their
locals: DataFrame shape, columns, NaNs, index timezone, order, uniqueness and
time range; Series; ndarrays with NaN/inf counts; scalars; container sizes.
Then it adds pattern hints (tz mix, missing column, None from MT5, empty
frames, ...).
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import sysconfig
import time
import traceback
import tracemalloc
import warnings
from collections import Counter
from pathlib import Path

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_STDLIB = {os.path.abspath(p) for p in (sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")) if p}


def _is_user_frame(filename: str) -> bool:
    if not filename or filename.startswith("<"):
        return False
    path = os.path.abspath(filename)
    parts = set(Path(path).parts)
    if "site-packages" in parts or "dist-packages" in parts:
        return False
    if os.path.dirname(path) == _PKG_DIR:  # exact directory: codeguard/samples still counts as user code
        return False
    return not any(path.startswith(s + os.sep) for s in _STDLIB)


def summarize_value(v) -> dict | None:
    """Short structured summary of a local variable (None to skip it)."""
    try:
        import numpy as np
        import pandas as pd
    except ImportError:  # pragma: no cover
        np = pd = None
    if callable(v) or isinstance(v, type) or type(v).__name__ == "module":
        return None
    if pd is not None and isinstance(v, pd.DataFrame):
        idx = v.index
        out = {"type": "DataFrame", "shape": list(v.shape), "columns": [str(c) for c in v.columns[:20]],
               "nan_by_column": {str(k): int(n) for k, n in v.isna().sum().items() if n}, "empty": v.empty,
               "index": type(idx).__name__}
        if isinstance(idx, pd.DatetimeIndex):
            out["index_tz"] = str(idx.tz) if idx.tz is not None else "naive"
        if len(idx):
            out["index_sorted"] = bool(idx.is_monotonic_increasing)
            out["index_unique"] = bool(idx.is_unique)
            if isinstance(idx, pd.DatetimeIndex):
                out["first"], out["last"] = str(idx.min()), str(idx.max())
        return out
    if pd is not None and isinstance(v, pd.Series):
        out = {"type": "Series", "len": len(v), "dtype": str(v.dtype), "name": str(v.name), "nan": int(v.isna().sum())}
        if isinstance(v.index, pd.DatetimeIndex):
            out["index_tz"] = str(v.index.tz) if v.index.tz is not None else "naive"
        return out
    if np is not None and isinstance(v, np.ndarray):
        out = {"type": "ndarray", "shape": list(v.shape), "dtype": str(v.dtype)}
        if v.dtype.kind in "fc":
            out["nan"] = int(np.isnan(v).sum())
            out["inf"] = int(np.isinf(v).sum())
        return out
    if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
        r = repr(v)
        return {"type": type(v).__name__, "value": r if len(r) <= 120 else r[:117] + "..."}
    if isinstance(v, (list, tuple, set, frozenset, dict)):
        return {"type": type(v).__name__, "len": len(v)}
    try:
        n = len(v)
        return {"type": type(v).__name__, "len": n}
    except TypeError:
        return {"type": type(v).__name__}


def _fmt_summary(name, s) -> str:
    t = s["type"]
    if t == "DataFrame":
        bits = [f"DataFrame shape {s['shape']}", f"columns {s['columns']}"]
        if s["empty"]:
            bits.append("EMPTY")
        if s["nan_by_column"]:
            bits.append(f"NaN {s['nan_by_column']}")
        if "index_tz" in s:
            bits.append(f"index tz {s['index_tz']}")
        if "index_sorted" in s:
            bits.append(f"sorted {s['index_sorted']}, unique {s['index_unique']}")
        if "first" in s:
            bits.append(f"{s['first']} .. {s['last']}")
        return f"{name}: " + ", ".join(bits)
    if t == "Series":
        return f"{name}: Series len {s['len']} dtype {s['dtype']} NaN {s['nan']}" + (
            f" index tz {s['index_tz']}" if "index_tz" in s else "")
    if t == "ndarray":
        extra = f" NaN {s['nan']} inf {s['inf']}" if "nan" in s else ""
        return f"{name}: ndarray shape {s['shape']} dtype {s['dtype']}{extra}"
    if "value" in s:
        return f"{name}: {t} = {s['value']}"
    if "len" in s:
        return f"{name}: {t} len {s['len']}"
    return f"{name}: {t}"


def _hints(exc: BaseException, frames: list[dict]) -> list[str]:
    msg = f"{type(exc).__name__}: {exc}"
    low = msg.lower()
    hints = []
    if ("tz-naive" in low and "tz-aware" in low) or "offset-naive and offset-aware" in low:
        hints.append("Timezone mix: a tz naive timestamp meets a tz aware one. Localise everything to UTC "
                     "at load time (MT5 times are broker server time; convert with the confirmed offset).")
    if isinstance(exc, KeyError):
        cols = sorted({c for fr in frames for s in fr["locals"].values() if s.get("type") == "DataFrame"
                       for c in s["columns"]})
        hints.append(f"Missing key {exc}: column name mismatch? Columns seen in local DataFrames: {cols[:30]}")
    if "'nonetype'" in low:
        hints.append("A value is None. MetaTrader5 functions (copy_rates_*, symbol_info, order_send...) return "
                     "None on failure: check the result and log mt5.last_error().")
    if isinstance(exc, IndexError) or "out of bounds" in low or "out-of-bounds" in low:
        hints.append("Index out of bounds: the sequence or frame is shorter than expected (often empty).")
    if isinstance(exc, ZeroDivisionError) or "division by zero" in low:
        hints.append("Divide by zero: guard denominators such as stop distance, ATR, volume or trade count.")
    for fr in frames:
        for name, s in fr["locals"].items():
            if s.get("type") != "DataFrame":
                continue
            if s["empty"]:
                hints.append(f"EMPTY DataFrame `{name}` (shape {s['shape']}) in {fr['function']}: a filter or "
                             "load returned no rows; check the filter values, symbol names and date range.")
            if s.get("index_sorted") is False:
                hints.append(f"`{name}` index is not sorted: sort_index() before time based slicing.")
            if s.get("index_unique") is False:
                hints.append(f"`{name}` index has duplicates: drop or aggregate duplicated timestamps.")
            if s["nan_by_column"]:
                hints.append(f"`{name}` has NaNs {s['nan_by_column']}: decide how to handle them before use.")
    seen, out = set(), []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def run(script, argv=(), report_path=None, echo: bool = True) -> dict:
    """Run ``script`` as __main__ with ``argv``; return (and optionally write) a report dict."""
    script = os.path.abspath(script)
    saved_argv, saved_path = list(sys.argv), list(sys.path)
    saved_show, saved_filters = warnings.showwarning, list(warnings.filters)
    counts: Counter = Counter()

    def show(message, category, filename, lineno, file=None, line=None):
        counts[(category.__name__, str(message), os.path.relpath(filename) if filename else "?", lineno)] += 1

    report = {"script": os.path.relpath(script), "argv": list(argv), "status": "ok"}
    started_tm = not tracemalloc.is_tracing()
    t0 = time.perf_counter()
    try:
        sys.argv = [script, *argv]
        sys.path.insert(0, os.path.dirname(script))
        warnings.showwarning = show
        warnings.simplefilter("always")
        if started_tm:
            tracemalloc.start()
        try:
            runpy.run_path(script, run_name="__main__")
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            report["status"] = "ok" if code == 0 else "exit"
            report["exit_code"] = code
        except BaseException as e:  # noqa: BLE001  we report every crash
            frames = []
            for fr, lineno in traceback.walk_tb(e.__traceback__):
                fn = fr.f_code.co_filename
                if not _is_user_frame(fn):
                    continue
                loc = {}
                for k, v in fr.f_locals.items():
                    if k.startswith("__"):
                        continue
                    try:
                        s = summarize_value(v)
                    except Exception as err:  # noqa: BLE001  a broken __len__ must not hide the crash
                        s = {"type": type(v).__name__, "value": f"<summary failed: {err}>"}
                    if s is not None:
                        loc[k] = s
                frames.append({"file": os.path.relpath(fn), "line": lineno, "function": fr.f_code.co_name,
                               "code": _source_line(fn, lineno), "locals": loc})
            report.update(status="crashed", exception=type(e).__name__, message=str(e)[:500],
                          frames=frames, hints=_hints(e, frames))
    finally:
        report["wall_seconds"] = round(time.perf_counter() - t0, 3)
        if started_tm and tracemalloc.is_tracing():
            report["peak_memory_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 2)
            tracemalloc.stop()
        warnings.showwarning = saved_show
        warnings.filters[:] = saved_filters
        sys.argv = saved_argv
        sys.path[:] = saved_path
    report["warnings"] = [{"category": c, "message": m, "where": f"{f}:{ln}", "count": n}
                          for (c, m, f, ln), n in counts.most_common()]
    if report_path:
        Path(report_path).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    if echo:
        print(format_report(report))
    return report


def _source_line(filename, lineno) -> str:
    try:
        with open(filename, encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                if i == lineno:
                    return line.strip()
    except OSError:
        pass
    return ""


def format_report(r: dict) -> str:
    lines = [f"codeguard run: {r['script']} {' '.join(r['argv'])}".rstrip()]
    if r["status"] == "crashed":
        lines.append(f"status: CRASHED ({r['exception']}: {r['message']})")
    else:
        lines.append(f"status: {r['status'].upper()}" + (f" (exit code {r['exit_code']})" if "exit_code" in r else ""))
    lines.append(f"wall time {r['wall_seconds']}s, peak memory {r.get('peak_memory_mb', '?')} MB")
    if r["warnings"]:
        lines.append("warnings:")
        lines += [f"  {w['count']}x {w['category']}: {w['message'][:160]} ({w['where']})" for w in r["warnings"][:20]]
    else:
        lines.append("warnings: none")
    if r["status"] == "crashed":
        lines.append("frames in your code (outermost first):")
        for fr in r["frames"]:
            lines.append(f"  {fr['file']}:{fr['line']} in {fr['function']}")
            if fr["code"]:
                lines.append(f"      {fr['code']}")
            for name, s in fr["locals"].items():
                lines.append(f"      {_fmt_summary(name, s)}")
        lines.append("hints:")
        lines += [f"  - {h}" for h in r["hints"]] or ["  (none)"]
    return "\n".join(lines)
