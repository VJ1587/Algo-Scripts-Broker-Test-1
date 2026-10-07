#!/usr/bin/env python3
"""
Trade gate: the hold between the scanner and any order
======================================================

The scanner (scanner.py) evaluates whether a setup is enough to take a trade. This script is the
only path from a scanned setup to an order, and nothing reaches a broker without the owner's answer
to every question below:

    1. Take this trade?                       yes / no for each qualified setup in the latest scan
    2. Which account(s)?                      from accounts.yaml (MT5 accounts and manual-only accounts)
    3. What modifications?                    entries, stop, targets, lots, route; proposed from v1.0
    4. Who places it?                         algo (MT5 order sent by this script) or manual (you place it)
    5. Final ticket                           typed CONFIRM, otherwise nothing happens

Proposed levels and sizing reuse BROKER-v1.1 rules from broker_v11_algo.py so research and the gate
cannot drift apart: ladder 38.2 / 50 / 61.8 % of the impulse (weights 20 / 30 / 50), FX stop at the
89.3 % retracement, TP1 and TP2 at the 27 % and 61.8 % extensions, idea risk 2 % of min(balance,
equity) capped by 5 % portfolio risk, quantities rounded down.

Safety rules [OWNER gate-0.1.0]
    - Algo placement is off unless the account has allow_algo: true. Live (real money) accounts also
      need allow_live_algo: true at the top of accounts.yaml (demo first). Algo Trading must be on
      in the MT5 terminal. The terminal's logged in account must match the configured login.
    - Algo orders above the 2 % / 5 % risk ceilings are refused. Manual tickets above them need the
      word OVERRIDE and are logged as overrides.
    - Every request passes MT5 order_check before any order is sent; one failure sends nothing.
    - Every decision (skipped, cancelled, approved manual, sent, failed) is logged to
      logs/trade_decisions.csv and tickets/<ticket id>.json, and shows on output/journal.html.
    - The gate opens trades only. Trailing the stop and closing half at TP1 beyond the order split
      are not automated.

Usage
    python trade_gate.py                          # latest live scan, qualified setups
    python trade_gate.py --include-developing     # also list developing setups (2+ checks, not qualified)
    python trade_gate.py --scan output/scan_<stamp>_<run>.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import pandas as pd
import yaml

import broker_v11_algo as bv
import news as newsmod
import scanner as sc

GATE_VERSION = "0.1.0"
MAGIC = 5110001                 # marks every order this gate sends
CONFIRM_WORD = "CONFIRM"
OVERRIDE_WORD = "OVERRIDE"
RULES = bv.StrategyConfig()     # v1.0 geometry and risk ceilings, never edited here
LOG = logging.getLogger("trade_gate")
BASE = Path(__file__).resolve().parent

DECISION_FIELDS = ["time_utc", "ticket_id", "scan_stamp", "symbol", "direction", "section", "total",
                   "c1", "c2", "c3", "c4", "c5", "c6", "decision", "account", "mode", "route", "entries",
                   "lots", "stop", "tp1", "tp2", "risk", "allowed_risk", "currency", "modifications",
                   "orders", "notes"]


# =============================================================================
# Accounts
# =============================================================================

@dataclass
class Account:
    id: str
    label: str
    platform: str                                   # mt5 | manual
    login: Optional[int] = None
    server: str = ""
    terminal_path: Optional[str] = None
    allow_algo: bool = False
    currency: str = "USD"
    commission_per_lot_rt: float = 0.0              # account currency per lot, round trip
    slippage_ticks: int = 0                         # expected stop slippage, in ticks
    symbols: dict = field(default_factory=dict)     # scanner symbol -> broker symbol
    instruments: list = field(default_factory=list) # scanner symbols this account trades; empty = all
    value_per_point: dict = field(default_factory=dict)  # manual: account ccy per 1.0 price move per lot

    def trades(self, symbol: str) -> bool:
        return not self.instruments or symbol in self.instruments

    def venue_symbol(self, symbol: str) -> str:
        return self.symbols.get(symbol, symbol)


def load_accounts(path: Path) -> tuple[list[Account], dict]:
    if not path.exists():
        raise SystemExit(f"{path.name} not found. Copy accounts.example.yaml to accounts.yaml and fill it in.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    settings = {"allow_live_algo": bool(raw.get("allow_live_algo", False)),
                "max_scan_age_hours": float(raw.get("max_scan_age_hours", 12)),
                "ladder_expiry_hours": float(raw.get("ladder_expiry_hours", 24)),
                "max_quote_age_seconds": float(raw.get("max_quote_age_seconds", 30))}
    accts = []
    for a in raw.get("accounts", []):
        if a.get("platform") not in ("mt5", "manual"):
            raise ValueError(f"account {a.get('id')}: platform must be mt5 or manual")
        accts.append(Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__}))
    if not accts:
        raise SystemExit(f"{path.name} lists no accounts")
    return accts, settings


def algo_permission(acct: Account, settings: dict, account_mode: Optional[str],
                    terminal_algo_on: Optional[bool]) -> tuple[bool, str]:
    """[OWNER gate-0.1.0] Demo first: live algo orders need two explicit switches plus the terminal's."""
    if acct.platform != "mt5":
        return False, "manual-only account"
    if not acct.allow_algo:
        return False, "allow_algo is off for this account in accounts.yaml"
    if account_mode is None:
        return False, "MT5 account not connected"
    if account_mode != "demo" and not settings.get("allow_live_algo", False):
        return False, f"{account_mode} account: live algo orders are off until allow_live_algo: true (demo first)"
    if not terminal_algo_on:
        return False, "Algo Trading is switched off in the MT5 terminal"
    return True, ""


