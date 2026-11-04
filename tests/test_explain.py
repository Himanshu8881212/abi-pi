"""explain_change (1.2 M2.4), "why did it change" in one call, on this machine's engine, no LLM. On Brewline at the bench's
scale (bench/brewline.py) the latest complete month against the one before: the total and each dimension's top member
with its change equal a hand-written DuckDB query over the files to the cent, the planted member leads the first
dimension, and the call answers uncached within 3 s; a dimension's members add up to the total change; named periods
and dimensions; a Viewer who may see North only gets North's members and totals over North's rows, and a column hidden
from him is no dimension; an answer written from the rows passes the harness's grounding; what can't be explained is a
400; measures.json and the project's Git are as they were after every call (rule 7). The harness offers the tool
directly and in Viewer mode."""
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

P = "pi_explain_test"
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
    assert 3 <= len(r["dimensions"]) <= 5 and r["dimensions"][0] == f"cafes.{w['dimension']}"  # where the change sits
    for dim in r["dimensions"]:
        got = [row for row in r["rows"] if row[0] == dim]
        want = hand(brew["out"], dim, a, b)
        top = max(want, key=lambda k: abs(want[k][1] - want[k][0]))
        assert 1 <= len(got) <= 3 and got[0][1] == top, (dim, got[0], top)
        assert (dim in r["partial"]) == (len(want) > 3), dim  # round 2: "all three formats" of the four, top 3 shown
        x, y = want[top]
        assert [cents(v) for v in got[0][4:7]] == [cents(x), cents(y), cents(y - x)], dim
        assert round(got[0][8], 9) == round((y - x) / (tb - ta), 9) and round(got[0][7], 9) == round((y - x) / x, 9)
        assert [abs(row[6]) for row in got] == sorted((abs(row[6]) for row in got), reverse=True)
    north = next(row for row in r["rows"] if row[1] == w["member"])
    assert [cents(v) for v in north[4:6]] == [cents(v) for v in w["member_revenue"].values()]
    assert round(north[8], 4) == w["share_of_change"]
    assert "products.note" in r["also_checked"]  # a note per product is prose, not a breakdown
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
                       ({"measure": "revenue", "a": "2025-12", "b": "2025-12"}, "the same month")):
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
