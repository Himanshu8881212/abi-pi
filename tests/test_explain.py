"""explain_change (1.2 M2.4), "why did it change" in one call, on this machine's engine, no LLM. On Brewline at the bench's
scale (bench/brewline.py) the latest complete month against the one before: the total and each dimension's top member
with its change equal a hand-written DuckDB query over the files to the cent, the planted member leads the first
dimension, and the call answers uncached within 3 s; a dimension's members add up to the total change; named periods
and dimensions; a Viewer who may see North only gets North's members and totals over North's rows, and a column hidden
from him is no dimension; an answer written from the rows passes the harness's grounding; what can't be explained is a
400; measures.json and the project's Git are as they were after every call (rule 7). The harness offers the tool
directly and in Viewer mode. M2 review: a member without rows in one period, the retail plant's default dimensions, a
ratio's (no shares) and when a period is complete."""
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time

import duckdb
import ground
import pytest
from conftest import drop
from test_expressions import projects, sure

import abi

sys.path.insert(0, str(abi.ROOT / "bench"))
import bench  # noqa: E402
import brewline  # noqa: E402

P, R = "pi_explain_test", "pi_explain_retail"
WHY = f"In project {P}: why did revenue change in the latest complete month of data compared with the month before?"
NORTH = {c for c, r in brewline.CITIES if r == "North"}


def state() -> tuple:
    """Rule 7: the project's last commit, its uncommitted files and the bytes of measures.json, in ABI's own repo."""
    repo = abi.ABI_PROJECTS / P
    git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
    return git("rev-parse", "HEAD"), git("status", "--porcelain"), hashlib.sha256((repo / "model/measures.json").read_bytes()).hexdigest()


def hand(out, dim: str, a: str, b: str, where: str = "true") -> dict:
    """{member: (revenue in month a, in month b)} of `dim` (a column of the orders joined to their café, customer and
    product), from a fresh DuckDB over the files, never ABI."""
    t = {x: f"read_parquet('{brewline.q(out)}/{x}/{x}.parquet')" for x in ("cafes", "customers", "products")}
    j = (f"read_parquet('{brewline.q(out)}/orders.parquet') LEFT JOIN {t['cafes']} USING (cafe_id)"
         f" LEFT JOIN {t['customers']} USING (customer_id) LEFT JOIN {t['products']} USING (product_id)")
    m = "strftime(order_date, '%Y-%m')"
    rows = duckdb.connect().execute(f"SELECT {dim.split('.')[1] if dim else 'NULL'}, sum(revenue) FILTER ({m} = ?),"
                                    f" sum(revenue) FILTER ({m} = ?) FROM {j} WHERE {m} IN (?, ?) AND {where} GROUP BY ALL",
                                    [a, b, a, b]).fetchall()
    return {k: (float(x or 0), float(y or 0)) for k, x, y in rows}


def cents(x):
    return round(float(x), 2)


@pytest.fixture(scope="module")
def brew(tmp_path_factory):
    """Brewline as the bench loads it, and Bob, a Viewer who may see the North region's cafés only and not their format."""
    out = tmp_path_factory.mktemp("brew")
    if P in projects():  # an interrupted run's
        drop([P])
    d = bench.data(out)
    assert abi.load("demo", P, d["files"])["readiness"]["ready"]
    bob, pw = f"explain_bob_{secrets.token_hex(3)}", secrets.token_urlsafe(16)
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


