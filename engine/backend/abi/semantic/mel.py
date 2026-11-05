"""Measure Expression Language (MEL): parser and compiler. PRD SE-3, glossary "Measure", §6.6 model/measures.json.

`compile({id: expression}, columns)` parses every measure, checks it, inlines `[id]` references and
returns {id: node}. References use the stable measure ID, never the display name (PC-1). The compiled tree is what the planner (S-02) walks:
- `Agg` leaves are the only things that touch rows; each belongs to one fact (`Agg.col.table`), so the planner
  collects the leaves under their filter context and emits one aggregate query per fact (and context).
- `Where`, `Ignore` and `Time` wrap a subtree and change the filter context of every `Agg` below them.
- `Op` is scalar arithmetic, comparison, logic and the scalar functions DIVIDE, IF, ROUND, ABS over aggregates.
Nodes are frozen dataclasses, so equal aggregates hash equal and the planner can compute each once.
Semantics of each construct: work/validation/S-03.md (golden suite).
"""

import functools
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from graphlib import CycleError, TopologicalSorter

from lark import Lark, Transformer, UnexpectedEOF, UnexpectedInput, v_args

AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "DISTINCT_COUNT", "MEDIAN"}
SCALARS = {"DIVIDE": 2, "IF": 3, "ROUND": 2, "ABS": 1}  # name: argument count
CONVERTS = ("SUM", "AVG", "MIN", "MAX", "MEDIAN")  # SE-13: the aggregates CONVERT may feed (amounts, not counts)
TIME = {"YTD", "QTD", "MTD", "PREVIOUS_YEAR", "PREVIOUS_PERIOD", "ROLLING"}
UNITS = {"day", "month", "quarter", "year"}  # the calendar grains (M-03)
USAGE = {  # shown in arity errors
    **{f: f"{f}(table.column)" for f in AGGREGATES}, "DIVIDE": "DIVIDE(numerator, denominator)",
    "CONVERT": "SUM(CONVERT(table.amount, table.currency, table.date))", "IF": "IF(condition, then, else)", "ROUND": "ROUND(value, digits)", "ABS": "ABS(value)",
    "WHERE": "WHERE([Measure], table.column = 'value')", "IGNORE": "IGNORE([Measure], table.column, ...)",
    **{f: f"{f}([Measure])" for f in TIME}, "ROLLING": "ROLLING([Measure], 3, month)",
}
CONDITION = {"=", "<>", "<", "<=", ">", ">=", "AND", "OR", "NOT", "IN", "IS NULL", "IS NOT NULL"}


@dataclass(frozen=True)
class Lit:
    value: str | Decimal | bool | None


@dataclass(frozen=True)
class Col:
    table: str
    column: str


@dataclass(frozen=True)
class Ref:  # [Measure]; only in parse() output, compile() inlines it
    name: str


@dataclass(frozen=True)
class Agg:
    fn: str  # one of AGGREGATES
    col: Col


