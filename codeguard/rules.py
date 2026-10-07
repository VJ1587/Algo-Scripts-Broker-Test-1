"""AST rules for trading bugs that generic linters miss.

Every finding carries: rule, severity, path, line, col, message, fix hint,
code snippet and source ("codeguard" or "ruff").

Suppress one line with ``# guard: ignore[TG101] reason``. A suppression only
counts if the reason has at least 5 characters. It applies to ruff codes too.

Bump ``RULESET_VERSION`` whenever a rule changes: it is part of every cache
key, so stale cached results are discarded.
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import asdict, dataclass

RULESET_VERSION = "2026.10.2"

BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"
SEVERITY_RANK = {INFO: 0, WARN: 1, BLOCK: 2}


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    fix: str


RULES: dict[str, Rule] = {r.id: r for r in (
    Rule("TG000", BLOCK, "File does not parse", "Fix the syntax error; nothing else in this file was checked."),
    Rule("TG101", BLOCK, "Negative shift reads future bars",
         "Use shift(n) with n >= 1 for features. Forward labels belong in a research only file, suppressed with a reason."),
    Rule("TG102", BLOCK, "Centred rolling window uses future bars",
         "Drop center=True; a trailing window only uses data up to the current bar."),
    Rule("TG103", BLOCK, "Backfill copies future values into the past",
         "Use ffill(), or drop the leading NaNs, instead of bfill/backfill."),
    Rule("TG104", WARN, "Full sample normalisation leaks future statistics",
         "Normalise with a trailing window: (x - x.rolling(n).mean()) / x.rolling(n).std(), or expanding()."),
    Rule("TG105", WARN, "Full sample percentile rank leaks future data",
         "Rank within a trailing window: x.rolling(n).rank(pct=True), or expanding().rank(pct=True)."),
    Rule("TG201", WARN, "Unseeded or global random number generator",
         "Pass a seed: rng = np.random.default_rng(seed); use rng.<fn>(...) everywhere."),
    Rule("TG202", WARN, "Timezone naive clock call",
         "Use datetime.now(timezone.utc) or pd.Timestamp.now(tz='UTC'); utcnow() returns a naive value."),
    Rule("TG203", WARN, "Exact equality on a float",
         "Compare with a tolerance: math.isclose(a, b, rel_tol=..., abs_tol=...)."),
    Rule("TG301", BLOCK, "Exception swallowed silently",
         "Catch the specific exception and log or re-raise it; a silent pass hides broker and data failures."),
    Rule("TG302", WARN, "Mutable default argument",
         "Default to None and create the list or dict inside the function."),
    Rule("TG303", WARN, "Chained assignment may write to a copy",
         "Use a single indexer: df.loc[rows, 'col'] = value."),
    Rule("TG304", BLOCK, "MetaTrader5 call result ignored",
         "Check the return value; on failure log mt5.last_error() and stop."),
    Rule("TG305", BLOCK, "Order request has no stop loss",
         "Add an 'sl' price to every order request dict."),
    Rule("TG306", BLOCK, "Hardcoded credential",
         "Read secrets from environment variables or an untracked config file, never from source."),
    Rule("TG307", WARN, "Side effect at import time",
         "Move it into a function called under if __name__ == '__main__':"),
    Rule("TG308", WARN, "assert used for runtime checks",
         "Raise a specific exception; assert is stripped under python -O."),
    Rule("TG401", WARN, "pd.concat inside a loop is quadratic",
         "Collect pieces in a list and call pd.concat once after the loop."),
    Rule("TG402", BLOCK, "DataFrame.append was removed in pandas 2.0",
         "Collect rows in a list and build the frame once, or use pd.concat."),
    Rule("TG403", INFO, "Row wise iteration is slow",
         "Vectorise with column operations, or use itertuples() if a loop is unavoidable."),
    Rule("TG404", WARN, "eval, exec or pickle load executes arbitrary code",
         "Parse data with json, ast.literal_eval or a schema; never unpickle untrusted files."),
    Rule("TG405", WARN, "Unescaped value inserted into HTML",
         "Wrap text in html.escape(...) (or format numbers with a spec) before inserting into markup."),
)}

WINDOWED = {"rolling", "expanding", "ewm", "groupby", "resample"}
MT5_CALLS = {"initialize", "login", "order_send", "symbol_select", "order_check"}
NP_RANDOM_OK = {"default_rng", "Generator", "SeedSequence", "PCG64", "PCG64DXSM", "Philox", "MT19937", "SFC64",
                "BitGenerator", "RandomState"}
RANDOM_OK = {"Random", "SystemRandom"}
SUBPROCESS_CALLS = {"run", "call", "Popen", "check_call", "check_output"}
WRITE_METHODS = {"to_csv", "to_parquet", "to_excel"}
SECRET_RE = re.compile(r"(pass(word|wd)|api_?key|secret|token|private_?key)", re.I)
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")  # looks like the NAME of an env var, not a secret
HTML_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z]")
SUPPRESS_RE = re.compile(r"#\s*guard:\s*ignore\[([A-Za-z0-9_, ]+)\]\s*(.*)$")
ORDER_KEYS = {"action", "symbol", "volume", "type"}


@dataclass
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    col: int
    message: str
    fix: str
    snippet: str
    source: str = "codeguard"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(**d)


def normalise_snippet(s: str) -> str:
    return " ".join(s.split())


def fingerprint(f: Finding) -> str:
    """Line independent identity: rule, path and whitespace normalised code."""
    blob = f"{f.rule}|{f.path}|{normalise_snippet(f.snippet)}".encode()
    return hashlib.sha1(blob, usedforsecurity=False).hexdigest()


def parse_suppressions(lines: list[str]) -> dict[int, set[str]]:
    """Map 1-based line number to the rule ids validly suppressed on it."""
    out: dict[int, set[str]] = {}
    for i, line in enumerate(lines, start=1):
        if "guard" not in line:
            continue
        m = SUPPRESS_RE.search(line)
        if m and len(m.group(2).strip()) >= 5:
            out[i] = {r.strip().upper() for r in m.group(1).split(",") if r.strip()}
    return out


def is_test_file(path: str) -> bool:
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
            or "/tests/" in f"/{p}" or "/test/" in f"/{p}")


# --------------------------------------------------------------------- helpers
def _chain(node):
    """Method/attribute names along a receiver chain, outermost first, and the base node."""
    names = []
    while True:
        if isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Subscript):
            node = node.value
        else:
            return names, node


def _is_neg(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value < 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        o = node.operand
        return not (isinstance(o, ast.Constant) and o.value == 0)
    return False


def _kw(call: ast.Call, name: str):
    for k in call.keywords:
        if k.arg == name:
            return k.value
    return None


def _const(node, value) -> bool:
    return isinstance(node, ast.Constant) and node.value == value and type(node.value) is type(value)


def _attr_call(node, attr: str | set) -> bool:
    attrs = {attr} if isinstance(attr, str) else attr
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in attrs


def _base_name(node) -> str | None:
    return node.id if isinstance(node, ast.Name) else None


def _is_main_guard(node) -> bool:
    if not isinstance(node, ast.If):
        return False
    t = node.test
    if isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq):
        sides = [t.left, t.comparators[0]]
        names = [s.id for s in sides if isinstance(s, ast.Name)]
        consts = [s.value for s in sides if isinstance(s, ast.Constant)]
        return names == ["__name__"] and consts == ["__main__"]
    return False


class _Imports:
    def __init__(self, nodes):
        self.np = {"np", "numpy"}
        self.npr = set()          # names bound to numpy.random
        self.rng_funcs = set()    # default_rng imported directly
        self.pd = {"pd", "pandas"}
        self.concat = set()
        self.mt5 = {"mt5"}
        self.mt5_funcs = set()
        self.random = set()
        self.random_funcs = set()
        self.pickle = {"pickle"}
        self.pickle_funcs = set()
        self.requests = {"requests"}
        self.yf = {"yf", "yfinance"}
        self.subprocess = {"subprocess"}
        self.sub_funcs = set()
        self.os = {"os"}
        self.dt_mod = set()       # import datetime
        self.dt_cls = set()       # from datetime import datetime / date
        for n in nodes:
            if isinstance(n, ast.Import):
                for a in n.names:
                    name = a.asname or a.name.split(".")[0]
                    m = a.name
                    if m == "numpy":
                        self.np.add(name)
                    elif m == "numpy.random" and a.asname:
                        self.npr.add(a.asname)
                    elif m == "pandas":
                        self.pd.add(name)
                    elif m == "MetaTrader5":
                        self.mt5.add(name)
                    elif m == "random":
                        self.random.add(name)
                    elif m == "pickle":
                        self.pickle.add(name)
                    elif m == "requests":
                        self.requests.add(name)
                    elif m == "yfinance":
                        self.yf.add(name)
                    elif m == "subprocess":
                        self.subprocess.add(name)
                    elif m == "os":
                        self.os.add(name)
                    elif m == "datetime":
                        self.dt_mod.add(name)
            elif isinstance(n, ast.ImportFrom) and n.module:
                for a in n.names:
                    name = a.asname or a.name
                    if n.module == "numpy" and a.name == "random":
                        self.npr.add(name)
                    elif n.module == "numpy.random":
                        if a.name == "default_rng":
                            self.rng_funcs.add(name)
                    elif n.module == "pandas" and a.name == "concat":
                        self.concat.add(name)
                    elif n.module == "MetaTrader5" and a.name in MT5_CALLS:
                        self.mt5_funcs.add(name)
                    elif n.module == "random" and a.name not in RANDOM_OK:
                        self.random_funcs.add(name)
                    elif n.module == "pickle" and a.name in ("load", "loads"):
                        self.pickle_funcs.add(name)
                    elif n.module == "subprocess" and a.name in SUBPROCESS_CALLS:
                        self.sub_funcs.add(name)
                    elif n.module == "datetime" and a.name in ("datetime", "date"):
                        self.dt_cls.add(name)


def _dict_names(nodes) -> set[str]:
    """Names bound to a dict (literal, dict() call, or annotated dict); nested dict writes are not pandas chains."""
    out = set()

    def is_dict_ann(a):
        return a is not None and "dict" in ast.unparse(a).lower()
    for n in nodes:
        if isinstance(n, ast.Assign):
            v = n.value
            if isinstance(v, (ast.Dict, ast.DictComp)) or (
                    isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                    and v.func.id in ("dict", "defaultdict", "OrderedDict")):
                out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and is_dict_ann(n.annotation):
            out.add(n.target.id)
        elif isinstance(n, ast.arg) and is_dict_ann(n.annotation):
            out.add(n.arg)
    return out


def _windowed_aliases(nodes) -> set[str]:
    """Names assigned from a windowed chain, e.g. ``r = s.rolling(50)``."""
    out = set()
    for n in nodes:
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            names, _ = _chain(n.value)
            if WINDOWED & set(names):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                out |= {t.id for t in targets if isinstance(t, ast.Name)}
    return out


# --------------------------------------------------------------------- analyzer
class _Analyzer(ast.NodeVisitor):
    def __init__(self, path: str, lines: list[str], tree):
        self.path, self.lines = path, lines
        nodes = list(ast.walk(tree))  # one walk shared by the three pre-passes
        self.imp = _Imports(nodes)
        self.windowed = _windowed_aliases(nodes)
        self.dicts = _dict_names(nodes)
        self.test_file = is_test_file(path)
        self.loop_depth = 0
        self.findings: list[Finding] = []

    def add(self, rule_id, node, message):
        r = RULES[rule_id]
        line = getattr(node, "lineno", 1)
        snippet = self.lines[line - 1].rstrip() if 0 < line <= len(self.lines) else ""
        self.findings.append(Finding(rule_id, r.severity, self.path, line, getattr(node, "col_offset", 0) + 1,
                                     message, r.fix, snippet))

    def _is_windowed(self, node) -> bool:
        names, base = _chain(node)
        return bool(WINDOWED & set(names)) or (_base_name(base) in self.windowed)

    # ---- loops
    def _loop(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_For = visit_AsyncFor = visit_While = _loop

    # ---- functions
    def _func(self, node):
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]
        for d in defaults:
            mutable = isinstance(d, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)) or (
                isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                and d.func.id in ("list", "dict", "set", "defaultdict"))
            if mutable:
                self.add("TG302", d, f"mutable default argument in {getattr(node, 'name', 'lambda')}()")
        saved = self.loop_depth
        self.loop_depth = 0
        self.generic_visit(node)
        self.loop_depth = saved

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    # ---- statements
    def visit_ExceptHandler(self, node):
        t = node.type
        names = []
        if t is None:
            broad = True
        else:
            elts = t.elts if isinstance(t, ast.Tuple) else [t]
            names = [e.id for e in elts if isinstance(e, ast.Name)]
            broad = any(n in ("Exception", "BaseException") for n in names)

        def trivial(s):
            return isinstance(s, (ast.Pass, ast.Continue)) or (
                isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                and (isinstance(s.value.value, str) or s.value.value is Ellipsis))
        if broad and all(trivial(s) for s in node.body):
            what = "bare except" if t is None else f"except {'/'.join(names)}"
            self.add("TG301", node, f"{what} with an empty body swallows every error")
        self.generic_visit(node)

    def visit_Assert(self, node):
        if not self.test_file:
            self.add("TG308", node, "assert outside a test file")
        self.generic_visit(node)

    def visit_Expr(self, node):
        c = node.value
        if isinstance(c, ast.Call):
            f = c.func
            hit = None
            if isinstance(f, ast.Attribute) and f.attr in MT5_CALLS and _base_name(f.value) in self.imp.mt5:
                hit = f"{f.value.id}.{f.attr}"
            elif isinstance(f, ast.Name) and f.id in self.imp.mt5_funcs:
                hit = f.id
            if hit:
                self.add("TG304", node, f"{hit}() result discarded; failures go unnoticed")
        self.generic_visit(node)

    def _check_secret_target(self, target, value, node):
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr
        elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and isinstance(
                target.slice.value, str):
            name = target.slice.value
        if name and SECRET_RE.search(name) and self._secret_literal(value):
            self.add("TG306", node, f"'{name}' is assigned a string literal")

    @staticmethod
    def _secret_literal(value) -> bool:
        return (isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) >= 4
                and not ENV_NAME_RE.match(value.value))

    def visit_Assign(self, node):
        for t in node.targets:
            self._check_secret_target(t, node.value, node)
            if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Subscript):
                inner = t.value
                col_access = isinstance(inner.slice, ast.Constant) and isinstance(inner.slice.value, str)
                loc_access = isinstance(inner.value, ast.Attribute) and inner.value.attr in ("loc", "iloc", "at", "iat")
                if (col_access or loc_access) and _base_name(inner.value) not in self.dicts:
                    self.add("TG303", node, "chained indexing assignment df[a][b] = v")
        v = node.value
        if len(node.targets) == 1 and _attr_call(v, "append"):
            if ast.unparse(node.targets[0]) == ast.unparse(v.func.value):  # unparse ignores Load/Store context
                self.add("TG402", node, f"{ast.unparse(node.targets[0])} = {ast.unparse(v.func.value)}.append(...)")
        if self.loop_depth and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and isinstance(v, ast.Call):
            f = v.func
            is_concat = (isinstance(f, ast.Attribute) and f.attr == "concat" and _base_name(f.value) in self.imp.pd) or (
                isinstance(f, ast.Name) and f.id in self.imp.concat)
            if is_concat and v.args and isinstance(v.args[0], (ast.List, ast.Tuple)):
                tname = node.targets[0].id
                if any(isinstance(e, ast.Name) and e.id == tname for e in v.args[0].elts):
                    self.add("TG401", node, f"{tname} = pd.concat([{tname}, ...]) inside a loop")
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._check_secret_target(node.target, node.value, node)
        self.generic_visit(node)

    def visit_Dict(self, node):
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if ORDER_KEYS <= keys and "sl" not in keys:
            self.add("TG305", node, "order request dict without an 'sl' key")
        self.generic_visit(node)

    def visit_Compare(self, node):
        if any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and type(side.value) is float and side.value != 0.0:
                    self.add("TG203", node, f"== / != against float literal {side.value!r}")
                    break
        self.generic_visit(node)

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Div) and isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Sub):
            mean, std = node.left.right, node.right
            if _attr_call(mean, "mean") and _attr_call(std, "std"):
                if not (self._is_windowed(mean) or self._is_windowed(std)):
                    self.add("TG104", node, "(x - x.mean()) / x.std() normalises with full sample statistics")
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        consts = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if HTML_TAG_RE.search(consts):
            raw = [ast.unparse(v.value) for v in node.values
                   if isinstance(v, ast.FormattedValue) and v.format_spec is None
                   and isinstance(v.value, (ast.Subscript, ast.Attribute))
                   and not any(isinstance(x, ast.Call) for x in ast.walk(v.value))]
            if raw:
                self.add("TG405", node, "unescaped value(s) in HTML f-string: " + ", ".join(raw[:4]))
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        attr = f.attr if isinstance(f, ast.Attribute) else None
        recv = f.value if isinstance(f, ast.Attribute) else None
        # TG101 negative shift
        if attr == "shift":
            arg = node.args[0] if node.args else _kw(node, "periods")
            if arg is not None and _is_neg(arg):
                self.add("TG101", node, f"shift({ast.unparse(arg)}) pulls future values back")
        # TG102 centred window
        if attr == "rolling" and _const(_kw(node, "center"), True):
            self.add("TG102", node, "rolling(..., center=True) averages future bars into the current one")
        # TG103 backfill
        if attr in ("bfill", "backfill"):
            self.add("TG103", node, f".{attr}() fills gaps with later values")
        if attr == "fillna":
            m = _kw(node, "method")
            if isinstance(m, ast.Constant) and m.value in ("bfill", "backfill"):
                self.add("TG103", node, f"fillna(method={m.value!r}) fills gaps with later values")
        # TG105 full sample rank
        if attr == "rank" and _const(_kw(node, "pct"), True) and not self._is_windowed(node):
            self.add("TG105", node, "rank(pct=True) over the whole series uses future data")
        # TG201 randomness
        self._check_random(node, f, attr, recv)
        # TG202 naive clocks
        self._check_clock(node, attr, recv)
        # TG403 row iteration
        if attr == "iterrows":
            self.add("TG403", node, ".iterrows() is slow and drops dtypes")
        if attr == "apply":
            ax = _kw(node, "axis")
            if _const(ax, 1) or _const(ax, "columns"):
                self.add("TG403", node, ".apply(axis=1) runs Python per row")
        # TG404 code execution
        if isinstance(f, ast.Name) and f.id in ("eval", "exec"):
            self.add("TG404", node, f"{f.id}() executes arbitrary code")
        if (attr in ("load", "loads") and _base_name(recv) in self.imp.pickle) or (
                isinstance(f, ast.Name) and f.id in self.imp.pickle_funcs):
            self.add("TG404", node, "pickle load executes code embedded in the file")
        # TG305 dict(action=..., ...) form
        if isinstance(f, ast.Name) and f.id == "dict":
            keys = {k.arg for k in node.keywords if k.arg}
            if ORDER_KEYS <= keys and "sl" not in keys:
                self.add("TG305", node, "order request dict(...) without sl=")
        # TG306 keyword credentials
        for k in node.keywords:
            if k.arg and SECRET_RE.search(k.arg) and self._secret_literal(k.value):
                self.add("TG306", k.value, f"keyword '{k.arg}' is passed a string literal")
        self.generic_visit(node)

    def _check_random(self, node, f, attr, recv):
        imp = self.imp
        if attr and isinstance(recv, ast.Attribute) and recv.attr == "random" and _base_name(recv.value) in imp.np:
            if attr not in NP_RANDOM_OK:
                self.add("TG201", node, f"global np.random.{attr}() is unseeded shared state")
                return
        if attr and _base_name(recv) in imp.npr and attr not in NP_RANDOM_OK:
            self.add("TG201", node, f"global numpy.random.{attr}() is unseeded shared state")
            return
        is_rng = (attr == "default_rng") or (isinstance(f, ast.Name) and f.id in imp.rng_funcs)
        if is_rng:
            seed = node.args[0] if node.args else _kw(node, "seed")
            if seed is None or _const(seed, None):
                self.add("TG201", node, "default_rng() without a seed is not reproducible")
            return
        if attr and _base_name(recv) in imp.random and attr not in RANDOM_OK:
            self.add("TG201", node, f"random.{attr}() uses the global unseeded generator")
            return
        if isinstance(f, ast.Name) and f.id in imp.random_funcs:
            self.add("TG201", node, f"{f.id}() from random uses the global unseeded generator")

    def _check_clock(self, node, attr, recv):
        if attr == "utcnow":
            self.add("TG202", node, "utcnow() returns a timezone naive value")
            return
        if attr not in ("now", "today"):
            return
        has_tz = bool(node.args) or _kw(node, "tz") is not None or _kw(node, "tzinfo") is not None
        is_dt = (_base_name(recv) in self.imp.dt_cls) or (
            isinstance(recv, ast.Attribute) and recv.attr in ("datetime", "date") and _base_name(recv.value) in self.imp.dt_mod)
        is_ts = isinstance(recv, ast.Attribute) and recv.attr == "Timestamp" or _base_name(recv) == "Timestamp"
        if attr == "today" and (is_dt or is_ts):
            self.add("TG202", node, f"{ast.unparse(node.func)}() is local, timezone naive time")
        elif attr == "now" and (is_dt or is_ts) and not has_tz:
            self.add("TG202", node, f"{ast.unparse(node.func)}() without tz is local, timezone naive time")


# --------------------------------------------------------------------- module level side effects
def _walk_no_defs(node):
    """ast.walk that does not descend into functions, classes or lambdas (deferred code)."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(c for c in ast.iter_child_nodes(n)
                     if not isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)))


