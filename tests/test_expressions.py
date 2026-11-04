"""Inline comparison expressions in semantic_query (1.2 M2.1), on this machine's engine, no LLM: a change, a % change or
a share comes from ABI's rows, computed for that one query and never saved. On Brewline (bench/brewline.py at the
bench's scale) the latest complete month's revenue, the month before, the change and the % change by region equal a
hand-written DuckDB query over the files to the cent; whatever could make up a figure or read around the model is a 400;
a Viewer who may see North only gets North's rows, IGNORE drops the query's filters but never the row-level rule, and a
hidden column stays hidden; measures.json and the project's Git are as they were after each of these questions; a named
model and a custom agent's scope hold what an expression reads as they hold the query's own measures and columns."""
import hashlib
import json
import secrets
import subprocess
import sys

import duckdb
import pytest
from conftest import drop

import abi

sys.path.insert(0, str(abi.ROOT / "bench"))
import bench  # noqa: E402
import brewline  # noqa: E402

P = "pi_expr_test"
CHANGE = [{"name": "prev", "expr": "PREVIOUS_PERIOD([revenue])"}, {"name": "change", "expr": "[revenue] - PREVIOUS_PERIOD([revenue])"},
          {"name": "change_pct", "expr": "DIVIDE([revenue] - PREVIOUS_PERIOD([revenue]), ABS(PREVIOUS_PERIOD([revenue])))"}]
REFUSED = {  # an expression: a word of its 400
    "SUM(orders.revenue)": "SUM(...) isn't a measure",  # a raw aggregate
    "orders.revenue": "orders.revenue isn't a measure",  # a column
    "[revenue] * 1.15": "The number 1.15 isn't allowed",  # a figure the model picked
    "[revenue] * (1 + 1)": "The number 1 isn't allowed",  # or built from others (M2 review: no number at all)
    "100": "The number 100 isn't allowed",
    "[revenue] + 'x'": "Text, TRUE, FALSE and NULL aren't allowed",
    # M2 review: free arithmetic built any figure from constants: 2 (so "three cities" grounded), 1/3 ("33%"), 100
    "DIVIDE([revenue], [revenue]) + DIVIDE([revenue], [revenue])": "+ isn't allowed here",
    "DIVIDE([revenue], [revenue] + [revenue] + [revenue])": "+ isn't allowed here",
    "[revenue] * 0 + 100": "The number 0 isn't allowed",
    "-[revenue]": "- isn't allowed here",
    "WHERE([revenue], orders.revenue > 1234.56)": "WHERE isn't allowed",  # a filter in disguise, its literal echoed back
    "ROLLING([revenue], 5000, day)": "at most 1000",
    "[revenue_by_magic] - [revenue]": "[revenue_by_magic] is not a measure",
    "ROUND([revenue], 2)": "ROUND isn't allowed",  # a function outside the set
    "[revenue] > 0": "> isn't allowed",
    "SQRT([revenue])": "Unknown function SQRT",
    "-(-(-(-(-(-(-(-[revenue])))))))": "more than 8 levels",
    "[revenue]) FROM orders --": "Syntax error",  # nothing reaches SQL unparsed
}


def projects() -> set:
    return {x["project"] for x in abi.call("list_projects")["projects"]}


def sure(action: str, **args) -> dict:
    """An action on what this test made (its account, rules): ABI's confirmation is answered here."""
    try:
        return abi.call(action, **args)
    except abi.ActionError as e:
        if e.status != 409 or not (e.body or {}).get("confirm_token"):
            raise
        return abi.call(action, confirm=e.body["confirm_token"], **args)


def state() -> tuple:
    """Rule 7: the project's last commit, its uncommitted files and the bytes of measures.json, in ABI's own repo."""
    repo = abi.ABI_PROJECTS / P
    git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
    return git("rev-parse", "HEAD"), git("status", "--porcelain"), hashlib.sha256((repo / "model/measures.json").read_bytes()).hexdigest()


