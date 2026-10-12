"""Independent Gold / ES scaling strategy, source v2.8.0. No order submission.

Setup S/A/B are components of this ONE strategy. They share the source Book's
numbering/equity and retain separate trade arrays, limits, cooldowns and statistics.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, field

import pandas as pd

from .data import validate
from .features import ema, patterns, pine_round, structures

VERSION = "2.8.0"
STRATEGY_ID = "gold_es_scaling"


@dataclass(frozen=True)
class Profile:
    name: str
    major: float = 100
    mid: float = 50
    major_buffer: float = 15
    mid_buffer: float = 10
    max_stop: float = 50
    stop_pad: float = 1
    breakeven: float = 2
    front_run: float = 10
    entry_range: float = 5
    round_step: float = 1
    value_per_point: float = 100
    quantity_step: float = 0.01
    micro_value: float = 0

    @classmethod
    def gold(cls):
        return cls("XAU/USD")

    @classmethod
    def es(cls):
        return cls(
            "ES / S&P 500",
            major_buffer=10,
            mid_buffer=7,
            front_run=5,
            entry_range=3,
            round_step=0.25,
            value_per_point=50,
            quantity_step=1,
            micro_value=5,
        )

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "name":
                continue
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in (
            "major",
            "mid",
            "max_stop",
            "round_step",
            "value_per_point",
            "quantity_step",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if abs(self.major / self.mid - round(self.major / self.mid)) > 1e-9:
            raise ValueError("major spacing must be an integer multiple of mid spacing")


@dataclass(frozen=True)
class Config:
    profile: Profile = field(default_factory=Profile.gold)
    ema_a: int = 14
    ema_b: int = 50
    chart_seconds: int = 900
    a_seconds: int = 7200
    s_seconds: int = 14400
    near_mode: str = "ATR"
    near_atr: float = 0.5
    near_fixed: float = 3
    min_confluences: int = 3
    pullback_atr: float = 1
    stretch_atr: float = 2
    use_swing: bool = True
    swing_shift: bool = True
    swing_retest_bars: int = 30
    retest_bars: int = 30
    cooldown_bars: int = 1
    entries_per_impulse: int = 2
    max_open: int = 5
    tp_count: int = 5
    min_tp_r: float = 0.5
    setup_pivot: int = 5
    daily_pivot: int = 3
    weekly_pivot: int = 2
    mixed_weekly_blocks: bool = True
    bias_mode: str = "Auto"
    starting_equity: float = 10000
    current_equity: float = 0
    track_from: str = "2026-10-01T00:00:00+00:00"
    base_risk: float = 1
    tier_growth: tuple[float, ...] = (25, 50, 100)
    tier_risk: tuple[float, ...] = (1.5, 2, 3)
    violation_alerts: bool = True

    def __post_init__(self):
        if self.bias_mode not in (
            "Auto",
            "Buys only",
            "Sells only",
        ) or self.near_mode not in ("ATR", "Fixed"):
            raise ValueError("invalid bias/near mode")
        for name in (
            "ema_a",
            "ema_b",
            "setup_pivot",
            "daily_pivot",
            "weekly_pivot",
            "cooldown_bars",
            "retest_bars",
            "swing_retest_bars",
            "chart_seconds",
            "a_seconds",
            "s_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.chart_seconds != 900:
            raise ValueError(
                "This port requires the source recommended 15m chart / Setup B timeframe"
            )
        if self.a_seconds % self.chart_seconds or self.s_seconds % self.chart_seconds:
            raise ValueError(
                "A/S timeframes must be exact multiples of chart timeframe"
            )
        if (
            self.min_confluences not in (2, 3)
            or not 1 <= self.entries_per_impulse <= 10
            or not 1 <= self.max_open <= 20
            or not 1 <= self.tp_count <= 10
        ):
            raise ValueError(
                "confluence/scaling/target parameters outside source bounds"
            )
        if (
            not math.isfinite(self.starting_equity)
            or not math.isfinite(self.current_equity)
            or self.starting_equity <= 0
            or self.current_equity < 0
        ):
            raise ValueError("invalid equity")
        for value in (
            self.near_atr,
            self.near_fixed,
            self.pullback_atr,
            self.stretch_atr,
            self.min_tp_r,
            self.base_risk,
            *self.tier_growth,
            *self.tier_risk,
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError("risk/distance inputs must be finite and nonnegative")
        if (
            len(self.tier_growth) != 3
            or len(self.tier_risk) != 3
            or list(self.tier_growth) != sorted(self.tier_growth)
        ):
            raise ValueError("three ordered equity milestones required")
        if pd.Timestamp(self.track_from).tzinfo is None:
            raise ValueError("track_from must have a timezone")

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


@dataclass
class Trade:
    side: int
    number: int
    pending: bool
    entry: float
    stop: float
    quantity: float
    kind: str
    targets: list[float]
    bar: int
    be: bool = False
    tp_hit: int = 0


@dataclass
class Setup:
    trades: list[Trade] = field(default_factory=list)
    last_bar: int = -1000000
    rearm: int = 0
    impulse_id: int = -2
    impulse_count: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0


@dataclass
class Book:
    week: int = 0
    pnl: float = 0


def level(price, profile):
    value = pine_round(price / profile.mid) * profile.mid
    major = abs(value / profile.major - pine_round(value / profile.major)) < 0.0001
    return value, major, profile.major_buffer if major else profile.mid_buffer


def targets(side, entry, stop_distance, cfg):
    p = cfg.profile
    base = (
        math.floor(entry / p.major) if side > 0 else math.ceil(entry / p.major)
    ) * p.major
    result = []
    for k in range(1, 41):
        tp = pine_round(base + side * k * p.major - side * p.front_run, p.round_step)
        if (tp - entry) * side >= cfg.min_tp_r * stop_distance:
            result.append(tp)
        if len(result) >= cfg.tp_count:
            break
    return result


def rounded_stop(side, entry, swing_stop, profile):
    value = swing_stop
    if (
        not math.isfinite(value)
        or (entry - value) * side <= 0
        or abs(entry - value) > profile.max_stop
    ):
        value = entry - side * profile.max_stop
    return pine_round(value, profile.round_step)


def sizing(equity, distance, cfg):
    p = cfg.profile
    growth = (equity / cfg.starting_equity - 1) * 100
    risk = cfg.base_risk
    for threshold, value in zip(cfg.tier_growth, cfg.tier_risk, strict=True):
        if growth >= threshold:
            risk = value
    dollars = equity * risk / 100
    qty = (
        math.floor(dollars / (distance * p.value_per_point) / p.quantity_step + 1e-9)
        * p.quantity_step
        if distance > 0
        else 0
    )
    micros = (
        math.floor(dollars / (distance * p.micro_value))
        if distance > 0 and p.micro_value > 0 and qty <= 0
        else None
    )
    return risk, dollars, max(qty, 0), micros


def scan(frame, ema_length, cfg):
    p = cfg.profile
    ss = structures(frame, cfg.setup_pivot)
    pp, av = patterns(frame)
    ev = ema(frame.close.to_numpy(float), ema_length)
    leg_high = leg_low = math.nan
    bull_bar = bear_bar = -1
    run_max = 0
    live = {1: None, -1: None}
    rows = []
    for t, bar in enumerate(frame.itertuples()):
        s = ss[t]
        if s["bull"]:
            leg_high, bull_bar = bar.high, t
        elif math.isfinite(leg_high):
            leg_high = max(leg_high, bar.high)
        if s["bear"]:
            leg_low, bear_bar = bar.low, t
        elif math.isfinite(leg_low):
            leg_low = min(leg_low, bar.low)
        tol = cfg.near_atr * av[t] if cfg.near_mode == "ATR" else cfg.near_fixed
        stretched = run_max >= cfg.stretch_atr * av[t]
        touch = bar.low <= ev[t] + tol and bar.high >= ev[t] - tol
        run_max = 0 if touch else max(run_max, abs(bar.close - ev[t]))
        row = dict(
            **s,
            ema=ev[t],
            sig_l=0,
            sig_s=0,
            text_l="",
            text_s="",
            stop_l=s["low"] - p.stop_pad,
            stop_s=s["high"] + p.stop_pad,
            imp_l=bull_bar,
            imp_s=bear_bar,
        )
        for side in (1, -1):
            pat = pp[t][0 if side > 0 else 1]
            extreme = bar.low if side > 0 else bar.high
            lv, mj, buf = level(extreme, p)
            at = (
                bar.low <= lv + buf and bar.close >= lv - buf
                if side > 0
                else bar.high >= lv - buf and bar.close <= lv + buf
            )
            near = (
                bar.low <= ev[t] + tol and bar.close > ev[t]
                if side > 0
                else bar.high >= ev[t] - tol and bar.close < ev[t]
            )
            corr = s["trend"] == side and (
                t > bull_bar
                and math.isfinite(leg_high)
                and leg_high - bar.low >= cfg.pullback_atr * av[t]
                and bar.close > s["low"]
                if side > 0
                else t > bear_bar
                and math.isfinite(leg_low)
                and bar.high - leg_low >= cfg.pullback_atr * av[t]
                and bar.close < s["high"]
            )
            pullback = (
                s["trend"] == side
                and side * (bar.close - ev[t]) > 0
                and corr
                and at
                and int(bool(pat)) + int(at) + int(near) >= cfg.min_confluences
            )
            bk, bkm, bkb = level(
                (
                    math.floor(bar.close / p.mid)
                    if side > 0
                    else math.ceil(bar.close / p.mid)
                )
                * p.mid,
                p,
            )
            old = live[side]
            previous = frame.close.iloc[t - 1] if t else math.nan
            brk = (
                s["trend"] == side
                and side * (bar.close - ev[t]) > 0
                and side * (bar.close - bk) > bkb
                and side * (previous - bk) <= bkb
                and not (old and old[0] == bk)
            )
            ret = bool(
                old
                and t > old[3]
                and (
                    bar.low <= old[0] + old[1] and bar.close >= old[0] - old[1]
                    if side > 0
                    else bar.high >= old[0] - old[1] and bar.close <= old[0] + old[1]
                )
                and pat
                and side * (bar.close - ev[t]) > 0
                and s["trend"] == side
            )
            if old and (
                ret
                or side * (bar.close - old[0]) < -old[1]
                or t - old[3] > cfg.retest_bars
            ):
                live[side] = None
            if brk:
                live[side] = (bk, bkb, bkm, t)
            sig = 1 if pullback else 2 if ret else 3 if brk else 0
            text = (
                f"{pat or 'no candle'} | {lv:g} {'major' if mj else 'mid'} zone"
                + (f" | at EMA{ema_length}" if near else "")
                if sig == 1
                else f"Retest of broken {old[0]:g} level | {pat}"
                if sig == 2
                else f"Closed through {bk:g} zone; limit on retest"
                if sig == 3
                else ""
            )
            if stretched:
                text += f" | Stretched + snap-back to EMA{ema_length}"
            if t and (
                bar.close > bar.open and bar.close > frame.high.iloc[t - 1]
                if side > 0
                else bar.close < bar.open and bar.close < frame.low.iloc[t - 1]
            ):
                nxt = (
                    math.floor(bar.close / p.mid) + 1
                    if side > 0
                    else math.ceil(bar.close / p.mid) - 1
                ) * p.mid
                text += f" | Impulse -> Correction -> Continuation into {nxt:g}"
            suffix = "l" if side > 0 else "s"
            row["sig_" + suffix] = sig
            row["text_" + suffix] = text
            row["limit_" + suffix] = bk + side * bkb
        rows.append(row)
    return rows


def daily(frame, cfg):
    p = cfg.profile
    pp, _ = patterns(frame)
    ss = structures(frame, cfg.daily_pivot)
    rows = []
    for t, bar in enumerate(frame.itertuples()):
        row = dict(**ss[t], sig_l=0, sig_s=0, text_l="", text_s="")
        for side in (1, -1):
            pat = pp[t][0 if side > 0 else 1]
            lv, mj, buf = level(bar.low if side > 0 else bar.high, p)
            rev = bool(
                pat
                and (
                    bar.low <= lv + buf and bar.close >= lv - buf
                    if side > 0
                    else bar.high >= lv - buf and bar.close <= lv + buf
                )
            )
            bk, bkm, bkb = level(
                (
                    math.floor(bar.close / p.mid)
                    if side > 0
                    else math.ceil(bar.close / p.mid)
                )
                * p.mid,
                p,
            )
            previous = frame.close.iloc[t - 1] if t else math.nan
            suffix = "l" if side > 0 else "s"
            row.update(
                {
                    f"rev_{suffix}": rev,
                    f"text_{suffix}": f"{pat} closed at {lv:g} zone",
                    f"break_{suffix}": side * (bar.close - bk) > 0
                    and side * (previous - bk) <= 0,
                    f"level_{suffix}": bk,
                    f"buffer_{suffix}": bkb,
                    f"major_{suffix}": bkm,
                }
            )
        rows.append(row)
    return rows


class ScalingStrategy:
    def __init__(self, config=None):
        self.cfg = config or Config()

    def _event(self, events, kind, setup="", trade=None, **values):
        result = dict(event=kind, setup=setup, **values)
        if trade:
            result["trade"] = asdict(trade)
        events.append(result)

    @staticmethod
    def _finish(setup, result):
        if result > 0:
            setup.wins += 1
        elif result < 0:
            setup.losses += 1
        else:
            setup.breakevens += 1

    def manage(self, su, book, label, bar, index, bias, state, is_close, ratio, events):
        cfg, p = self.cfg, self.cfg.profile
        for tr in list(reversed(su.trades)):
            d = tr.side
            if tr.pending:
                if bar.low <= tr.entry if d > 0 else bar.high >= tr.entry:
                    tr.pending = False
                    tr.bar = index
                    self._event(events, "LIMIT_FILLED", label, tr)
                    # Source defers stop/BE/TP processing to NEXT chart bar after a pending fill.
                elif index - tr.bar > cfg.retest_bars * ratio:
                    self._event(events, "LIMIT_CANCELLED", label, tr)
                    su.trades.remove(tr)
                continue
            hit = bar.low <= tr.stop if d > 0 else bar.high >= tr.stop
            if hit:
                pnl = (tr.stop - tr.entry) * d * tr.quantity * p.value_per_point
                if bar.Index >= pd.Timestamp(cfg.track_from):
                    book.pnl += pnl
                valid = bias == d and state.get("trend", 0) == d
                su.rearm = d if valid else 0
                self._event(
                    events,
                    "BREAKEVEN_STOP" if tr.be else "STOP_LOSS",
                    label,
                    tr,
                    pnl=pnl,
                    reentry_armed=valid,
                )
                self._finish(su, 1 if tr.tp_hit > 0 else 0 if tr.be else -1)
                su.trades.remove(tr)
                continue
            if not tr.be and (
                bar.high >= tr.entry + p.breakeven
                if d > 0
                else bar.low <= tr.entry - p.breakeven
            ):
                tr.stop = tr.entry
                tr.be = True
                self._event(events, "MOVE_TO_BREAKEVEN", label, tr)
            while tr.tp_hit < len(tr.targets) and (
                bar.high >= tr.targets[tr.tp_hit]
                if d > 0
                else bar.low <= tr.targets[tr.tp_hit]
            ):
                tr.tp_hit += 1
                self._event(
                    events,
                    "TARGET_HIT",
                    label,
                    tr,
                    target_number=tr.tp_hit,
                    partial_optional=True,
                )
            if is_close and state.get("bear" if d > 0 else "bull", False):
                pnl = (bar.close - tr.entry) * d * tr.quantity * p.value_per_point
                if bar.Index >= pd.Timestamp(cfg.track_from):
                    book.pnl += pnl
                self._event(
                    events, "STRUCTURE_EXIT", label, tr, pnl=pnl, exit_price=bar.close
                )
                su.rearm = 0
                self._finish(
                    su,
                    1
                    if tr.tp_hit > 0 or (bar.close - tr.entry) * d > 0
                    else -1
                    if (bar.close - tr.entry) * d < 0
                    else 0,
                )
                su.trades.remove(tr)

    def enter(self, su, book, label, bar, index, bias, state, is_close, ratio, events):
        if not is_close:
            return
        cfg, p = self.cfg, self.cfg.profile
        side = (
            1
            if bias == 1 and state.get("sig_l", 0) > 0
            else -1
            if bias == -1 and state.get("sig_s", 0) > 0
            else 0
        )
        if (
            not side
            or index - su.last_bar < cfg.cooldown_bars * ratio
            or len(su.trades) >= cfg.max_open
        ):
            return
        impulse = state.get("imp_l", -1) if side > 0 else -state.get("imp_s", -1) - 2
        same = impulse == su.impulse_id
        if same and su.impulse_count >= cfg.entries_per_impulse:
            return
        suffix = "l" if side > 0 else "s"
        code = state["sig_" + suffix]
        kind = {
            1: "PULLBACK",
            2: "RETEST",
            3: "BREAKOUT",
            4: "DAILY REVERSAL",
            5: "DAILY STRUCTURE SHIFT",
        }[code]
        entry = pine_round(
            state["limit_" + suffix] if code == 3 else bar.close, p.round_step
        )
        stop = rounded_stop(side, entry, state.get("stop_" + suffix, math.nan), p)
        distance = abs(entry - stop)
        eq = (
            cfg.current_equity
            if cfg.current_equity > 0
            else cfg.starting_equity + book.pnl
        )
        risk, usd, qty, micros = sizing(eq, distance, cfg)
        book.week += 1
        tr = Trade(
            side,
            book.week,
            code == 3,
            entry,
            stop,
            qty,
            kind,
            targets(side, entry, distance, cfg),
            index,
        )
        reentry = su.rearm == side
        self._event(
            events,
            "ENTRY",
            label,
            tr,
            signal_code=code,
            reentry=reentry,
            scaling_in=len(su.trades) > 0,
            impulse_entry=su.impulse_count + 1 if same else 1,
            risk_percent=risk,
            risk_amount=usd,
            equity=eq,
            micro_equivalent=micros,
            quantity_below_minimum=qty <= 0,
            reason=state.get("text_" + suffix, ""),
            entry_range=[
                pine_round(bar.close - p.entry_range, p.round_step),
                pine_round(bar.close + p.entry_range, p.round_step),
            ]
            if code != 3
            else None,
        )
        su.trades.append(tr)
        su.impulse_count = su.impulse_count + 1 if same else 1
        su.impulse_id = impulse
        su.last_bar = index
        su.rearm = 0

    def evaluate(self, frames):
        cfg = self.cfg
        required = ("chart", "a", "s", "daily", "weekly")
        if set(frames) != set(required):
            raise ValueError(f"exactly {required} data frames are required")
        for frame in frames.values():
            validate(frame)
        features = {
            "a": scan(frames["a"], cfg.ema_a, cfg),
            "chart": scan(frames["chart"], cfg.ema_b, cfg),
            "s": structures(frames["s"], cfg.setup_pivot),
            "daily": daily(frames["daily"], cfg),
            "weekly": structures(frames["weekly"], cfg.weekly_pivot),
        }
        pats, _ = patterns(frames["s"])
        for i, row in enumerate(features["s"]):
            row.update(
                pattern_l=pats[i][0],
                pattern_s=pats[i][1],
                stop_l=row["low"] - cfg.profile.stop_pad,
                stop_s=row["high"] + cfg.profile.stop_pad,
            )
        closes = {key: list(frames[key].time_close) for key in required}
        setups = {key: Setup() for key in ("S", "A", "B")}
        book = Book()
        swing_break = None
        previous_auto = 0
        previous_open = None
        history = []
        empty = dict(
            trend=0,
            bull=False,
            bear=False,
            high=math.nan,
            low=math.nan,
            prev_high=math.nan,
            prev_low=math.nan,
        )
        for i, bar in enumerate(frames["chart"].itertuples()):
            now = bar.time_close
            events = []
            if previous_open is not None:
                dow = bar.Index.tz_convert("America/New_York").isoweekday() % 7
                prevdow = previous_open.tz_convert("America/New_York").isoweekday() % 7
                if dow < prevdow or bar.Index - previous_open >= pd.Timedelta(days=7):
                    book.week = 0
            previous_open = bar.Index
            # f_htf(...)[1] + lookahead_on: bias uses HTF bars closed by chart OPEN,
            # even while the new daily candle's own reversal is evaluated on its CLOSE.
            di = bisect_right(closes["daily"], bar.Index) - 1
            wi = bisect_right(closes["weekly"], bar.Index) - 1
            ds = features["daily"][di] if di >= 0 else empty
            ws = features["weekly"][wi] if wi >= 0 else empty
            mixed = (
                cfg.mixed_weekly_blocks
                and math.isfinite(ws["prev_high"])
                and math.isfinite(ws["prev_low"])
                and ((ws["high"] > ws["prev_high"]) != (ws["low"] > ws["prev_low"]))
            )
            wb = 0 if mixed else ws["trend"]
            auto = wb if wb != 0 and ds["trend"] == wb else 0
            bias = (
                1
                if cfg.bias_mode == "Buys only"
                else -1
                if cfg.bias_mode == "Sells only"
                else auto
            )
            bias_flip = auto != 0 and auto != previous_auto
            indices = {
                key: bisect_right(closes[key], now) - 1 for key in ("a", "s", "daily")
            }
            isclose = {
                key: indices[key] >= 0 and closes[key][indices[key]] == now
                for key in indices
            }
            h = features["s"][indices["s"]] if indices["s"] >= 0 else empty
            hbar = frames["s"].iloc[indices["s"]] if indices["s"] >= 0 else None
            dd = features["daily"][indices["daily"]] if indices["daily"] >= 0 else empty
            ss = dict(
                h,
                sig_l=0,
                sig_s=0,
                text_l="",
                text_s="",
                imp_l=int(now.timestamp() // 86400),
                imp_s=int(now.timestamp() // 86400),
            )
            if cfg.use_swing:
                if isclose["daily"]:
                    for side in (1, -1):
                        suffix = "l" if side > 0 else "s"
                        if dd["break_" + suffix] and bias == side:
                            swing_break = (
                                side,
                                dd["level_" + suffix],
                                dd["buffer_" + suffix],
                                i,
                            )
                            self._event(
                                events,
                                "DAILY_BREAK_ACCEPTED",
                                "S",
                                side=side,
                                level=swing_break[1],
                            )
                    if swing_break and swing_break[3] != i:
                        side, lv, buf, _ = swing_break
                        opposite = dd["break_s" if side > 0 else "break_l"]
                        if opposite or side * (bar.close - lv) < -buf:
                            self._event(events, "DAILY_BREAK_CANCELLED", "S", level=lv)
                            swing_break = None
                if swing_break and i - swing_break[3] > cfg.swing_retest_bars * (
                    cfg.s_seconds // cfg.chart_seconds
                ):
                    swing_break = None
                if isclose["s"] and swing_break and hbar is not None:
                    side, lv, buf, _ = swing_break
                    suffix = "l" if side > 0 else "s"
                    good = (
                        hbar.low <= lv + buf and hbar.close > lv
                        if side > 0
                        else hbar.high >= lv - buf and hbar.close < lv
                    )
                    if good and h["pattern_" + suffix] and h["trend"] != -side:
                        ss["sig_" + suffix] = 2
                        ss["text_" + suffix] = (
                            f"Retest of {lv:g} after daily close | 4H {h['pattern_' + suffix]}"
                        )
                        swing_break = None
                # Source priority: S retest, Daily long reversal, Daily short reversal, then shift.
                if not ss["sig_l"] and not ss["sig_s"]:
                    if isclose["daily"] and dd["rev_l"] and h["trend"] != -1:
                        ss["sig_l"], ss["text_l"] = (
                            4,
                            "Daily close confirmed: " + dd["text_l"],
                        )
                    elif isclose["daily"] and dd["rev_s"] and h["trend"] != 1:
                        ss["sig_s"], ss["text_s"] = (
                            4,
                            "Daily close confirmed: " + dd["text_s"],
                        )
                    elif cfg.swing_shift and bias_flip and h["trend"] != -auto:
                        suffix = "l" if auto > 0 else "s"
                        ss["sig_" + suffix] = 5
                        ss["text_" + suffix] = "Daily closed back in line with Weekly"
            # S management only uses BOS when its own H4 bar closes.
            ss["bull"] = isclose["s"] and h["bull"]
            ss["bear"] = isclose["s"] and h["bear"]
            a = features["a"][indices["a"]] if indices["a"] >= 0 else empty
            b = features["chart"][i]
            for label, state, closed, ratio in (
                (
                    "S",
                    ss,
                    isclose["daily"] or isclose["s"] or bias_flip,
                    cfg.s_seconds // cfg.chart_seconds,
                ),
                ("A", a, isclose["a"], cfg.a_seconds // cfg.chart_seconds),
                ("B", b, True, 1),
            ):
                self.manage(
                    setups[label],
                    book,
                    label,
                    bar,
                    i,
                    bias,
                    state,
                    closed,
                    ratio,
                    events,
                )
                self.enter(
                    setups[label],
                    book,
                    label,
                    bar,
                    i,
                    bias,
                    state,
                    closed,
                    ratio,
                    events,
                )
            if cfg.bias_mode == "Auto" and auto != previous_auto:
                self._event(
                    events,
                    "BIAS_CHANGED",
                    bias=auto,
                    weekly=wb,
                    daily=ds["trend"],
                    mixed_weekly=mixed,
                )
            if cfg.violation_alerts:
                longs = any(tr.side == 1 for su in setups.values() for tr in su.trades)
                shorts = any(
                    tr.side == -1 for su in setups.values() for tr in su.trades
                )
                watch_long = longs or (not shorts and bias == 1)
                watch_short = shorts or (not longs and bias == -1)
                for tf, state, closed in (
                    ("4H", h, isclose["s"]),
                    ("Daily", dd, isclose["daily"]),
                ):
                    if closed:
                        if watch_long and state["bear"]:
                            self._event(
                                events,
                                "STRUCTURE_VIOLATION",
                                timeframe=tf,
                                side=1,
                                level=state["low"],
                            )
                        if watch_short and state["bull"]:
                            self._event(
                                events,
                                "STRUCTURE_VIOLATION",
                                timeframe=tf,
                                side=-1,
                                level=state["high"],
                            )
            previous_auto = auto
            history.append(
                dict(
                    time_open=bar.Index.isoformat(),
                    time_close=now.isoformat(),
                    time_new_york=now.tz_convert("America/New_York").isoformat(),
                    bias=bias,
                    auto_bias=auto,
                    weekly=wb,
                    daily=ds["trend"],
                    mixed_weekly=mixed,
                    events=events,
                    book=asdict(book),
                    equity=cfg.current_equity
                    if cfg.current_equity > 0
                    else cfg.starting_equity + book.pnl,
                    setups={key: asdict(su) for key, su in setups.items()},
                )
            )
        return history
