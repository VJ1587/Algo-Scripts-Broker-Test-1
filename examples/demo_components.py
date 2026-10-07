"""Demo components for the stress harness: good ones and planted bugs.

Good: DemoPersonality, DonchianScanner, FixedFractionalExecutor, SimpleJournal.
Planted bugs (the harness must catch them): LeakyScanner, SloppyPersonality.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from stresslab.adapters import validate_bars

BARS_PER_YEAR = 24 * 252  # hourly bars, illustrative
MAX_LOG_MOVE = 0.5


def check_input(bars: pd.DataFrame, who: str) -> None:
    """Raise ValueError on bad data, including price scale changes."""
    problems = validate_bars(bars)
    if not problems and len(bars) > 1:
        lr = np.log(bars["close"].to_numpy(float))
        jump = np.abs(np.diff(lr))
        if (jump > MAX_LOG_MOVE).any():
            problems.append(f"{int((jump > MAX_LOG_MOVE).sum())} single bar log moves > {MAX_LOG_MOVE} "
                            "(price scale change?)")
    if problems:
        raise ValueError(f"{who}: bad bars: " + "; ".join(problems))


# ------------------------------------------------------------- personality
class DemoPersonality:
    """Trend strength, realized volatility and mean reversion from recent bars."""

    output_bounds = {"trend_strength": (0.0, 1.0), "realized_vol": (0.0, 1.0), "mean_reversion": (-1.0, 1.0)}
    expectations = {"trend_strength": ["range", "trend_up"],
                    "realized_vol": ["range", "panic"],
                    "mean_reversion": ["trend_up", "range"]}

    def __init__(self, er_len: int = 50, vol_len: int = 100, ac_len: int = 100):
        self.er_len, self.vol_len, self.ac_len = er_len, vol_len, ac_len
        self.min_bars = max(er_len, vol_len, ac_len) + 1

    def _validate(self, bars):
        check_input(bars, type(self).__name__)
        if len(bars) < 20:
            raise ValueError(f"{type(self).__name__}: need at least 20 bars, got {len(bars)}")

    @staticmethod
    def _autocorr1(x: np.ndarray) -> float:
        a, b = x[:-1], x[1:]
        if len(a) < 3:
            return 0.0
        a, b = a - a.mean(), b - b.mean()
        den = math.sqrt(float((a * a).sum() * (b * b).sum()))
        return float((a * b).sum() / den) if den > 0 else 0.0

    def _point(self, close: np.ndarray) -> dict:
        lr = np.diff(np.log(close))
        k = min(self.er_len, len(lr))
        path = np.abs(lr[-k:]).sum()
        er = abs(float(lr[-k:].sum())) / path if path > 0 else 0.0
        v = lr[-min(self.vol_len, len(lr)):]
        vol = float(v.std(ddof=1)) * math.sqrt(BARS_PER_YEAR) if len(v) > 1 else 0.0
        ac = self._autocorr1(lr[-min(self.ac_len, len(lr)):])
        return {"trend_strength": float(np.clip(er, 0, 1)), "realized_vol": float(np.clip(vol, 0, 1)),
                "mean_reversion": float(np.clip(-ac, -1, 1))}

    def assess(self, bars: pd.DataFrame) -> dict:
        self._validate(bars)
        return self._point(bars["close"].to_numpy(float))

    def assess_series(self, bars: pd.DataFrame) -> pd.DataFrame:
        """One row per bar using only data up to that bar (trailing windows)."""
        self._validate(bars)
        lr = np.log(bars["close"]).diff()
        er = (lr.rolling(self.er_len).sum().abs() / lr.abs().rolling(self.er_len).sum()).fillna(0.0)
        vol = lr.rolling(self.vol_len).std() * math.sqrt(BARS_PER_YEAR)
        lag = lr.shift(1)
        ac = lr.rolling(self.ac_len - 1).corr(lag)
        out = pd.DataFrame({"trend_strength": er.clip(0, 1), "realized_vol": vol.clip(0, 1),
                            "mean_reversion": (-ac).clip(-1, 1)}, index=bars.index)
        out.iloc[: self.min_bars - 1] = np.nan
        return out


class SloppyPersonality(DemoPersonality):
    """PLANTED BUG: no input validation, and assess_series normalises over the full sample."""

    def _validate(self, bars):
        return None

    def assess_series(self, bars: pd.DataFrame) -> pd.DataFrame:
        out = super().assess_series(bars)
        # full sample percentile rank: the value at bar t depends on bars after t
        out["trend_strength"] = out["trend_strength"].rank(pct=True)  # guard: ignore[TG105] planted bug for harness self test
        return out

    def assess(self, bars: pd.DataFrame) -> dict:
        point = self._point(bars["close"].to_numpy(float))
        er = super().assess_series(bars)["trend_strength"].dropna()
        if len(er):
            point["trend_strength"] = float((er <= er.iloc[-1]).mean())
        return point


# ------------------------------------------------------------- scanner
class DonchianScanner:
    """Close breaks the prior ``lookback`` bar high (long) or low (short).

    Stop and target are sized in ATR. The signal bar's close is the entry
    reference; the executor fills at a later bar's open.
    """

    def __init__(self, lookback: int = 55, atr_len: int = 14, stop_atr: float = 2.0, target_atr: float = 3.0):
        self.lookback, self.atr_len = int(lookback), int(atr_len)
        self.stop_atr, self.target_atr = float(stop_atr), float(target_atr)

    def _true_range(self, bars):
        prev = bars["close"].shift(1)
        return pd.concat([bars["high"] - bars["low"], (bars["high"] - prev).abs(), (bars["low"] - prev).abs()],
                         axis=1).max(axis=1)

    def _atr(self, bars: pd.DataFrame) -> pd.Series:
        return self._true_range(bars).rolling(self.atr_len).mean()

    def scan(self, bars: pd.DataFrame) -> pd.DataFrame:
        check_input(bars, type(self).__name__)
        c = bars["close"]
        hi = bars["high"].rolling(self.lookback).max().shift(1)
        lo = bars["low"].rolling(self.lookback).min().shift(1)
        up = (c > hi) & (c.shift(1) <= hi.shift(1))
        dn = (c < lo) & (c.shift(1) >= lo.shift(1))
        atr = self._atr(bars)
        side = pd.Series(np.where(up, 1, np.where(dn, -1, 0)), index=bars.index)
        ok = (side != 0) & atr.notna() & (atr > 0)
        s = side[ok]
        a = atr[ok]
        e = c[ok]
        return pd.DataFrame({"time": s.index, "side": s.to_numpy(int), "entry": e.to_numpy(),
                             "stop": (e - s * self.stop_atr * a).to_numpy(),
                             "target": (e + s * self.target_atr * a).to_numpy()}).reset_index(drop=True)


class LeakyScanner(DonchianScanner):
    """PLANTED BUG: ATR uses a centred window, so stops are sized with future volatility."""

    def _atr(self, bars: pd.DataFrame) -> pd.Series:
        tr = self._true_range(bars)
        return tr.rolling(self.atr_len, center=True).mean()  # guard: ignore[TG102] planted bug for harness self test


# ------------------------------------------------------------- executor
class FixedFractionalExecutor:
    """Risk ``risk_frac`` of current equity per trade; at most ``max_open`` positions."""

    def __init__(self, risk_frac: float = 0.01, max_open: int = 1):
        self.risk_frac, self.max_open = float(risk_frac), int(max_open)

    def on_signal(self, signal_row, broker) -> None:
        if broker.open_count() >= self.max_open:
            return
        dist = abs(float(signal_row["entry"]) - float(signal_row["stop"]))
        if not np.isfinite(dist) or dist <= 0:
            return
        risk_budget = broker.equity * self.risk_frac
        broker.market_order(int(signal_row["side"]), risk_budget / dist, float(signal_row["stop"]),
                            float(signal_row["target"]), signal_row["time"], risk_budget)


# ------------------------------------------------------------- journal
class SimpleJournal:
    def __init__(self):
        self.trades: list[dict] = []

    def record(self, trade: dict) -> None:
        self.trades.append(dict(trade))

    def summary(self) -> dict:
        pnl = np.array([t["pnl"] for t in self.trades], dtype=float)
        r = np.array([t["r_multiple"] for t in self.trades], dtype=float)
        n = len(pnl)
        return {"n_trades": n, "net_pnl": float(pnl.sum()) if n else 0.0,
                "win_rate": float((pnl > 0).mean()) if n else math.nan,
                "avg_r": float(np.nanmean(r)) if n else math.nan}