# =============================================================================
# Scan
# =============================================================================

def latest_scan(out_dir: Path) -> Path:
    for p in sorted(out_dir.glob("scan_*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not d.get("meta", {}).get("demo"):
            return p
    raise SystemExit(f"no live scan found in {out_dir}; run scanner.py first")


def candidates(scan: dict, include_developing: bool) -> list[dict]:
    keep = ("qualified", "developing") if include_developing else ("qualified",)
    rows = [r for r in scan.get("rows", []) if r.get("section") in keep]
    return sorted(rows, key=lambda r: ({"A": 0, "B": 1}.get(r.get("grade") or "", 2), r.get("rank") or 99,
                                       -r.get("total", 0)))


# =============================================================================
# Levels and sizing  [v1.0 Sections 5 to 7 via broker_v11_algo]
# =============================================================================

def tick_of(inst: sc.Instrument) -> Decimal:
    return bv.D(inst.tick)


def propose_levels(row: dict, inst: sc.Instrument) -> tuple[list[Decimal], Optional[Decimal], Optional[Decimal],
                                                             Optional[Decimal], list[str]]:
    """Ladder entries, stop, TP1 and TP2 from the impulse the scanner logged. Same formulas as
    broker_v11_algo.Backtester._plan. Missing pieces come back as None with a note."""
    notes: list[str] = []
    imp = row.get("impulse") or {}
    if "A" not in imp or "B" not in imp:
        return [], None, None, None, ["scan logged no impulse: enter entries, stop and targets yourself"]
    d = 1 if row["direction"] == sc.LONG else -1
    tick = tick_of(inst)
    A, B = bv.D(imp["A"]), bv.D(imp["B"])
    rng = B - A
    toward_entry = "down" if d == 1 else "up"
    entries = [bv.round_price(B - r * rng, tick, toward_entry) for r in RULES.ladder_ratios]
    tp1 = bv.round_price(B + RULES.tp1_extension * rng, tick, toward_entry)
    tp2 = bv.round_price(B + RULES.tp2_extension * rng, tick, toward_entry)
    if inst.asset == "fx":
        stop = bv.round_price(B - RULES.fx_stop_ratio * rng, tick, "nearest")
    else:
        stop = None
        notes.append("v1.0 stop for gold, S&P and oil is structural (last 4H pivot beyond the zone minus "
                     "0.10 x 4H ATR); the scan does not carry it, so enter the stop")
    if d == -1 and inst.symbol in RULES.long_only_symbols:
        notes.append(f"v1.0 trades {inst.symbol} long only; this short is the short-test experiment")
    return entries, stop, tp1, tp2, notes


def validate_levels(direction: str, entries: list[Decimal], stop: Optional[Decimal], tp1: Optional[Decimal],
                    tp2: Optional[Decimal], min_stop: Decimal = Decimal(0)) -> list[str]:
    d = 1 if direction == sc.LONG else -1
    errs = []
    if not entries or any(e <= 0 for e in entries):
        errs.append("no entry price" if not entries else "an entry price is missing (0)")
    if stop is None:
        errs.append("no stop")
    if tp1 is None:
        errs.append("no TP1")
    if errs:
        return errs
    for e in entries:
        if (e - stop) * d <= 0:
            errs.append(f"stop {stop} is not beyond entry {e}")
        elif abs(e - stop) < min_stop:
            errs.append(f"entry {e} is closer to the stop than the broker minimum {min_stop}")
        if (tp1 - e) * d <= 0:
            errs.append(f"TP1 {tp1} is not beyond entry {e}")
    if tp2 is not None and (tp2 - tp1) * d < 0:
        errs.append("TP2 is closer than TP1")
    return errs


@dataclass
class SizeSpec:
    value_per_point: Optional[Decimal]   # account currency per 1.0 price move per lot
    vol_min: Decimal = Decimal("0.01")
    vol_max: Decimal = Decimal("100")
    vol_step: Decimal = Decimal("0.01")
    min_stop: Decimal = Decimal(0)
    source: str = ""


def cost_per_lot(acct: Account, spec: SizeSpec, tick: Decimal) -> Decimal:
    if spec.value_per_point is None:
        return Decimal(0)
    return bv.D(acct.commission_per_lot_rt) + Decimal(acct.slippage_ticks) * tick * spec.value_per_point


def plan_risk(entries: list[Decimal], lots: list[Decimal], stop: Decimal, spec: SizeSpec, cost: Decimal) -> Optional[Decimal]:
    if spec.value_per_point is None or stop is None:
        return None
    return sum((q * bv.loss_per_lot(e, stop, spec.value_per_point, cost) for e, q in zip(entries, lots)), Decimal(0))


def size_lots(entries: list[Decimal], weights: list[Decimal], stop: Decimal, spec: SizeSpec, cost: Decimal,
              allowed: Decimal) -> tuple[list[Decimal], str]:
    """v1.0 sizing: lots = allowed risk / weighted loss per lot, split by weight, each rounded DOWN."""
    if spec.value_per_point is None:
        return [], "no contract value for this account and symbol: enter lots yourself"
    venue = SimpleNamespace(qty_step=spec.vol_step, min_qty=spec.vol_min, max_qty=spec.vol_max)
    res = bv.size_ladder(entries, weights, stop, spec.value_per_point, cost, allowed, venue)
    return res.quantities, ("" if res.ok else res.reason)


def split_for_targets(lots: Decimal, step: Decimal, vol_min: Decimal) -> list[tuple[Decimal, str]]:
    """[v1.0 6] Half the leg closes at TP1, the rest runs to TP2. MT5 holds one TP per order, so each
    leg is sent as two orders. A leg too small to split goes as one order at TP1."""
    first = bv.round_qty_down(lots * RULES.tp1_close_fraction, step)
    rest = lots - first
    if first < vol_min or rest < vol_min:
        return [(lots, "tp1")]
    return [(first, "tp1"), (rest, "tp2")]


# =============================================================================
# Prompting (injectable so the flow is testable)
# =============================================================================

class Prompter:
    def __init__(self, input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print):
        self.input, self.say = input_fn, print_fn

    def ask(self, q: str, default: str = "") -> str:
        try:
            a = self.input(f"{q}{f' [{default}]' if default else ''}: ").strip()
        except EOFError:
            a = ""
        return a or default

    def yes(self, q: str) -> bool:
        return self.ask(f"{q} (y/N)").lower() in ("y", "yes")


def fmt_levels(xs) -> str:
    return ", ".join(str(x) for x in xs) if xs else "-"


# =============================================================================
# Broker connection per account
# =============================================================================

class Mt5Session:
    """One MT5 terminal per account. Verifies the terminal is logged in to the configured login."""

    def __init__(self, acct: Account):
        self.acct = acct
        self.client = sc.Mt5Client({"mt5": {"terminal_path": acct.terminal_path, "server_time": "ny_close",
                                            "symbol_map": acct.symbols}})
        self.mode: Optional[str] = None
        self.algo_on: Optional[bool] = None
        self.error = ""

    def open(self) -> bool:
        if not self.client.connect():
            self.error = self.client.status
            return False
        m = self.client.mt5
        info = m.account_info()
        if self.acct.login and info.login != int(self.acct.login):
            self.error = f"terminal is logged in to {info.login}, not {self.acct.login}"
            self.client.shutdown()
            return False
        self.mode = {0: "demo", 1: "contest", 2: "real"}.get(info.trade_mode, "real")
        self.algo_on = bool(m.terminal_info().trade_allowed)
        return True

    def close(self) -> None:
        self.client.shutdown()

    @property
    def mt5(self):
        return self.client.mt5

    def size_spec(self, symbol: str) -> SizeSpec:
        s = self.mt5.symbol_info(self.acct.venue_symbol(symbol))
        if s is None:
            raise ValueError(f"{self.acct.venue_symbol(symbol)} not offered on {self.acct.label}")
        self.mt5.symbol_select(s.name, True)
        return SizeSpec(value_per_point=bv.D(s.trade_tick_value) / bv.D(s.trade_tick_size),
                        vol_min=bv.D(s.volume_min), vol_max=bv.D(s.volume_max), vol_step=bv.D(s.volume_step),
                        min_stop=bv.D(s.trade_stops_level) * bv.D(s.point), source=f"MT5 {s.name}")

    def capital(self) -> tuple[Decimal, Decimal, list[str]]:
        """Return (K = min(balance, equity), risk already open or pending, warnings)."""
        a = self.mt5.account_info()
        warns, used = [], Decimal(0)
        for p in list(self.mt5.positions_get() or []) + list(self.mt5.orders_get() or []):
            sym = p.symbol
            price = getattr(p, "price_open", 0.0)
            vol = getattr(p, "volume", None) or getattr(p, "volume_current", 0.0)
            if not p.sl:
                warns.append(f"{sym} #{p.ticket} has no stop: its risk is not counted (unbounded)")
                continue
            s = self.mt5.symbol_info(sym)
            v = bv.D(s.trade_tick_value) / bv.D(s.trade_tick_size)
            used += abs(bv.D(price) - bv.D(p.sl)) * v * bv.D(vol)
        return bv.capital_base(bv.D(a.balance), bv.D(a.equity)), used, warns

    def quote(self, symbol: str) -> tuple[Optional[Decimal], Optional[Decimal], float]:
        tk = self.mt5.symbol_info_tick(self.acct.venue_symbol(symbol))
        if not tk or not tk.bid:
            return None, None, float("inf")
        age = (pd.Timestamp.now(tz=sc.UTC) - self.client.ts(tk.time)).total_seconds()
        return bv.D(tk.bid), bv.D(tk.ask), age


# =============================================================================
# Ticket
# =============================================================================

@dataclass
class Ticket:
    ticket_id: str
    scan_stamp: str
    row: dict
    account: Account
    route: str = "L"                      # L = 3 leg limit ladder, M = market now
    entries: list = field(default_factory=list)
    weights: list = field(default_factory=list)
    stop: Optional[Decimal] = None
    tp1: Optional[Decimal] = None
    tp2: Optional[Decimal] = None
    lots: list = field(default_factory=list)
    lots_manual: bool = False
    risk: Optional[Decimal] = None
    allowed: Optional[Decimal] = None
    capital: Optional[Decimal] = None
    mode: str = "manual"
    notes: list = field(default_factory=list)
    modifications: list = field(default_factory=list)
    orders: list = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return self.row["symbol"]

    @property
    def direction(self) -> str:
        return self.row["direction"]

    def lines(self) -> list[str]:
        cur = self.account.currency
        out = [f"Ticket {self.ticket_id}  {self.symbol} {self.direction.upper()}  account {self.account.label}  "
               f"route {'ladder (3 limits)' if self.route == 'L' else 'market now'}",
               f"  entries {fmt_levels(self.entries)}   weights {fmt_levels(self.weights)}",
               f"  stop {self.stop}   TP1 {self.tp1}   TP2 {self.tp2}",
               f"  lots {fmt_levels(self.lots)}{' (entered by you)' if self.lots_manual else ''}"]
        if self.risk is not None:
            out.append(f"  risk at stop {self.risk:,.2f} {cur}   allowed {self.allowed:,.2f} {cur} "
                       f"(2% of {self.capital:,.2f}, within the 5% portfolio cap)"
                       if self.allowed is not None and self.capital is not None else f"  risk at stop {self.risk:,.2f} {cur}")
        out += [f"  note: {n}" for n in self.notes]
        if self.modifications:
            out.append(f"  your changes: {'; '.join(self.modifications)}")
        return out

    def over_risk(self) -> bool:
        return self.risk is not None and self.allowed is not None and self.risk > self.allowed + Decimal("0.005")


def decision_row(t: Ticket, decision: str, now: pd.Timestamp) -> dict:
    r = t.row
    return {"time_utc": now.strftime("%Y-%m-%d %H:%M:%S"), "ticket_id": t.ticket_id, "scan_stamp": t.scan_stamp,
            "symbol": t.symbol, "direction": t.direction, "section": sc.section_label(r), "total": r.get("total"),
            **{k: int(sc.flag(r.get(k))) for k in sc.CONF_KEYS},
            "decision": decision, "account": t.account.id, "mode": t.mode, "route": t.route,
            "entries": fmt_levels(t.entries), "lots": fmt_levels(t.lots), "stop": t.stop, "tp1": t.tp1, "tp2": t.tp2,
            "risk": None if t.risk is None else round(float(t.risk), 2),
            "allowed_risk": None if t.allowed is None else round(float(t.allowed), 2),
            "currency": t.account.currency, "modifications": "; ".join(t.modifications),
            "orders": "; ".join(str(o) for o in t.orders), "notes": "; ".join(t.notes)}


def log_decision(base: Path, row: dict, ticket: Optional[Ticket] = None) -> None:
    log_dir = base / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "trade_decisions.csv"
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=DECISION_FIELDS)
        if new:
            wr.writeheader()
        wr.writerow(row)
    if ticket is not None:
        tdir = base / "tickets"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / f"{ticket.ticket_id}.json").write_text(json.dumps(
            {**row, "gate_version": GATE_VERSION, "confluences_logged": ticket.row}, indent=1, default=str), encoding="utf-8")