def _side_effect_calls(stmt, imp: _Imports):
    for n in _walk_no_defs(stmt):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            base = _base_name(f.value)
            a = f.attr
            if (base in imp.mt5 and a in ("initialize", "login", "order_send")) or \
               (base in imp.requests and a in ("get", "post")) or \
               (base in imp.yf and a == "download") or \
               (base in imp.subprocess and a in SUBPROCESS_CALLS) or \
               (base in imp.os and a == "system") or a in WRITE_METHODS:
                yield n, f"{ast.unparse(f)}()"
        elif isinstance(f, ast.Name) and (f.id in imp.sub_funcs or f.id in imp.mt5_funcs):
            yield n, f"{f.id}()"


def _module_statements(body):
    """Module level statements, descending into if/try/with/for but not the main guard or defs."""
    for s in body:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or _is_main_guard(s):
            continue
        if isinstance(s, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            for field in ("body", "orelse", "finalbody"):
                yield from _module_statements(getattr(s, field, []) or [])
            for h in getattr(s, "handlers", []) or []:
                yield from _module_statements(h.body)
            if isinstance(s, ast.If):
                yield s.test
            continue
        yield s


def _check_import_side_effects(tree, an: _Analyzer):
    seen = set()
    for stmt in _module_statements(tree.body):
        if not isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.expr)):
            continue
        for call, label in _side_effect_calls(stmt, an.imp):
            if id(call) not in seen:
                seen.add(id(call))
                an.add("TG307", call, f"{label} runs at import time")


# --------------------------------------------------------------------- entry points
def analyze_source(text: str, path: str) -> list[Finding]:
    """Run every AST rule on one file's text. Suppressions are applied."""
    lines = text.splitlines()
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as e:
        r = RULES["TG000"]
        ln = e.lineno or 1
        snippet = lines[ln - 1].rstrip() if 0 < ln <= len(lines) else ""
        return [Finding("TG000", r.severity, path, ln, (e.offset or 1), f"syntax error: {e.msg}", r.fix, snippet)]
    an = _Analyzer(path, lines, tree)
    an.visit(tree)
    _check_import_side_effects(tree, an)
    sup = parse_suppressions(lines)
    out = [f for f in an.findings if f.rule not in sup.get(f.line, ())]
    out.sort(key=lambda f: (f.line, f.col, f.rule))
    return out


def apply_suppressions(findings: list[Finding], lines: list[str]) -> list[Finding]:
    sup = parse_suppressions(lines)
    return [f for f in findings if f.rule not in sup.get(f.line, ())]