@dataclass(frozen=True)
class Conv:
    """SE-13: CONVERT(amount, currency, date), the amount in the display currency at the latest rate on or before the
    row's local day; only an aggregate's argument. The planner adds it to the fact's rows as column `column`."""
    amount: Col
    currency: Col | Lit  # a column of ISO codes or one 'ISO' literal
    date: Col

    @property
    def table(self) -> str:
        return self.amount.table

    @property
    def column(self) -> str:
        return "__cv_" + hashlib.sha256(repr(self).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Op:
    op: str  # + - * / NEG, CONDITION, or a SCALARS name
    args: tuple


@dataclass(frozen=True)
class Where:
    expr: object
    cond: object  # a row condition: Op over Col and Lit only


@dataclass(frozen=True)
class Ignore:
    expr: object
    cols: tuple  # Col, ...


@dataclass(frozen=True)
class Time:
    fn: str  # one of TIME
    expr: object
    n: int | None = None  # ROLLING only
    unit: str | None = None  # ROLLING only


@dataclass(frozen=True)
class _Word:  # a bare name; only valid as ROLLING's unit
    name: str


GRAMMAR = r"""
?start: disj
?disj: conj | disj _OR conj -> or_
?conj: neg | conj _AND neg -> and_
?neg: comp | NOT neg -> not_
?comp: sum
     | sum COMP sum -> compare
     | sum _IN "(" literal ("," literal)* ")" -> in_
     | sum _IS [NOT] _NULL -> is_null
?sum: product | sum ADD product -> arith
?product: unary | product MUL unary -> arith
?unary: atom | "-" unary -> negate
?atom: literal
     | "[" MEASURE "]" -> ref
     | name "." name -> column
     | name "(" [disj ("," disj)*] ")" -> call
     | name -> word
     | "(" disj ")"
?literal: NUMBER -> number | STRING -> string | _TRUE -> true | _FALSE -> false | _NULL -> null
name: IDENT | QUOTED

_OR: "OR"i
_AND: "AND"i
NOT: "NOT"i
_IN: "IN"i
_IS: "IS"i
_NULL: "NULL"i
_TRUE: "TRUE"i
_FALSE: "FALSE"i
COMP: "=" | "<>" | "!=" | "<=" | ">=" | "<" | ">"
ADD: "+" | "-"
MUL: "*" | "/"
NUMBER: /\d+(\.\d+)?/
STRING: /'([^']|'')*'/
MEASURE: /[^\]]+/
IDENT: /[A-Za-z_][A-Za-z0-9_]*/
QUOTED: /"([^"]|"")+"/
%ignore /\s+/
"""


class MelError(ValueError):
    """Compile errors: [{measure, error, fix}], one per problem found."""

    def __init__(self, errors: list[dict]):
        super().__init__("; ".join(f"Measure '{e['measure']}': {e['error']}" for e in errors))
        self.errors = errors


class _Bad(Exception):
    def __init__(self, error: str, fix: str):
        super().__init__(error)
        self.fix = fix


@v_args(inline=True)
class _Build(Transformer):
    number = lambda self, t: Lit(Decimal(t))
    string = lambda self, t: Lit(t[1:-1].replace("''", "'"))
    true = lambda self: Lit(True)
    false = lambda self: Lit(False)
    null = lambda self: Lit(None)
    ref = lambda self, t: Ref(t.strip())
    column = lambda self, t, c: Col(t, c)
    word = lambda self, w: _Word(w)
    or_ = lambda self, a, b: Op("OR", (a, b))
    and_ = lambda self, a, b: Op("AND", (a, b))
    not_ = lambda self, _, a: Op("NOT", (a,))
    compare = lambda self, a, op, b: Op("<>" if op == "!=" else str(op), (a, b))
    arith = lambda self, a, op, b: Op(str(op), (a, b))
    negate = lambda self, a: Op("NEG", (a,))
    in_ = lambda self, a, *vals: Op("IN", (a, *vals))
    is_null = lambda self, a, neg: Op("IS NOT NULL" if neg else "IS NULL", (a,))

    def name(self, t):
        return t[1:-1].replace('""', '"') if t.type == "QUOTED" else str(t)

    def call(self, fn, *args):
        args = () if args == (None,) else args
        f = fn.upper()
        if f not in AGGREGATES | SCALARS.keys() | TIME | {"WHERE", "IGNORE", "CONVERT"}:
            raise _Bad(f"Unknown function {fn}.", "Use one of: " + ", ".join(sorted(USAGE)) + ".")
        want = 1 if f in AGGREGATES or f in TIME - {"ROLLING"} else 3 if f in ("ROLLING", "CONVERT") else SCALARS.get(f, 2)
        if len(args) != want and not (f == "IGNORE" and len(args) > 2):
            raise _Bad(f"{f} takes {want} argument{'s' * (want > 1)}, got {len(args)}.", f"Write it as {USAGE[f]}.")
        if f == "CONVERT":
            return _convert(*args)
        if f in AGGREGATES:
            if isinstance(args[0], Conv) and f not in CONVERTS:
                raise _Bad(f"{f} can't read converted amounts; CONVERT goes inside {', '.join(CONVERTS)}.",
                           f"Write it as {f}({args[0].amount.table}.{args[0].amount.column}).")
            if not isinstance(args[0], (Col, Conv)):
                raise _Bad(f"{f} aggregates a column, not an expression.", f"Write it as {USAGE[f]}.")
            return Agg(f, args[0])
        if f == "ROUND" and not _integer(args[1]):
            raise _Bad("ROUND's digits must be a whole number.", "Write it as ROUND([Measure], 1).")
        if f in SCALARS:
            return Op(f, args)
        if f == "WHERE":
            return Where(*args)
        if f == "IGNORE":
            if not all(isinstance(c, Col) for c in args[1:]):
                raise _Bad("IGNORE names the columns whose filters the measure ignores.", f"Write it as {USAGE[f]}.")
            return Ignore(args[0], args[1:])
        if f == "ROLLING":
            n, unit = args[1:]
            if not (_integer(n) and n.value > 0) or not (isinstance(unit, _Word) and unit.name.lower() in UNITS):
                raise _Bad("ROLLING needs a positive whole number and a unit (" + ", ".join(sorted(UNITS)) + ").",
                           f"Write it as {USAGE[f]}.")
            return Time(f, args[0], int(n.value), unit.name.lower())
        return Time(f, args[0])


def _convert(amount, currency, date) -> Conv:
    iso = isinstance(currency, Lit) and isinstance(currency.value, str) and len(currency.value) == 3 and currency.value.isupper()
    if not (isinstance(amount, Col) and isinstance(date, Col) and (isinstance(currency, Col) or iso)):
        raise _Bad("CONVERT takes an amount column, a currency column or 'ISO' code, and a date column.",
                   f"Write it as {USAGE['CONVERT']}.")
    if {c.table for c in (amount, currency, date) if isinstance(c, Col)} != {amount.table}:
        raise _Bad("CONVERT's amount, currency and date must be columns of the same table.", f"Write it as {USAGE['CONVERT']}.")
    return Conv(amount, currency, date)


def _integer(x) -> bool:
    return isinstance(x, Lit) and isinstance(x.value, Decimal) and x.value == x.value.to_integral_value()


_parser = Lark(GRAMMAR, parser="lalr", transformer=_Build())
NICE = {"RPAR": "')'", "LPAR": "'('", "COMMA": "','", "RSQB": "']'", "DOT": "'.'", "$END": "the end", "LSQB": "[Measure]",
        "IDENT": "a name", "NUMBER": "a number", "STRING": "'text'", "MINUS": "'-'", "ADD": "+ or -", "MUL": "* or /",
        "COMP": "a comparison", "NOT": "NOT"}  # lark terminal names → words; others (QUOTED, MEASURE) aren't shown


def _syntax(text: str, e: UnexpectedInput) -> _Bad:
    pos = e.pos_in_stream if isinstance(getattr(e, "pos_in_stream", None), int) else len(text)
    end = pos >= len(text) or isinstance(e, UnexpectedEOF) or getattr(getattr(e, "token", None), "type", "") == "$END"
    pos = len(text) if end else pos
    expected = sorted({NICE[x] for x in (getattr(e, "expected", None) or getattr(e, "allowed", None) or ()) if x in NICE})
    return _Bad(("Syntax error: the expression ends early." if end else f"Syntax error at column {pos + 1}: unexpected {text[pos]!r}.")
                + "\n" + text + "\n" + " " * min(pos, len(text)) + "^",
                ("Expected " + ", ".join(expected) + ". " if expected else "") + "Check brackets, commas and quotes.")


def parse(text: str):
    """One MEL expression → node tree with `Ref`s left in place. Raises _Bad (the inline transformer raises it as is)."""
    try:
        return _parser.parse(text)
    except UnexpectedInput as e:
        raise _syntax(text, e) from None


def _walk(n):
    yield n
    kids = {Op: lambda: n.args, Agg: lambda: (n.col,), Conv: lambda: (n.amount, n.currency, n.date), Where: lambda: (n.expr, n.cond), Ignore: lambda: (n.expr, *n.cols),
            Time: lambda: (n.expr,)}.get(type(n), lambda: ())()
    for k in kids:
        yield from _walk(k)


def _check(n, row: bool = False) -> None:
    """Measure level vs row level: columns only inside an aggregate, a WHERE condition or IGNORE."""
    t = type(n)
    if t is _Word:
        raise _Bad(f"'{n.name}' is not a column, measure or function.",
                   "Write columns as table.column and measures as [measure-id]; text values need single quotes.")
    if t is Conv:
        raise _Bad("CONVERT converts the amounts an aggregate reads.", f"Write it as {USAGE['CONVERT']}.")
    if t is Col and not row:
        raise _Bad(f"Column {n.table}.{n.column} is outside an aggregate.", f"Aggregate it, e.g. SUM({n.table}.{n.column}).")
    if row and t in (Agg, Ref, Where, Ignore, Time) or (row and t is Op and n.op in SCALARS):
        raise _Bad("A WHERE condition compares columns with values; it can't use measures or functions.",
                   "Write it as table.column = 'value' (also <>, <, >, IN (...), IS NULL, AND, OR, NOT).")
    if t is Where:
        if not (type(n.cond) is Op and n.cond.op in CONDITION):
            raise _Bad("WHERE's second argument must be a condition.", f"Write it as {USAGE['WHERE']}.")
        _check(n.expr)
        _check(n.cond, row=True)
    elif t in (Op, Time, Ignore):
        for k in (n.args if t is Op else (n.expr,)):
            _check(k, row)


def _inline(n, done: dict):
    """Replace every Ref by the compiled tree of the measure it names."""
    t = type(n)
    if t is Ref:
        return done[n.name]
    if t is Op:
        return Op(n.op, tuple(_inline(a, done) for a in n.args))
    if t in (Where, Ignore, Time):
        return type(n)(**{**n.__dict__, "expr": _inline(n.expr, done)})
    return n


def compile(measures: dict[str, str], columns: dict[str, set[str] | None] | None = None) -> dict:
    """{name: MEL} → {name: compiled tree}. `columns` ({table: column names, or None when unknown}) checks
    column references against the model. Raises MelError listing every problem. M3.6: the last 256 compiled are
    kept (a page compiled the same measures 24 times); trees are frozen, so they are shared."""
    return dict(_compiled(json.dumps([measures, columns and {t: c if c is None else sorted(c) for t, c in columns.items()}])))


@functools.lru_cache(256)
def _compiled(key: str) -> dict:
    measures, columns = json.loads(key)
    return _compile(measures, columns and {t: c if c is None else set(c) for t, c in columns.items()})


def _compile(measures: dict[str, str], columns: dict[str, set[str] | None] | None = None) -> dict:
    errors, trees = [], {}

    def bad(name, error, fix):
        errors.append({"measure": name, "error": error, "fix": fix})

    for name, text in measures.items():
        try:
            trees[name] = tree = parse(text)
            _check(tree)
        except _Bad as e:
            bad(name, str(e), e.fix)
            continue
        for n in _walk(tree):
            if type(n) is Ref and n.name not in measures:
                others = ", ".join(sorted(f"[{m}]" for m in measures if m != name))
                bad(name, f"Unknown measure [{n.name}].", f"Use one of: {others}." if others else "Add that measure first.")
            if type(n) is Col and columns is not None:
                if n.table not in columns:
                    bad(name, f"Unknown table '{n.table}'.", "Use a model table: " + ", ".join(sorted(columns)) + ".")
                elif columns[n.table] is not None and n.column not in columns[n.table]:
                    bad(name, f"Table '{n.table}' has no column '{n.column}'.",
                        "Use one of: " + ", ".join(sorted(columns[n.table])) + ".")
    if errors:
        raise MelError(errors)
    graph = {m: {n.name for n in _walk(t) if type(n) is Ref} for m, t in trees.items()}
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as e:
        cycle = e.args[1]
        raise MelError([{"measure": cycle[0], "error": "Measures refer to each other in a cycle: "
                         + " → ".join(f"[{m}]" for m in reversed(cycle)) + ".",
                         "fix": "Remove the [Measure] reference that closes the cycle."}]) from None
    done: dict = {}
    for m in order:
        done[m] = _inline(trees[m], done)
    return {m: done[m] for m in measures}


INLINE = ("+", "-", "*", "/", "NEG", "DIVIDE", "ABS")  # M2.1: the operators of a semantic query's inline expressions
DEPTH = 8
ROLLS = 1000  # M2 review: ROLLING's periods at most in an inline expression
SHAPES = ("a measure [id], under time functions (" + ", ".join(sorted(TIME)) + ") and IGNORE or not; two of those"
          " subtracted; or DIVIDE of one or a difference by one, ABS(one) or a difference: e.g. [m] - PREVIOUS_PERIOD([m]),"
          " DIVIDE([m] - PREVIOUS_PERIOD([m]), ABS(PREVIOUS_PERIOD([m]))), DIVIDE([m], IGNORE([m], table.column)), DIVIDE([a], [b])")


def inline(text: str, measures: set[str]):
    """M2.1: a semantic query's inline expression (evaluated for that query, never saved) as its parse tree. M2 review:
    one of a few shapes (SHAPES) over `measures` ([id]) under time functions and IGNORE, with no number, no WHERE (a
    filter in disguise: the query's filters do that) and no other arithmetic, so no figure is built from constants
    (free arithmetic made DIVIDE([m], [m]) + DIVIDE([m], [m]) a 2, and so any number): every figure it gives is a
    measure's or a comparison of two (AGENTS.md rule 3). Raises _Bad; compile() checks the rest (ROLLING's unit,
    columns) as for any measure."""
    allowed = f"An expression is {SHAPES}."

    def scan(n, depth: int) -> None:  # what it names and holds, top down, before its shape
        t = type(n)
        if depth > DEPTH:
            raise _Bad(f"It nests more than {DEPTH} levels deep.", "Split it into simpler expressions.")
        if t is Ref and n.name not in measures:
            raise _Bad(f"[{n.name}] is not a measure of this project.",
                       "Use one of: " + ", ".join(sorted(f"[{m}]" for m in measures)) + ".")
        if t is Lit:
            raise _Bad(f"The number {n.value} isn't allowed: an expression combines measures only." if isinstance(
                n.value, Decimal) else "Text, TRUE, FALSE and NULL aren't allowed: filter the query instead.", allowed)
        if t in (Agg, Conv, Col, _Word):
            what = f"{n.fn}(...)" if t is Agg else f"{n.table}.{n.column}" if t is Col else "CONVERT(...)" if t is Conv else f"'{n.name}'"
            raise _Bad(f"{what} isn't a measure: an expression combines existing measures, and a column goes only in"
                       " IGNORE.", allowed)
        if t is Where:
            raise _Bad("WHERE isn't allowed in an expression: filter the query (filters) instead.", allowed)
        if t is Op and n.op not in INLINE:
            raise _Bad(f"{n.op} isn't allowed in an expression.", allowed)
        if t is Time and n.fn == "ROLLING" and n.n > ROLLS:
            raise _Bad(f"ROLLING over {n.n} periods is too long: at most {ROLLS}.", allowed)
        for k in n.args if t is Op else (n.expr,) if t in (Ignore, Time) else ():
            scan(k, depth + 1)

    def term(n) -> None:  # a measure under time functions and IGNORE
        if type(n) in (Time, Ignore):
            return term(n.expr)
        if type(n) is not Ref:
            raise _Bad(f"{'-' if n.op == 'NEG' else n.op} isn't allowed here: an expression computes nothing else.", allowed)

    def diff(n) -> None:
        for k in n.args if type(n) is Op and n.op == "-" else (n,):
            term(k)

    scan(tree := parse(text), 1)
    if type(tree) is Op and tree.op == "DIVIDE":
        top, bottom = tree.args
        diff(top)
        diff(bottom.args[0] if type(bottom) is Op and bottom.op == "ABS" else bottom)
    else:
        diff(tree)
    return tree