# =============================================================================
# Modifications
# =============================================================================

MOD_HELP = ("Change values as field=value, several at once, e.g.  stop=1.1180 tp1=1.1650 e1=1.1310 "
            "lots=0.10,0.15,0.25 route=M\n  fields: e1 e2 e3 (entries), stop, tp1, tp2, lots, route (L or M). "
            "'resize' recomputes lots from the risk budget. Enter alone keeps the ticket.")


def apply_mods(t: Ticket, text: str, tick: Decimal) -> list[str]:
    """Apply field=value edits; return errors. Prices are rounded to the instrument tick."""
    errs = []
    for tok in text.split():
        if tok.lower() == "resize":
            t.lots_manual = False
            t.modifications.append("lots resized from risk budget")
            continue
        if "=" not in tok:
            errs.append(f"'{tok}' is not field=value")
            continue
        k, v = (x.strip() for x in tok.split("=", 1))
        k = k.lower()
        try:
            if k == "route":
                v = v.upper()
                if v not in ("L", "M"):
                    raise ValueError("route is L or M")
                if v != t.route:
                    t.modifications.append(f"route {t.route} -> {v}")
                    t.route = v
                    t.lots_manual = False
            elif k == "lots":
                lots = [bv.D(x) for x in v.split(",")]
                if len(lots) != len(t.entries):
                    raise ValueError(f"give {len(t.entries)} lot sizes, one per entry")
                t.modifications.append(f"lots {fmt_levels(t.lots)} -> {fmt_levels(lots)}")
                t.lots, t.lots_manual = lots, True
            elif k in ("stop", "tp1", "tp2"):
                new = bv.round_price(bv.D(v), tick, "nearest")
                t.modifications.append(f"{k} {getattr(t, k)} -> {new}")
                setattr(t, k, new)
            elif k in ("e1", "e2", "e3"):
                i = int(k[1]) - 1
                if i >= len(t.entries):
                    raise ValueError(f"this route has {len(t.entries)} entr{'y' if len(t.entries) == 1 else 'ies'}")
                new = bv.round_price(bv.D(v), tick, "nearest")
                t.modifications.append(f"{k} {t.entries[i]} -> {new}")
                t.entries[i] = new
            else:
                raise ValueError(f"unknown field {k}")
        except (ValueError, ArithmeticError) as exc:
            errs.append(f"{tok}: {exc}")
    return errs