def hand(out) -> tuple:
    """(year, month) of the latest complete month and {region: (revenue, the month before's, change, % change)} from a
    fresh DuckDB over the files, never ABI."""
    o, c = (f"read_parquet('{brewline.q(out)}/{t}')" for t in ("orders.parquet", "cafes/cafes.parquet"))
    db = duckdb.connect()
    y, m, lo, hi = db.execute(f"""SELECT year(m), month(m), m - INTERVAL 1 MONTH, m + INTERVAL 1 MONTH
                                  FROM (SELECT date_trunc('month', max(order_date) + 1) - INTERVAL 1 MONTH AS m FROM {o})""").fetchone()
    rows = db.execute(f"""SELECT region, sum(revenue) FILTER (order_date >= ?::DATE + INTERVAL 1 MONTH),
                                 sum(revenue) FILTER (order_date < ?::DATE + INTERVAL 1 MONTH)
                          FROM {o} JOIN {c} USING (cafe_id) WHERE order_date >= ? AND order_date < ? GROUP BY region""",
                      [lo, lo, lo, hi]).fetchall()
    return (y, m), {r: (b, a, b - a, float(b - a) / float(a)) for r, b, a in rows}


@pytest.fixture(scope="module")
def brew(tmp_path_factory):
    """Brewline as the bench loads it, and Bob, a Viewer who may see the North region's cafés only and not their format."""
    out = tmp_path_factory.mktemp("brew")
    if P in projects():  # an interrupted run's
        drop([P])
    d = bench.data(out)
    assert abi.load("demo", P, d["files"])["readiness"]["ready"]
    bob, pw = f"expr_bob_{secrets.token_hex(3)}", secrets.token_urlsafe(16)
    uid = abi.call("create_user", name=bob, password=pw)["id"]
    try:
        abi.call("set_role", project=P, user=bob, role="viewer")
        sure("set_rls_rule", project=P, id="north", table="cafes", column="region", users={str(uid): ["North"]})
        sure("set_column_rule", project=P, id="no_format", table="cafes", columns=["format"])
        yield {"out": out, "known": d["known"], "bob": abi.login(bob, pw)[1]["token"], "before": state()}
    finally:
        if P in projects():
            drop([P])
        sure("delete_user", name=bob)


def test_the_latest_months_change_by_region_is_the_files_to_the_cent(brew):
    (y, m), want = hand(brew["out"])
    assert f"{y}-{m:02d}" == brew["known"]["latest_complete_month"]
    r = abi.call("semantic_query", project=P, measures=["revenue", "revenue_change_pct"], dimensions=["calendar.year", "calendar.month",
                 "cafes.region"], filters=[{"column": "calendar.year", "op": "=", "value": y}, {"column": "calendar.month", "op": "=", "value": m}],
                 expressions=CHANGE, order_by=[{"field": "change", "desc": False}])
    assert r["columns"] == ["calendar.year", "calendar.month", "cafes.region", "revenue", "revenue_change_pct", "prev", "change", "change_pct"]
    assert r["expressions"]["change"] == "[revenue] - PREVIOUS_PERIOD([revenue])"  # the figure says how it was made
    assert r["measures"]["change"] == "change"  # M2 review: not its MEL, which the grounding never reads (its literals)
    assert [row[:2] for row in r["rows"]] == [[y, m]] * len(want)
    assert all(row[4] == row[-1] for row in r["rows"])  # the governed % change (M2.3) and the inline one agree
    got = {row[2]: [row[3], *row[5:]] for row in r["rows"]}
    assert {k: [round(float(x), 2) for x in v[:3]] for k, v in got.items()} == {k: [round(float(x), 2) for x in v[:3]] for k, v in want.items()}
    assert {k: round(v[3], 9) for k, v in got.items()} == {k: round(v[3], 9) for k, v in want.items()}
    assert r["rows"][0][2] == brew["known"]["why"]["member"]  # ranked by the change: the planted drop first
    assert state() == brew["before"]


def test_what_could_make_up_a_figure_or_read_around_the_model_is_refused(brew):
    base = {"project": P, "measures": ["revenue"], "dimensions": ["calendar.year", "calendar.month"]}
    cases = [([{"name": "x", "expr": e}], says) for e, says in REFUSED.items()] + [
        ([{"name": "revenue", "expr": "[revenue] + 0"}], "'revenue' already names a measure"),
        ([{"name": f"x{i}", "expr": "[revenue]"} for i in range(11)], "at most 10 expressions"),
        # M2 review: each distinct period or IGNORE is one more scan and join: 10 of 32 ROLLING windows planned for 452 s
        ([{"name": f"r{i}", "expr": f"ROLLING([revenue], {i}, day)"} for i in range(1, 6)], "at most 4")]
    for exprs, says in cases:
        with pytest.raises(abi.ActionError) as e:
            abi.call("semantic_query", **base, expressions=exprs)
        assert e.value.status == 400 and says in e.value.body["message"], (exprs, e.value.body)
    assert state() == brew["before"]


