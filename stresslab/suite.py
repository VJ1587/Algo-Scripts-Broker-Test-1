"""StressSuite: runs every layer and aggregates a PASS / WARN / FAIL scorecard."""
from __future__ import annotations

import math
import warnings

import numpy as np
import pandas as pd

from stresslab import faults as F
from stresslab import generators as g
from stresslab import invariants as inv
from stresslab.adapters import validate_signals
from stresslab.execution_sim import ExecConditions, run_execution
from stresslab.montecarlo import parameter_sensitivity, trade_sequence_mc
from stresslab.regression import golden_check
from stresslab.result import FAIL, PASS, WARN, Result, worst
from stresslab.scenarios import SCENARIOS, SCENARIOS_BY_NAME

# numeric warnings are not the code noticing bad data
_IGNORED_WARNINGS = (RuntimeWarning, DeprecationWarning, PendingDeprecationWarning, FutureWarning)

RECONCILE_TOL = 0.01
RISK_BREACH_MULT = 1.25
WORST_R_LIMIT = -3.0
NULL_T_LIMIT = 2.0
RUIN_DD = 0.25
RUIN_PROB_LIMIT = 0.05
MC_HORIZON = 200
CLIFF_LIMIT = 0.30
NULL_PATHS = 8


def _call_under_fault(fn, bars):
    """Return (outcome, payload): 'raised' / 'warned' / 'ok'."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            out = fn(bars)
        except Exception as e:  # noqa: BLE001  any exception counts as detection
            return "raised", f"{type(e).__name__}: {str(e)[:120]}"
    real = [w for w in caught if not issubclass(w.category, _IGNORED_WARNINGS)]
    if real:
        return "warned", f"{real[0].category.__name__}: {str(real[0].message)[:120]}"
    return "ok", out


class StressSuite:
    def __init__(self, personality, scanner, executor_factory, journal_factory, scanner_factory=None,
                 scanner_params=None, seeds=(1, 2, 3), golden_dir="golden", update_golden=False,
                 report_generator=None, report_browser=False, sensitivity_runs=20, verbose=True):
        self.personality = personality
        self.scanner = scanner
        self.executor_factory = executor_factory
        self.journal_factory = journal_factory
        self.scanner_factory = scanner_factory
        self.scanner_params = dict(scanner_params or {})
        self.seeds = tuple(seeds)
        self.golden_dir = golden_dir
        self.update_golden = update_golden
        self.report_generator = report_generator
        self.report_browser = report_browser
        self.sensitivity_runs = sensitivity_runs
        self.verbose = verbose
        self.rows: list[dict] = []
        self.html_detail = pd.DataFrame(columns=["dataset", "check", "status", "detail"])

    # ------------------------------------------------------------ helpers
    def _add(self, layer: str, check: str, res: Result):
        self.rows.append({"layer": layer, "check": check, "status": res.status, "detail": res.reason})
        if self.verbose:
            print(f"  [{res.status:4}] {layer:15} {check:38} {res.reason}")

    def _guard(self, layer, check, fn):
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001  a crashing check is itself a FAIL
            res = Result(FAIL, f"check crashed: {type(e).__name__}: {str(e)[:150]}")
        self._add(layer, check, res)
        return res

    def _reference_bars(self, seed=1):
        return SCENARIOS_BY_NAME["regime_switching"].make_bars(seed)

    # ------------------------------------------------------------ 1 invariants
    def run_invariants(self):
        bars = self._reference_bars()
        sc, pm = self.scanner, self.personality
        self._guard("invariants", "scanner_no_lookahead", lambda: inv.check_scanner_no_lookahead(sc, bars))
        self._guard("invariants", "personality_no_lookahead", lambda: inv.check_personality_no_lookahead(pm, bars))
        self._guard("invariants", "scanner_schema", lambda: inv.check_scanner_schema(sc, bars))
        sample = [s.make_bars(1) for s in SCENARIOS[:5]]
        self._guard("invariants", "personality_bounds", lambda: inv.check_personality_bounds(pm, sample))
        self._guard("invariants", "scanner_determinism", lambda: inv.check_determinism(lambda: sc.scan(bars),
                                                                                         "scanner signals"))
        self._guard("invariants", "personality_determinism",
                    lambda: inv.check_determinism(lambda: pm.assess(bars), "personality scores"))

        def exec_once():
            t, _ = run_execution(sc.scan(bars), bars, self.executor_factory(), ExecConditions(reject_prob=0.1,
                                 partial_fill_prob=0.1), seed=7, journal=self.journal_factory())
            return t
        self._guard("invariants", "execution_determinism", lambda: inv.check_determinism(exec_once, "trades"))

    # ------------------------------------------------------------ 2 data faults
    def _fault_result(self, name, outcome, payload, validate_out):
        if name in F.MUST_DETECT:
            if outcome in ("raised", "warned"):
                return Result(PASS, f"detected ({outcome}): {payload}")
            return Result(FAIL, "corrupted data accepted silently; output produced without raise or warning")
        if outcome == "raised":
            return Result(FAIL, f"crashed on legitimate data: {payload}")
        problems = validate_out(payload)
        if problems:
            return Result(FAIL, "invalid output: " + "; ".join(problems))
        if name in F.UNDETECTABLE:
            return Result(WARN, "ran fine, but a server time offset cannot be detected from data; "
                                "confirm the broker's UTC offset")
        if outcome == "warned":
            return Result(PASS, f"survived with a warning: {payload}")
        return Result(PASS, "survived, output valid")

    def run_faults(self):
        clean = self._reference_bars(seed=2)
        pm = self.personality
        bounds = pm.output_bounds

        def val_scan(out):
            return validate_signals(out) if isinstance(out, pd.DataFrame) else ["scan did not return a DataFrame"]

        def val_pers(out):
            if not isinstance(out, dict):
                return ["assess did not return a dict"]
            bad = [f"{k}={v}" for k, v in out.items()
                   if k in bounds and (not np.isfinite(v) or not bounds[k][0] <= v <= bounds[k][1])]
            return [f"out of bounds or non finite: {bad}"] if bad else []

        for name in F.MUST_DETECT + F.MUST_SURVIVE:
            bad_bars = F.apply_fault(name, clean, seed=3)
            for comp, fn, val in (("scanner", self.scanner.scan, val_scan), ("personality", pm.assess, val_pers)):
                def check(fn=fn, val=val, name=name, bad_bars=bad_bars):
                    outcome, payload = _call_under_fault(fn, bad_bars)
                    return self._fault_result(name, outcome, payload, val)
                kind = "detect" if name in F.MUST_DETECT else "survive"
                self._guard("data_faults", f"{comp}:{kind}:{name}", check)

    # ------------------------------------------------------------ 3 discrimination
    def run_discrimination(self, n_samples=10, n_bars=600):
        exp = getattr(self.personality, "expectations", None)
        if not exp:
            self._add("discrimination", "expectations", Result(WARN, "model declares no expectations; "
                                                                     "ground truth not tested"))
            return
        for metric, (low, high) in exp.items():
            def check(metric=metric, low=low, high=high):
                by = {r: [g.regime_bars(r, n_bars, seed=1000 + 17 * k) for k in range(n_samples)]
                      for r in (low, high)}
                return inv.check_personality_discrimination(self.personality, by, metric, (low, high))
            self._guard("discrimination", f"{metric}:{low}<{high}", check)

    # ------------------------------------------------------------ 4 scenarios
    def _run_one(self, scenario, seed, scanner=None):
        bars = scenario.make_bars(seed)
        sig = (scanner or self.scanner).scan(bars)
        journal = self.journal_factory()
        trades, broker = run_execution(sig, bars, self.executor_factory(), scenario.cond, seed=seed,
                                       journal=journal)
        return bars, sig, trades, broker, journal

    def run_scenarios(self):
        rows = []
        pooled_baseline = []
        for sc in SCENARIOS:
            hard, soft, all_r = [], [], []
            n_trades = n_breach = 0
            worst_r = math.inf
            ev_tot: dict = {}
            for seed in self.seeds:
                try:
                    bars, sig, trades, broker, journal = self._run_one(sc, seed)
                except Exception as e:  # noqa: BLE001
                    hard.append(f"seed {seed} crashed: {type(e).__name__}: {e}")
                    continue
                ev = broker.event_counts()
                for k, v in ev.items():
                    ev_tot[k] = ev_tot.get(k, 0) + v
                if ev.get("naked_order"):
                    hard.append(f"seed {seed}: {ev['naked_order']} naked orders")
                summ = journal.summary()
                broker_pnl = broker.equity - broker.start_equity
                if summ.get("n_trades") != len(trades):
                    hard.append(f"seed {seed}: journal has {summ.get('n_trades')} trades, broker {len(trades)}")
                if not math.isclose(float(summ.get("net_pnl", math.nan)), broker_pnl, abs_tol=RECONCILE_TOL):
                    hard.append(f"seed {seed}: journal net_pnl {summ.get('net_pnl'):.2f} vs broker {broker_pnl:.2f}")
                if len(trades):
                    breach = trades["planned_risk"] > RISK_BREACH_MULT * trades["risk_budget"]
                    n_breach += int(breach.sum())
                    worst_r = min(worst_r, float(trades["r_multiple"].min()))
                    all_r += trades["r_multiple"].tolist()
                n_trades += len(trades)
            if n_breach:
                soft.append(f"{n_breach} fills over {RISK_BREACH_MULT}x risk budget")
            if worst_r < WORST_R_LIMIT:
                soft.append(f"worst trade {worst_r:.2f}R below {WORST_R_LIMIT}R")
            if hard:
                res = Result(FAIL, "; ".join(hard))
            elif soft:
                res = Result(FAIL if sc.baseline else WARN, ("baseline: " if sc.baseline else "stress: ") +
                             "; ".join(soft))
            else:
                res = Result(PASS, f"{n_trades} trades, reconciled, no naked orders")
            r = np.asarray(all_r, dtype=float)
            if sc.baseline:
                pooled_baseline += all_r
            rows.append({"scenario": sc.name, "baseline": sc.baseline, "status": res.status, "n_trades": n_trades,
                         "mean_r": float(r.mean()) if len(r) else math.nan,
                         "win_rate": float((r > 0).mean()) if len(r) else math.nan,
                         "worst_r": worst_r if np.isfinite(worst_r) else math.nan,
                         "risk_breaches": n_breach, "events": ";".join(f"{k}={v}" for k, v in sorted(ev_tot.items())),
                         "detail": res.reason})
            self._add("scenarios", sc.name, res)
        self.pooled_baseline_r = np.asarray(pooled_baseline, dtype=float)
        return pd.DataFrame(rows)

    # ------------------------------------------------------------ 5 null test
    def run_null_test(self):
        sc = SCENARIOS_BY_NAME["random_walk_null"]

        def check():
            rs = []
            for k in range(NULL_PATHS):
                bars = sc.make_bars(500 + k)
                trades, _ = run_execution(self.scanner.scan(bars), bars, self.executor_factory(),
                                          ExecConditions(), seed=500 + k, journal=self.journal_factory())
                if len(trades):
                    rs += trades["r_multiple"].dropna().tolist()
            r = np.asarray(rs, dtype=float)
            if len(r) < 2:
                return Result(WARN, f"only {len(r)} trades on {NULL_PATHS} random walks; null test inconclusive")
            sd = r.std(ddof=1)
            t = r.mean() / (sd / math.sqrt(len(r))) if sd > 0 else (math.inf if r.mean() > 0 else 0.0)
            reason = f"mean {r.mean():+.3f}R over {len(r)} trades on {NULL_PATHS} random walks, t={t:+.2f}"
            if t >= NULL_T_LIMIT:
                return Result(FAIL, reason + " (edge on pure noise: leakage or fill bias)")
            return Result(PASS, reason)
        self._guard("null_test", "random_walk_edge", check)

    # ------------------------------------------------------------ 6 monte carlo
    def run_monte_carlo(self):
        r = getattr(self, "pooled_baseline_r", np.array([]))
        risk = getattr(self.executor_factory(), "risk_frac", 0.01)
        if len(r) == 0:
            self._add("monte_carlo", "ruin_probability", Result(WARN, "no baseline trades to resample"))
            return None
        mc = trade_sequence_mc(r, n_sims=2000, n_trades=MC_HORIZON, risk_frac=risk, ruin_dd=RUIN_DD, seed=11)
        reason = (f"P(dd>={RUIN_DD:.0%} in {MC_HORIZON} trades)={mc['prob_ruin']:.1%}, maxDD p95={mc['max_dd_p95']:.1%}, "
                  f"P(loss)={mc['prob_loss']:.1%}, from {mc['n_input_trades']} baseline trades")
        self._add("monte_carlo", "ruin_probability",
                  Result(WARN if mc["prob_ruin"] >= RUIN_PROB_LIMIT else PASS, reason))
        return mc

    # ------------------------------------------------------------ 7 sensitivity
    def run_sensitivity(self):
        if self.scanner_factory is None or not self.scanner_params:
            return None
        scs = [SCENARIOS_BY_NAME[n] for n in ("steady_trend", "regime_switching")]

        def metric(params):
            scanner = self.scanner_factory(**params)
            rs = []
            for sc in scs:
                _, _, trades, _, _ = self._run_one(sc, self.seeds[0], scanner=scanner)
                rs += trades["r_multiple"].tolist()
            return float(np.sum(rs)) if rs else 0.0

        out = {}

        def check():
            res = parameter_sensitivity(metric, self.scanner_params, jitter=0.2, n=self.sensitivity_runs, seed=5)
            out.update(res)
            reason = (f"base {res['base']:+.1f}R, perturbed p5/p50/p95 {res['p5']:+.1f}/{res['p50']:+.1f}/"
                      f"{res['p95']:+.1f}R, cliff_ratio {res['cliff_ratio']:.0%}")
            return Result(WARN if res["cliff_ratio"] > CLIFF_LIMIT else PASS, reason)
        self._guard("sensitivity", "parameter_jitter_20pct", check)
        return out or None

    # ------------------------------------------------------------ 8 regression
    def run_regression(self):
        bars = self._reference_bars(seed=4)
        gd, up = self.golden_dir, self.update_golden
        self._guard("regression", "golden:scanner_signals",
                    lambda: golden_check("scanner_signals", self.scanner.scan(bars), gd, update=up))
        self._guard("regression", "golden:personality_scores",
                    lambda: golden_check("personality_scores", self.personality.assess(bars), gd, update=up))

    # ------------------------------------------------------------ 9 html
    def run_html(self):
        if self.report_generator is None:
            return
        try:
            from stresslab.html_stress import HtmlReportStress
        except ImportError:
            self._add("html_reports", "html_layer", Result(WARN, "html_stress layer not installed yet"))
            return
        detail = HtmlReportStress(self.report_generator, browser=self.report_browser).run()
        self.html_detail = detail
        for check, grp in detail.groupby("check", sort=False):
            st = worst(grp["status"])
            bad = grp[grp["status"] == st]
            reason = (f"{len(grp)} datasets ok" if st == PASS else
                      f"{len(bad)}/{len(grp)} datasets {st}: " + "; ".join(
                          f"{d}: {x}" for d, x in zip(bad["dataset"].head(3), bad["detail"].head(3), strict=True)))
            self._add("html_reports", check, Result(st, reason[:300]))

    # ------------------------------------------------------------ all
    def run_all(self):
        self.rows = []
        if self.verbose:
            print("StressLab: running layers")
        self.run_invariants()
        self.run_faults()
        self.run_discrimination()
        scenario_rows = self.run_scenarios()
        self.run_null_test()
        mc = self.run_monte_carlo()
        sens = self.run_sensitivity()
        self.run_regression()
        self.run_html()
        scorecard = pd.DataFrame(self.rows, columns=["layer", "check", "status", "detail"])
        return scorecard, scenario_rows, mc, sens