def test_the_latest_months_change_by_dimension_is_the_files_to_the_cent_within_3_s(brew):
    t0 = time.perf_counter()
    r = abi.call("explain_change", project=P, measure="revenue")  # the module's first call: nothing of it is cached
    seconds = time.perf_counter() - t0
    assert seconds < 3, seconds
    w = brew["known"]["why"]
    a, b = w["previous_month"], w["month"]
    assert r["columns"] == ["dimension", "member", "period_a", "period_b", "a", "b", "change", "change_pct", "share_pct"]
    assert all(row[2:4] == [a, b] for row in r["rows"])
    (ta, tb), = hand(brew["out"], "", a, b).values()
    total = r["rows"][0]
    assert total[:2] == ["total", None] and [cents(x) for x in total[4:7]] == [cents(ta), cents(tb), cents(tb - ta)]
    assert round(total[7], 9) == round((tb - ta) / ta, 9) and total[8] == 1
    assert 1 <= len(r["dimensions"]) <= 5 and r["dimensions"][0] == f"cafes.{w['dimension']}"  # where the change sits
    for dim in r["dimensions"]:
        got = [row for row in r["rows"] if row[0] == dim]
        want = hand(brew["out"], dim, a, b)
        top = max(want, key=lambda k: abs(want[k][1] - want[k][0]))
        assert (dim in r["partial"]) == (len(want) > 3), dim  # round 2: "all three formats" of the four, top 3 shown
        # M2 review: a partial one ends with the members not shown, added up, so its rows add up to the total change
        assert [row[1] == "(others)" for row in got] == [False] * min(len(want), 3) + [True] * (len(want) > 3), (dim, got)
        assert round(sum(row[6] for row in got), 2) == cents(tb - ta) and round(sum(row[8] for row in got), 9) == 1, dim
        got = got[:3]
        assert got[0][1] == top, (dim, got[0], top)
        x, y = want[top]
        assert [cents(v) for v in got[0][4:7]] == [cents(x), cents(y), cents(y - x)], dim
        assert round(got[0][8], 9) == round((y - x) / (tb - ta), 9) and round(got[0][7], 9) == round((y - x) / x, 9)
        assert [abs(row[6]) for row in got] == sorted((abs(row[6]) for row in got), reverse=True)
    north = next(row for row in r["rows"] if row[1] == w["member"])
    assert [cents(v) for v in north[4:6]] == [cents(v) for v in w["member_revenue"].values()]
    assert round(north[8], 4) == w["share_of_change"]
    assert "products.note" in r["also_checked"]  # a note per product is prose, not a breakdown
    assert "orders.payment" in r["also_checked"]  # M2 review: drawn per order, its "card holds 77%" was noise answers promoted
    assert state() == brew["before"]


def test_members_add_up_and_named_periods_and_dimensions_are_the_files_too(brew):
    """Every member of a dimension (a café's NULL customer segment too) adds up to the total change, so the shares to 1;
    December 2025 against December 2024, by the dimensions asked, in their order."""
    r = abi.call("explain_change", project=P, measure="revenue", dimensions=["customers.segment", "cafes.region"], top=20)
    assert r["dimensions"] == ["customers.segment", "cafes.region"] and r["also_checked"] == [] == r["partial"]
    for dim in r["dimensions"]:
        got = [row for row in r["rows"] if row[0] == dim]
        assert round(sum(row[6] for row in got), 2) == cents(r["rows"][0][6]) and round(sum(row[8] for row in got), 9) == 1
    assert None in {row[1] for row in r["rows"] if row[0] == "customers.segment"}  # orders with no customer
    r = abi.call("explain_change", project=P, measure="revenue", a="2024-12", b="2025-12", dimensions=["cafes.region"], top=4)
    want = hand(brew["out"], "cafes.region", "2024-12", "2025-12")
    assert {row[1]: [cents(v) for v in row[4:7]] for row in r["rows"][1:]} == {
        k: [cents(x), cents(y), cents(y - x)] for k, (x, y) in want.items()}
    assert all(row[2:4] == ["2024-12", "2025-12"] for row in r["rows"])
    q = abi.call("explain_change", project=P, measure="revenue", period="quarter", dimensions=["cafes.region"])
    assert q["rows"][0][2:4] == ["2025-Q3", "2025-Q4"]  # the data reaches 31 December: Q4 is complete
    assert state() == brew["before"]