# =============================================================================
# The gate
# =============================================================================

class Gate:
    def __init__(self, accounts: list[Account], settings: dict, cfg: dict, base: Path = BASE,
                 prompter: Optional[Prompter] = None, session_factory: Callable[[Account], Mt5Session] = Mt5Session,
                 news_loader: Optional[Callable[[], pd.DataFrame]] = None):
        self.accounts, self.settings, self.cfg, self.base = accounts, settings, cfg, base
        self.p = prompter or Prompter()
        self.universe = {i.symbol: i for i in sc.build_universe(cfg)}
        self.session_factory = session_factory
        self.sessions: dict[str, Mt5Session] = {}
        self.news_loader = news_loader
        self._news: Optional[pd.DataFrame] = None
        self.news_status = ""

    def now(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz=sc.UTC)

    # ----------------------------------------------------------------- news [OWNER cfg-0.6.0]
    def news(self) -> pd.DataFrame:
        """Forex Factory red-folder events, fetched fresh once per gate run (decision time, not scan time)."""
        if self._news is None:
            try:
                if self.news_loader:
                    self._news = self.news_loader()
                else:
                    now = self.now()
                    events, self.news_status = sc.load_calendar(self.cfg, self.base, now, False)
                    snap = self.base / self.cfg["paths"]["data_dir"] / "calendar_snapshots"
                    self._news = newsmod.load_archive(snap, events or None, now)
            except Exception as exc:  # noqa: BLE001
                self.news_status = f"calendar unavailable: {exc}"
                self._news = newsmod.parse_events([], self.now())
        return self._news

    def news_check(self, inst: sc.Instrument, t: Ticket) -> Optional[str]:
        """Show red events for the trade's currencies; return a blackout reason inside the v1.0 window."""
        ccfg = self.cfg.get("calendar", {})
        impacts = tuple(ccfg.get("impacts_tracked", ["High"]))
        window = float(ccfg.get("news_window_minutes", 30))
        df, now, ccys = self.news(), self.now(), inst.calendar_currencies
        if df.empty:
            t.notes.append(f"news not checked ({self.news_status or 'no calendar data'}): check forexfactory.com/calendar")
            return None
        horizon = self.settings["ladder_expiry_hours"] if t.route == "L" else 4
        soon = newsmod.red(df, impacts)
        soon = soon[soon["currency"].isin(ccys) & (soon["event_time"] >= now - pd.Timedelta(minutes=window))
                    & (soon["event_time"] <= now + pd.Timedelta(hours=max(horizon, 48)))]
        for _, e in soon.iterrows():
            hrs = (e["event_time"] - now).total_seconds() / 3600
            self.p.say(f"    red news: {e['currency']} {e['title']} {'NOW' if abs(hrs) * 60 <= window else f'in {hrs:.1f}h'}"
                       f" (forecast {e['forecast'] or '-'}, previous {e['previous'] or '-'})")
        near = newsmod.events_near(df, ccys, now, window, impacts)
        if not near.empty:
            e = near.iloc[0]
            why = f"v1.0 news blackout: {e['currency']} {e['title']} within {window:.0f} min"
            t.notes.append(why)
            return why
        during = soon[(soon["event_time"] > now) & (soon["event_time"] <= now + pd.Timedelta(hours=horizon))]
        if t.route == "L" and not during.empty:
            e = during.iloc[0]
            t.notes.append(f"red event {e['currency']} {e['title']} falls inside the ladder's {horizon:.0f}h life: v1.0 "
                           "cancels pending legs in a blackout; the gate does not, so cancel them by hand if unfilled")
        return None

    def session(self, acct: Account) -> Optional[Mt5Session]:
        if acct.platform != "mt5":
            return None
        if acct.id not in self.sessions:
            for s in self.sessions.values():   # one terminal attached at a time
                s.close()
            s = self.session_factory(acct)
            if not s.open():
                self.p.say(f"  ! {acct.label}: MT5 not available ({s.error})")
            self.sessions = {acct.id: s}
        s = self.sessions[acct.id]
        return s if s.mode else None

    def close(self) -> None:
        for s in self.sessions.values():
            s.close()
        self.sessions = {}

    # ----------------------------------------------------------------- flow
    def run(self, scan_path: Path, include_developing: bool = False) -> list[Ticket]:
        scan = json.loads(scan_path.read_text(encoding="utf-8"))
        meta = scan["meta"]
        asof = pd.Timestamp(meta["asof_utc"], tz=sc.UTC)
        age_h = (self.now() - asof).total_seconds() / 3600
        say = self.p.say
        say(f"\nTrade gate {GATE_VERSION}. Scan {meta['asof_utc']} UTC ({meta['run_type']}, config "
            f"{meta['config_version']}), {age_h:.1f}h old. Nothing is sent without CONFIRM.")
        if age_h > self.settings["max_scan_age_hours"] and not self.p.yes(
                f"The scan is older than {self.settings['max_scan_age_hours']:.0f}h; prices may have moved. Continue anyway?"):
            return []
        cands = candidates(scan, include_developing)
        if not cands:
            say("No A or B setups in this scan. Nothing to decide.")
            return []
        done: list[Ticket] = []
        for row in cands:
            done += self.review(row, meta)
        self.close()
        return done

    def review(self, row: dict, meta: dict) -> list[Ticket]:
        say, inst = self.p.say, self.universe.get(row["symbol"])
        checks = " ".join(f"{k.upper()}{'+' if sc.flag(row.get(k)) else '-'}" for k in sc.CONF_KEYS)
        say(f"\n=== {row['symbol']} {row['direction'].upper()}  {sc.section_label(row)}"
            f"{' #' + str(row['rank']) if row.get('rank') else ''}  total {row.get('total')}  {checks}")
        say(f"    has: {sc.confluences_text(row) or 'none'}")
        say(f"    missing: {sc.confluences_text(row, met=False) or 'none'}")
        if row.get("c3_levels") or row.get("stacked"):
            say(f"    Fib hit: {row.get('c3_levels') or 'none'}{'  STACKED in the key level zone' if sc.flag(row.get('stacked')) else ''}")
        if row.get("grade_note"):
            say(f"    {row['grade_note']}")
        say(f"    zone {row.get('zone_level')} ({row.get('zone_low')} to {row.get('zone_high')})  "
            f"reversal: {row.get('candle') or 'none yet'}  price at scan {row.get('ref_price')}")
        cal = row.get("calendar") or {}
        if cal.get("event"):
            say(f"    next event: {cal.get('currency')} {cal['event']} in {cal.get('hours')}h"
                f"{'  << EVENT RISK' if cal.get('event_risk') else ''}")
        if row.get("flags"):
            say(f"    flags: {'; '.join(row['flags'])}")
        if row["section"] != "qualified":
            say("    NOTE: developing, not an A or B setup. The scanner says this is not enough to take a trade yet.")
        stamp = meta["stamp"]
        if inst is None:
            say("    not in the scanner universe; skipped")
            return []
        if not self.p.yes("Take this trade?"):
            t = Ticket(uuid.uuid4().hex[:10], stamp, row, Account("-", "-", "manual"))
            log_decision(self.base, decision_row(t, "skipped", self.now()))
            return []
        accts = [a for a in self.accounts if a.trades(row["symbol"])]
        if not accts:
            say("    no account in accounts.yaml trades this instrument")
            return []
        for i, a in enumerate(accts, 1):
            say(f"    {i}. {a.label} ({a.platform}{', algo allowed' if a.allow_algo else ''})")
        picks = self.p.ask("Which account(s)? numbers, comma separated (blank = none)")
        chosen = []
        for x in picks.replace(" ", "").split(","):
            if x.isdigit() and 1 <= int(x) <= len(accts) and accts[int(x) - 1] not in chosen:
                chosen.append(accts[int(x) - 1])
        if not chosen:
            say("    no account chosen; nothing done")
            t = Ticket(uuid.uuid4().hex[:10], stamp, row, Account("-", "-", "manual"))
            log_decision(self.base, decision_row(t, "skipped (no account)", self.now()))
            return []
        return [t for t in (self.ticket_for(row, stamp, inst, a) for a in chosen) if t]

    def ticket_for(self, row: dict, stamp: str, inst: sc.Instrument, acct: Account) -> Optional[Ticket]:
        say = self.p.say
        tick = tick_of(inst)
        t = Ticket(uuid.uuid4().hex[:10], stamp, row, acct)
        entries, stop, tp1, tp2, notes = propose_levels(row, inst)
        t.entries, t.stop, t.tp1, t.tp2, t.notes = entries, stop, tp1, tp2, notes
        t.weights = list(RULES.ladder_weights) if entries else []
        sess = self.session(acct)
        # risk budget and contract value
        spec = SizeSpec(None)
        if sess:
            try:
                spec = sess.size_spec(row["symbol"])
                K, used, warns = sess.capital()
                t.notes += warns
            except Exception as exc:  # noqa: BLE001
                say(f"  ! {acct.label}: {exc}")
                return self.finish(t, "failed", f"MT5 read failed: {exc}")
        else:
            if acct.platform == "mt5":
                t.notes.append("MT5 not connected: manual only for this ticket")
            vpp = acct.value_per_point.get(row["symbol"])
            spec = SizeSpec(None if vpp is None else bv.D(vpp), source="accounts.yaml")
            K = self.ask_decimal(f"{acct.label}: equity to size from ({acct.currency}), blank = enter lots yourself")
            used = self.ask_decimal(f"{acct.label}: risk already open on this account ({acct.currency})", "0") if K else None
        if K is not None and K <= 0:
            t.notes.append(f"{acct.label} balance/equity is {K}: no risk budget, so lots cannot be sized; "
                           "fund the account or enter lots yourself")
            t.capital, t.allowed = K, Decimal(0)
        elif K:
            t.capital = K
            t.allowed = bv.allowed_idea_risk(K, used or Decimal(0), RULES)
        cost = cost_per_lot(acct, spec, tick)
        if not t.entries:
            # No impulse: v1.0 defines no ladder, stop or targets, so start from a market entry and the
            # owner sets stop and targets. Lots still follow the 2 % / 5 % sizing once a stop exists.
            t.route, t.entries, t.weights = "M", [Decimal(0)], [Decimal(1)]
            t.notes = [n for n in t.notes if "no impulse" not in n]
            t.notes.append("no v1.0 levels without an impulse: route M at the live price; set stop, tp1, tp2")
            say(f"  {MOD_HELP}")
        # modification loop
        while True:
            self.reprice_route(t, sess, inst)
            if not t.lots_manual and t.stop is not None and t.allowed is not None and t.entries:
                t.lots, why = size_lots(t.entries, t.weights, t.stop, spec, cost, t.allowed)
                if why:
                    t.notes = [n for n in t.notes if not n.startswith("sizing:")] + [f"sizing: {why}"]
            t.risk = plan_risk(t.entries, t.lots, t.stop, spec, cost) if t.lots else None
            errs = validate_levels(t.direction, t.entries, t.stop, t.tp1, t.tp2, spec.min_stop)
            if not t.lots:
                errs.append("no lot sizes")
            say("")
            for line in t.lines():
                say(line)
            for e in errs:
                say(f"  ! {e}")
            text = self.p.ask(f"Modifications? ({'fix the errors above, ' if errs else ''}Enter keeps, 'help', 'cancel')")
            if text.lower() == "cancel":
                return self.finish(t, "cancelled", "")
            if text.lower() == "help":
                say(f"  {MOD_HELP}")
                continue
            if not text:
                if errs:
                    say("  the ticket has errors; change the values or type cancel")
                    continue
                break
            for e in apply_mods(t, text, tick):
                say(f"  ! {e}")
        # news at decision time [OWNER cfg-0.6.0]
        blackout = self.news_check(inst, t)
        if blackout:
            say(f"  ! {blackout}. A manual ticket is allowed but v1.0 would not enter now.")
        # who places it
        ok, why = algo_permission(acct, self.settings, sess.mode if sess else None, sess.algo_on if sess else None)
        if ok and t.over_risk():
            ok, why = False, "risk is above the 2% / 5% ceiling; algo orders must stay inside it"
        if ok and blackout:
            ok, why = False, blackout
        choice = self.p.ask(f"Who places it? m = manual (you place it), a = algo{'' if ok else ' (unavailable: ' + why + ')'}", "m")
        t.mode = "algo" if choice.lower().startswith("a") and ok else "manual"
        if choice.lower().startswith("a") and not ok:
            say(f"  algo not allowed: {why}. Ticket stays manual.")
        if t.over_risk():
            say(f"  ! risk {t.risk:,.2f} is above the allowed {t.allowed:,.2f}")
            if self.p.ask(f"Type {OVERRIDE_WORD} to keep this size anyway, anything else cancels") != OVERRIDE_WORD:
                return self.finish(t, "cancelled", "risk above ceiling")
            t.notes.append("risk ceiling OVERRIDE by owner")
        # final confirm
        say("\nFINAL TICKET")
        for line in t.lines():
            say(line)
        n_orders = sum(len(split_for_targets(q, spec.vol_step, spec.vol_min)) for q in t.lots) if t.mode == "algo" else 0
        act = (f"send {n_orders} order(s) to {acct.label} ({sess.mode})" if t.mode == "algo"
               else "record this as approved; you place it yourself")
        if self.p.ask(f"Type {CONFIRM_WORD} to {act}. Anything else cancels") != CONFIRM_WORD:
            return self.finish(t, "cancelled", "not confirmed")
        if t.mode == "manual":
            say("  Approved. Place it on your platform; it is logged and will show on the journal page.")
            return self.finish(t, "approved (manual)", "")
        return self.send(t, sess, spec, inst)

    def ask_decimal(self, q: str, default: str = "") -> Optional[Decimal]:
        while True:
            v = self.p.ask(q, default)
            if not v:
                return None
            try:
                return bv.D(v.replace(",", ""))
            except ArithmeticError:
                self.p.say("  ! enter a number")

    def reprice_route(self, t: Ticket, sess: Optional[Mt5Session], inst: sc.Instrument) -> None:
        """Route M: one entry at the live price (MT5) or at e1. Route L: three legs."""
        if t.route == "M":
            if len(t.entries) != 1:
                t.entries, t.weights = t.entries[:1] or [Decimal(0)], [Decimal(1)]
                t.lots_manual = False
            if sess:
                bid, ask, _ = sess.quote(t.symbol)
                if bid is not None:
                    t.entries = [ask if t.direction == sc.LONG else bid]
        elif len(t.entries) != 3:
            entries, *_ = propose_levels(t.row, inst)
            if entries:
                t.entries, t.weights, t.lots_manual = entries, list(RULES.ladder_weights), False
            else:   # no impulse: a ladder has no v1.0 levels; the owner sets e1 to e3
                t.entries, t.weights, t.lots_manual = (t.entries * 3)[:3], list(RULES.ladder_weights), False
                t.notes.append("ladder without an impulse: set e1, e2 and e3 yourself")

    def finish(self, t: Ticket, decision: str, note: str) -> Optional[Ticket]:
        if note:
            t.notes.append(note)
        log_decision(self.base, decision_row(t, decision, self.now()), t)
        self.p.say(f"  logged: {decision} ({t.ticket_id})")
        return t if decision.startswith(("approved", "sent")) else None

    # ----------------------------------------------------------------- orders
    def build_requests(self, t: Ticket, sess: Mt5Session, spec: SizeSpec) -> tuple[list[dict], list[str]]:
        m, sym = sess.mt5, t.account.venue_symbol(t.symbol)
        info = m.symbol_info(sym)
        bid, ask, age = sess.quote(t.symbol)
        problems = []
        if bid is None:
            return [], [f"no live quote for {sym}"]
        if age > self.settings["max_quote_age_seconds"]:
            problems.append(f"last {sym} quote is {age:.0f}s old (market closed or feed stalled)")
        long_ = t.direction == sc.LONG
        gap = bv.D(info.trade_stops_level) * bv.D(info.point)
        reqs = []
        expiry_ok = bool(info.expiration_mode & 4)   # SYMBOL_EXPIRATION_SPECIFIED
        exp_secs = int(m.symbol_info_tick(sym).time + self.settings["ladder_expiry_hours"] * 3600)
        fill = m.ORDER_FILLING_FOK if info.filling_mode & 1 else (m.ORDER_FILLING_IOC if info.filling_mode & 2 else m.ORDER_FILLING_RETURN)
        for i, (e, q) in enumerate(zip(t.entries, t.lots), 1):
            if t.route == "L":
                if long_ and e >= ask - gap:
                    problems.append(f"leg {i} buy limit {e} is not below the ask {ask}")
                if not long_ and e <= bid + gap:
                    problems.append(f"leg {i} sell limit {e} is not above the bid {bid}")
            for vol, which in split_for_targets(q, spec.vol_step, spec.vol_min):
                tp = t.tp1 if which == "tp1" or t.tp2 is None else t.tp2
                r = {"symbol": sym, "volume": float(vol), "price": float(e), "sl": float(t.stop), "tp": float(tp),
                     "magic": MAGIC, "comment": f"gate {t.ticket_id} L{i} {which}"}
                if t.route == "L":
                    r.update(action=m.TRADE_ACTION_PENDING, type=m.ORDER_TYPE_BUY_LIMIT if long_ else m.ORDER_TYPE_SELL_LIMIT,
                             type_filling=m.ORDER_FILLING_RETURN,
                             type_time=m.ORDER_TIME_SPECIFIED if expiry_ok else m.ORDER_TIME_GTC)
                    if expiry_ok:
                        r["expiration"] = exp_secs
                else:
                    r.update(action=m.TRADE_ACTION_DEAL, type=m.ORDER_TYPE_BUY if long_ else m.ORDER_TYPE_SELL,
                             type_filling=fill, type_time=m.ORDER_TIME_GTC, deviation=int(self.cfg.get("gate_deviation_points", 10)))
                reqs.append(r)
        if t.route == "L" and not expiry_ok:
            t.notes.append(f"broker does not take an expiry: cancel unfilled legs after {self.settings['ladder_expiry_hours']:.0f}h by hand")
        return reqs, problems

    def send(self, t: Ticket, sess: Mt5Session, spec: SizeSpec, inst: sc.Instrument) -> Optional[Ticket]:
        m, say = sess.mt5, self.p.say
        reqs, problems = self.build_requests(t, sess, spec)
        for r in reqs:   # every request must pass the broker's check before anything is sent
            chk = m.order_check(r)
            if chk is None or chk.retcode != 0:
                problems.append(f"order_check {r['comment']}: {getattr(chk, 'comment', m.last_error())}")
        if problems:
            for pr in problems:
                say(f"  ! {pr}")
            return self.finish(t, "failed (nothing sent)", "; ".join(problems))
        placed = []
        for r in reqs:
            res = m.order_send(r)
            if res is None or res.retcode not in (m.TRADE_RETCODE_DONE, m.TRADE_RETCODE_PLACED):
                why = getattr(res, "comment", None) or str(m.last_error())
                say(f"  ! {r['comment']} rejected: {why}")
                t.orders = placed
                if placed and t.route == "L" and self.p.yes(f"{len(placed)} order(s) already placed. Remove them?"):
                    for o in placed:
                        rr = m.order_send({"action": m.TRADE_ACTION_REMOVE, "order": o})
                        say(f"    remove {o}: {getattr(rr, 'comment', m.last_error())}")
                    t.notes.append("placed legs removed after a rejection")
                return self.finish(t, "failed (partly sent)" if placed else "failed (nothing sent)", why)
            placed.append(res.order)
            say(f"  sent {r['comment']}: order {res.order} {r['volume']} lots @ {r['price']}")
        t.orders = placed
        t.notes.append("trailing the stop after TP1 is not automated: manage it on the platform")
        return self.finish(t, "sent (algo)", "")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Trade gate: owner approval before any order")
    ap.add_argument("--scan", help="scan JSON to review (default: latest live scan in output/)")
    ap.add_argument("--accounts", default=str(BASE / "accounts.yaml"))
    ap.add_argument("--config", default=str(BASE / "scanner_config.yaml"))
    ap.add_argument("--include-developing", action="store_true", help="also list developing setups")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cfg = sc.load_config(Path(a.config))
    accounts, settings = load_accounts(Path(a.accounts))
    scan = Path(a.scan) if a.scan else latest_scan(BASE / cfg["paths"]["output_dir"])
    gate = Gate(accounts, settings, cfg)
    try:
        done = gate.run(scan, a.include_developing)
    finally:
        gate.close()
    print(f"\n{len(done)} ticket(s) approved or sent. Decisions: logs/trade_decisions.csv. "
          "Run 'python scanner.py --journal' to refresh the journal page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