def test_a_restricted_viewer_gets_only_their_rows_and_ignore_never_widens_them(brew):
    """IGNORE drops the query's region filter and grouping, so the owner's share reads every region; Bob's reads the
    North rows the rule lets him see, also when he filters on South. IGNORE on his hidden column is a 403 (M2 review:
    WHERE is no expression for anyone: a 400)."""
    (y, m), want = hand(brew["out"])
    month = [{"column": "calendar.year", "op": "=", "value": y}, {"column": "calendar.month", "op": "=", "value": m}]
    q = {"project": P, "measures": ["revenue"], "dimensions": ["cafes.region"], "filters": month, "expressions": [
        {"name": "every_region", "expr": "IGNORE([revenue], cafes.region)"}, {"name": "share", "expr": "DIVIDE([revenue], IGNORE([revenue], cafes.region))"}]}
    north, total = round(float(want["North"][0]), 2), round(sum(float(v[0]) for v in want.values()), 2)
    rows = abi.call("semantic_query", **q)["rows"]
    assert {r[0]: round(r[2], 2) for r in rows} == dict.fromkeys(want, total)
    assert round(sum(r[3] for r in rows), 9) == 1
    south = {**q, "dimensions": [], "filters": [*month, {"column": "cafes.region", "op": "=", "value": "South"}]}
    hidden = [{"name": "x", "expr": "IGNORE([revenue], cafes.format)"}]
    with abi.as_user(brew["bob"]):
        assert [[r[0], round(r[1], 2), round(r[2], 2), r[3]] for r in abi.call("semantic_query", **q)["rows"]] == [["North", north, north, 1]]
        assert [[r[0], round(r[1], 2), r[2]] for r in abi.call("semantic_query", **south)["rows"]] == [[None, north, None]]
        for x in hidden:
            with pytest.raises(abi.ActionError) as e:
                abi.call("semantic_query", **q | {"expressions": [x]})
            assert e.value.status == 403 and "cafes.format" in e.value.body["message"], e.value.body
    assert all(abi.call("semantic_query", **q | {"expressions": [x]})["rows"] for x in hidden)  # the owner may
    assert state() == brew["before"]


def test_a_named_model_holds_what_its_expressions_read(brew):
    """A question in a named model stays inside it: an expression's measures and columns count like the query's own."""
    abi.call("save_model", project=P, model={"id": "counts", "name": "Counts", "tables": ["orders", "calendar"], "measures": ["orders_rows"]})
    base = {"project": P, "model": "counts", "measures": ["orders_rows"], "dimensions": ["calendar.year", "calendar.month"]}
    assert abi.call("semantic_query", **base, expressions=[{"name": "change", "expr": "[orders_rows] - PREVIOUS_PERIOD([orders_rows])"}])["rows"]
    for expr, out in (("[revenue] - PREVIOUS_PERIOD([revenue])", "revenue"), ("IGNORE([orders_rows], cafes.region)", "cafes.region")):
        with pytest.raises(abi.ActionError) as e:
            abi.call("semantic_query", **base, expressions=[{"name": "x", "expr": expr}])
        assert e.value.status == 400 and f"{out} is not in model 'counts'" in e.value.body["message"], e.value.body


def test_a_custom_agents_scope_holds_what_its_expressions_read():
    """D-24: a custom agent reads only its scope's measures and tables. Its semantic_query is checked before it runs
    (runs._refused, in the engine's own Python: no model needed), and an expression's measures and columns count like
    the query's own: the check validated the call as a stored query, which has no expressions, so they went past it."""
    code = ("import json, sys\nfrom abi.agents import runs\nfrom abi.semantic import lineage\n"
            "q = {'measures': ['orders_rows'], 'dimensions': ['calendar.year', 'calendar.month']}\n"
            "print(json.dumps([runs._refused('semantic_query', q | {'expressions': [{'name': 'x', 'expr': e}]}, ['semantic_query'], {},"
            " lineage.graph({}), {'orders_rows'}, {'orders', 'calendar'}, []) for e in sys.argv[1:]]))")
    r = subprocess.run(["sh", "-c", 'eval "$(scripts/engine.sh env)"; cd engine/backend; exec .venv/bin/python -c "$0" "$@"', code,
                        "[orders_rows] - PREVIOUS_PERIOD([orders_rows])", "[revenue] - PREVIOUS_PERIOD([revenue])",
                        "IGNORE([orders_rows], cafes.region)"], cwd=abi.ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == [None, "Outside your scope: revenue.", "Outside your scope: cafes."]