def test_a_restricted_viewer_gets_only_their_members_and_totals_and_no_hidden_column(brew):
    w = brew["known"]["why"]
    (na, nb), = hand(brew["out"], "", w["previous_month"], w["month"], "region = 'North'").values()
    with abi.as_user(brew["bob"]):
        r = abi.call("explain_change", project=P, measure="revenue")
        assert [cents(x) for x in r["rows"][0][4:6]] == [cents(na), cents(nb)]  # totals over his rows only
        assert {row[1] for row in r["rows"] if row[0] == "cafes.city"} <= NORTH
        assert "cafes.format" not in r["dimensions"] + r["also_checked"]
        r = abi.call("explain_change", project=P, measure="revenue", dimensions=["cafes.region", "cafes.city"], top=20)
        assert [row[1] for row in r["rows"] if row[0] == "cafes.region"] == ["North"]
        assert {row[1] for row in r["rows"] if row[0] == "cafes.city"} == NORTH
        with pytest.raises(abi.ActionError) as e:
            abi.call("explain_change", project=P, measure="revenue", dimensions=["cafes.format"])
        assert e.value.status == 403 and "cafes.format" in e.value.body["message"], e.value.body
    assert state() == brew["before"]


def test_an_answer_written_from_its_rows_passes_the_harness_grounding(brew):
    r = abi.call("explain_change", project=P, measure="revenue")
    total, top = r["rows"][0], r["rows"][1]
    answer = (f"Revenue fell {abs(total[7]) * 100:.1f}% in December 2025, from {total[4]:,.2f} in November to {total[5]:,.2f}"
              f" ({total[6]:,.2f}). {top[1]} drove most of it: from {top[4]:,.2f} to {top[5]:,.2f}, a change of {top[6]:,.2f}"
              f" ({top[7] * 100:.1f}%), {top[8] * 100:.0f}% of the whole change.")
    areas = json.loads((abi.ROOT / "harness/areas.json").read_text())
    got = ground.run({"answer": answer, "results": [r], "asked": WHY, "project": P, "model": abi.call("semantic_model", project=P),
                      "vocab": [*areas["core"], *(t for x in areas["areas"].values() for t in x["tools"]), "ABI"]})
    assert (got["numbers"], got["names"]) == ([], []), (answer, got)
    assert "-1,685.20" in answer and "82% of the whole change" in answer  # the planted drop, as the bench knows it


def test_what_explain_change_cant_compare_is_refused(brew):
    for args, says in (({"measure": "revenue_change"}, "compares the periods itself"), ({"measure": "nope"}, "Unknown measure nope"),
                       ({"measure": "revenue", "b": "2025-13"}, "'2025-13' is not a month"),
                       ({"measure": "revenue", "period": "quarter", "b": "2025-12"}, "write it like 2025-Q4"),
                       ({"measure": "revenue", "a": "2025-12", "b": "2025-12"}, "the same month"),
                       # M2 review: a date filter emptied period a, so its "change" was all of b
                       ({"measure": "revenue", "filters": [{"column": "calendar.month", "op": "=", "value": 12}]},
                        "leave calendar.month out of the filters")):
        with pytest.raises(abi.ActionError) as e:
            abi.call("explain_change", project=P, **args)
        assert e.value.status == 400 and says in e.value.body["message"], (args, e.value.body)
    assert state() == brew["before"]


def test_the_harness_offers_it_directly_and_in_viewer_mode():
    """Each core tool is in Pi's tool list from the start (.pi/mcp.json `direct`), and each core read is allowed in
    Viewer mode (index.ts READS): explain_change among them."""
    core = json.loads((abi.ROOT / "harness/areas.json").read_text())["core"]
    exposure = json.loads((abi.ROOT / ".pi/mcp.json").read_text())["mcpServers"]["abi"]["toolExposure"]
    ts = (abi.ROOT / ".pi/extensions/abi/index.ts").read_text()
    reads = set(re.findall(r'"(\w+)"', re.search(r"const READS = new Set\(\[(.*?)\]\);", ts, re.S)[1]))
    assert "explain_change" in core and {t for t, v in exposure.items() if v == "direct"} == set(core)
    assert set(core) - {"remember"} <= reads


