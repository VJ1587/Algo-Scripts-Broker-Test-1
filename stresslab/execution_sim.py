"""Pessimistic simulated broker.

Rules (all chosen to err against the strategy):

* An order submitted at bar ``i`` fills at the OPEN of bar ``i + latency_bars``,
  adverse by half the spread plus slippage.
* A gap through the stop exits at that bar's open (``gap_stop``), adverse.
* Intrabar, the stop is checked BEFORE the target. If both are inside one
  bar's range the trade is a loss.
* Stop exits pay half spread plus slippage; target exits pay half spread.
* ``r_multiple = pnl / risk_budget`` where ``risk_budget`` is the money the
  executor INTENDED to risk. Dividing by the risk measured at the fill would
  give absurd values (for example -90R) when a fill lands next to the stop.
* Positions still open on the last bar are closed at its close
  (``end_of_data``) so the journal can reconcile with the broker.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ExecConditions:
    spread_mult: float = 1.0
    slippage: float = 0.0  # price units, applied adversely to entries and stop exits
    latency_bars: int = 1
    reject_prob: float = 0.0
    partial_fill_prob: float = 0.0
    max_units: float = math.inf

    def __post_init__(self):
        if self.latency_bars < 1:
            raise ValueError("latency_bars must be >= 1 (a signal at a bar close cannot fill in that bar)")


@dataclass
class Order:
    side: int
    units: float
    stop: float
    target: float
    signal_time: pd.Timestamp
    risk_budget: float
    submit_bar: int
    fill_bar: int


@dataclass
class Position:
    side: int
    units: float
    entry: float
    stop: float
    target: float
    signal_time: pd.Timestamp
    fill_time: pd.Timestamp
    fill_bar: int
    risk_budget: float
    planned_risk: float
    equity_at_fill: float
    flags: list = field(default_factory=list)


class SimBroker:
    def __init__(self, bars: pd.DataFrame, cond: ExecConditions | None = None, seed: int = 0,
                 start_equity: float = 100_000.0):
        self.bars = bars
        self.cond = cond or ExecConditions()
        self.rng = np.random.default_rng(seed)
        self.start_equity = float(start_equity)
        self.equity = float(start_equity)
        self.i = 0
        self.pending: list[Order] = []
        self.positions: list[Position] = []
        self.closed: list[dict] = []
        self.events: list[dict] = []
        self._o = bars["open"].to_numpy(float)
        self._h = bars["high"].to_numpy(float)
        self._l = bars["low"].to_numpy(float)
        self._c = bars["close"].to_numpy(float)
        sp = bars["spread"].to_numpy(float) if "spread" in bars.columns else np.zeros(len(bars))
        self._half = 0.5 * sp * self.cond.spread_mult
        self._t = bars.index

    # ------------------------------------------------------------ helpers
    def _event(self, kind: str, **detail):
        self.events.append({"kind": kind, "bar": self.i, "time": self._t[min(self.i, len(self._t) - 1)], **detail})

    def open_count(self) -> int:
        return len(self.positions) + len(self.pending)

    def event_counts(self) -> dict:
        out: dict = {}
        for e in self.events:
            out[e["kind"]] = out.get(e["kind"], 0) + 1
        return out

    # ------------------------------------------------------------ orders
    def market_order(self, side, units, stop, target, signal_time, risk_budget):
        side = int(side)
        if side not in (1, -1):
            raise ValueError(f"side must be +1 or -1, got {side}")
        if not (np.isfinite(stop)):
            self._event("naked_order", side=side, units=units)
        if not np.isfinite(units) or units <= 0:
            self._event("rejected", reason="non positive units")
            return None
        units = min(float(units), self.cond.max_units)
        o = Order(side, units, float(stop), float(target), signal_time, float(risk_budget),
                  self.i, self.i + self.cond.latency_bars)
        self.pending.append(o)
        return o

    def _fill(self, o: Order, i: int):
        cond = self.cond
        if self.rng.random() < cond.reject_prob:
            self._event("rejected", reason="broker reject", signal_time=o.signal_time)
            return
        units = o.units
        if self.rng.random() < cond.partial_fill_prob:
            units = units * float(self.rng.uniform(0.3, 0.9))
            self._event("partial", requested=o.units, filled=units)
        px = self._o[i] + o.side * (self._half[i] + cond.slippage)
        planned_risk = abs(px - o.stop) * units if np.isfinite(o.stop) else math.inf
        pos = Position(o.side, units, px, o.stop, o.target, o.signal_time, self._t[i], i,
                       o.risk_budget, planned_risk, self.equity)
        if np.isfinite(o.stop) and (px - o.stop) * o.side <= 0:
            self._event("filled_beyond_stop", fill=px, stop=o.stop)
            pos.flags.append("filled_beyond_stop")
        self.positions.append(pos)

    def _close(self, pos: Position, i: int, px: float, reason: str):
        pnl = (px - pos.entry) * pos.side * pos.units
        self.equity += pnl
        rb = pos.risk_budget
        self.closed.append({
            "signal_time": pos.signal_time,
            "entry_time": pos.fill_time,
            "exit_time": self._t[i],
            "side": pos.side,
            "units": pos.units,
            "entry": pos.entry,
            "exit": px,
            "stop": pos.stop,
            "target": pos.target,
            "pnl": pnl,
            "risk_budget": rb,
            "planned_risk": pos.planned_risk,
            "equity_at_fill": pos.equity_at_fill,
            "r_multiple": pnl / rb if rb > 0 else math.nan,
            "exit_reason": reason,
            "bars_held": i - pos.fill_bar,
            "flags": ",".join(pos.flags),
        })

    # ------------------------------------------------------------ stepping
    def step(self, i: int):
        """Process bar ``i``: fills at its open, then exits within the bar."""
        self.i = i
        due = [o for o in self.pending if o.fill_bar <= i]
        self.pending = [o for o in self.pending if o.fill_bar > i]
        for o in due:
            self._fill(o, i)
        half, slip = self._half[i], self.cond.slippage
        still = []
        for p in self.positions:
            s = p.side
            o, h, lo = self._o[i], self._h[i], self._l[i]
            if not np.isfinite(p.stop):
                stop_hit = False
            elif p.fill_bar < i and (o - p.stop) * s <= 0:
                self._close(p, i, o - s * (half + slip), "gap_stop")
                continue
            else:
                stop_hit = (lo <= p.stop) if s == 1 else (h >= p.stop)
            if "filled_beyond_stop" in p.flags and p.fill_bar == i:
                self._close(p, i, p.entry - s * (2 * half + slip), "gap_stop")
                continue
            if stop_hit:  # pessimistic: stop before target
                self._close(p, i, p.stop - s * (half + slip), "stop")
                continue
            if np.isfinite(p.target):
                if p.fill_bar < i and (o - p.target) * s >= 0:
                    self._close(p, i, p.target - s * half, "target")
                    continue
                tgt_hit = (h >= p.target) if s == 1 else (lo <= p.target)
                if tgt_hit:
                    self._close(p, i, p.target - s * half, "target")
                    continue
            still.append(p)
        self.positions = still

    def close_all(self, reason: str = "end_of_data"):
        i = len(self._c) - 1
        self.i = i
        for p in self.positions:
            self._close(p, i, self._c[i] - p.side * self._half[i], reason)
        self.positions = []
        for o in self.pending:
            self._event("cancelled", reason="end of data", signal_time=o.signal_time)
        self.pending = []

    def trades(self) -> pd.DataFrame:
        cols = ["signal_time", "entry_time", "exit_time", "side", "units", "entry", "exit", "stop",
                "target", "pnl", "risk_budget", "planned_risk", "equity_at_fill", "r_multiple",
                "exit_reason", "bars_held", "flags"]
        return pd.DataFrame(self.closed, columns=cols)


def run_execution(signals: pd.DataFrame, bars: pd.DataFrame, executor, cond: ExecConditions | None = None,
                  seed: int = 0, journal=None, start_equity: float = 100_000.0):
    """Walk bars, step the broker and hand each signal to the executor at its bar.

    Returns ``(trades_df, broker)``. Every closed trade is recorded in the
    journal (if one is given).
    """
    broker = SimBroker(bars, cond, seed=seed, start_equity=start_equity)
    by_bar: dict[int, list] = {}
    if signals is not None and len(signals):
        pos = bars.index.get_indexer(pd.DatetimeIndex(signals["time"]))
        for k, (p, row) in enumerate(zip(pos, signals.to_dict("records"), strict=True)):
            if p < 0:
                broker.events.append({"kind": "signal_off_bar", "bar": -1, "time": row["time"], "row": k})
                continue
            by_bar.setdefault(int(p), []).append(row)
    for i in range(len(bars)):
        broker.step(i)
        for row in by_bar.get(i, ()):
            executor.on_signal(row, broker)
    broker.close_all()
    trades = broker.trades()
    if journal is not None:
        for rec in trades.to_dict("records"):
            journal.record(rec)
    return trades, broker