def test_a_member_without_rows_in_a_period_counts_as_0_there(brew):
    """M2 review: every M2 test compared November and December 2025, where every member has rows in both, so the zero
    of a member with no rows in one period never ran. Brewline sells nothing on 25 December: each region's change is
    all of its 24 December (-100%), and the shares add up to 1. The governed revenue_change (M2.3) is empty there, as
    its description says (rank members with explain_change)."""
    r = abi.call("explain_change", project=P, measure="revenue", period="day", a="2025-12-24", b="2025-12-25",
                 dimensions=["cafes.region", "cafes.region"], top=4)  # the same dimension twice is read once
    regions = [row for row in r["rows"] if row[0] == "cafes.region"]
    assert sorted(row[1] for row in regions) == ["East", "North", "South", "West"] and r["dimensions"] == ["cafes.region"]
    assert r["rows"][0][5] == 0 and all(row[5] == 0 and row[6] == -row[4] and row[7] == -1 for row in regions), regions
    assert round(sum(row[8] for row in regions), 9) == 1 and r["partial"] == []
    q = abi.call("semantic_query", project=P, measures=["revenue", "revenue_prev", "revenue_change"],
                 dimensions=["calendar.day", "cafes.region"], filters=[{"column": "calendar.day", "op": "=", "value": "2025-12-25"}])
    assert {row[1]: (row[2], cents(row[3]), row[4]) for row in q["rows"]} == {row[1]: (None, cents(row[4]), None) for row in regions}
    assert state() == brew["before"]


@pytest.fixture(scope="module")
def retail(tmp_path_factory):
    """--plant retail (every café's Retail orders halve in December 2025) loaded as the bench loads it."""
    out = tmp_path_factory.mktemp("retail")
    if R in projects():
        drop([R])
    d = bench.data(out, "retail")
    assert abi.load("demo", R, d["files"])["readiness"]["ready"]
    try:
        yield d["known"]
    finally:
        if R in projects():
            drop([R])


def test_the_retail_plant_leads_with_the_category_and_a_ratio_has_no_shares(retail):
    """M2 review: the data-ranked default (M2.4 deviation 3) was built for this plant and checked only live. No region
    holds most of the change, so the product category leads and Retail tops it with its share, the categories not shown
    added up last. A ratio's members don't add up to its total: no share, no "(others)" ("South holds 200% of it")."""
    w = retail["why"]
    r = abi.call("explain_change", project=R, measure="revenue")
    cat = [row for row in r["rows"] if row[0] == "products.category"]
    assert r["dimensions"][0] == "products.category"
    assert cat[0][1] == w["member"] == "Retail" and round(cat[0][8], 4) == w["share_of_change"] and cat[-1][1] == "(others)"
    assert round(sum(row[6] for row in cat), 2) == cents(r["rows"][0][6])
    abi.call("set_measure", project=R, id="aov", name="Average order value", expression="DIVIDE([revenue], [orders_rows])")
    r = abi.call("explain_change", project=R, measure="aov", dimensions=["products.category"])
    assert [row[8] for row in r["rows"]] == [None] * 4 and "(others)" not in [row[1] for row in r["rows"]], r["rows"]
    assert r["partial"] == ["products.category"] and "none" in r["measures"]["share_pct"]


def test_a_period_that_is_over_with_its_last_days_closed_is_complete():
    """M2 review: a period counted as complete only when the data had a row on its last day, so a shop closed at weekends
    lost its latest month, every week, and a quarter or year ending on a weekend (data through Friday 29 December 2023
    gave 2021 against 2022). Now also when the period is over and the data reaches into its last days (2 for a week, 3
    otherwise); a month still filling, or one that ends 11 days early, still gives the one before."""
    code = ("import json\nfrom datetime import date\nfrom abi.semantic import explain as x\n"
            "print(json.dumps([x.label(x.complete(date.fromisoformat(d), g, date(2026, 11, 4)), g) for d, g in ["
            "('2025-11-28', 'month'), ('2025-11-28', 'week'), ('2023-12-29', 'quarter'), ('2023-12-29', 'year'),"
            " ('2025-12-30', 'month'), ('2025-12-31', 'week'), ('2025-12-20', 'month'), ('2026-11-03', 'month')]]))")
    r = subprocess.run(["sh", "-c", 'eval "$(scripts/engine.sh env)"; cd engine/backend; exec .venv/bin/python -c "$0"', code],
                       cwd=abi.ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout.splitlines()[-1]) == ["2025-11", "2025-W48", "2023-Q4", "2023", "2025-12", "2025-W52",
                                                     "2025-11", "2026-10"]
