"""M3.6 pages for people with row-level security (needs the engine at :8780, no LLM): each restricted read of a lake fact
is the fact's partials computed once per snapshot (grouped also by every column a person's filter reads) and sliced
through the person's own filter in the engine. Every visual of the report and a set of queries give each person exactly
what their own read gives (ABI_RLS_SHARED_ROWS=0, 1.2's path), after new data and after a rule change; what doesn't
decompose takes their own read; nothing shared reaches anyone but through their own filter."""
import concurrent.futures as cf
import contextlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request

import pytest
from conftest import drop
from test_pages import PY, sure

import abi
import app
from pi_driver import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import brewline  # noqa: E402
import duckdb  # noqa: E402

P = "pi_rls_shared"
WHO = ("one", "several", "region", "none")
MEASURES = {"avg_revenue": "AVG(orders.revenue)", "customers": "DISTINCT_COUNT(orders.customer_id)",
            "median_revenue": "MEDIAN(orders.revenue)", "max_quantity": "MAX(orders.quantity)",
            "big_revenue": "WHERE([revenue], orders.quantity > 1)"}
SHARE = {"name": "share", "expr": "DIVIDE([revenue], IGNORE([revenue], cafes.city))"}
QUERIES = [  # beside every visual of the report: totals, filters, time, AVG, MIN/MAX, WHERE, top N, M2.1, holistic ones
    {"measures": ["revenue", "orders_rows", "avg_revenue", "max_quantity", "big_revenue"]},
    {"measures": ["revenue"], "dimensions": ["cafes.city"], "order_by": [{"field": "revenue", "desc": True}], "limit": 3},
    {"measures": ["quantity", "quantity_prev", "quantity_change_pct"], "dimensions": ["calendar.year", "calendar.month"]},
    {"measures": ["revenue", "avg_revenue"], "dimensions": ["products.category"], "totals": [[]]},
    {"measures": ["revenue"], "dimensions": ["cafes.region"], "filters": [{"column": "products.category", "op": "<>", "value": "Tea"}]},
    {"measures": ["revenue"], "dimensions": ["cafes.city"], "expressions": [SHARE]},
    {"measures": ["customers", "revenue"], "dimensions": ["cafes.city"]},
    {"measures": ["median_revenue"], "dimensions": ["products.category"]},
]

# The visuals the starter report had until 1.2.5, which dropped summed prices and discounts from its KPIs and draws
# months instead of days: kept here as queries, so the checks still cover a read of the discount column (refused under
# a column rule) and a daily partial, whatever the starter report holds.
WAS = [{"measures": ["unit_price"]}, {"measures": ["discount"]}, {"measures": ["quantity"], "dimensions": ["calendar.day"]}]

# The engine's own code in a process of its own, driven one JSON line at a time: each person's every visual and query
# with shared partials (ABI_RLS_SHARED_ROWS) and without (0: each person's own read), compared value for value and type
# for type (a DOUBLE within 1e-9: DuckDB's own parallel sums vary as much).
DRIVER = r"""
import concurrent.futures as cf, json, sys, time
from abi import actions, db, security, semantic, storage
import abi.reports, abi.semantic.sql, abi.semantic.rls  # noqa: F401 - registers visual_data, sql_query, set_column_rule
from abi.project import imports
from abi.semantic import rls

def same(a, b):
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1, abs(a), abs(b))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(map(same, a, b))
    return type(a) is type(b) and a == b

def one(p, u, item):
    try:
        if item[0] == "v":
            r = actions.run("visual_data", {"project": p, "report": "overview", "page": item[1], "visual": item[2]}, u, "rest")
        else:
            r = actions.run("semantic_query", {"project": p, **item[1]}, u, "rest")
        return r["columns"], r["rows"]
    except actions.ActionFailed as e:
        return f"{e.status}: {e}"

def tables(p):
    con = storage.lake(p)
    built = {k: v and v[0] for k, v in semantic._parts.get(con, {}).items()}
    have = {r[0] for r in con.cursor().execute("SELECT table_name FROM duckdb_tables() WHERE database_name = 'memory'"
                                               " AND schema_name = 'shared'").fetchall()}
    snap = storage.snapshot_id(con)
    return {"names": sorted(n for (s, _), n in built.items() if n and s == snap), "none": sum(n is None for n in built.values()),
            "old": sorted(n for (s, _), n in built.items() if n and s < snap), "have": sorted(have)}

sliced, slice_ = [], semantic._shared
semantic._shared = lambda *x: (v := slice_(*x), sliced.append(1))[0]  # each read through shared partials

for line in sys.stdin:
    a = json.loads(line)
    with db.connect() as c:
        users = {w: security.user(c, int(i)) for w, i in a["users"].items()}
    p, out = a["project"], {}
    if a["cmd"] == "check":
        rep = imports.load(p, "report", "overview")
        items = [("v", pg["id"], v["id"]) for pg in rep["pages"] for v in pg["visuals"] if "query" in v] + [("q", q) for q in a["queries"]]
        label = lambda i: f"v:{items[i][2]}" if items[i][0] == "v" else f"q:{i - sum(x[0] == 'v' for x in items)}"
        work = [(w, i) for w in users for i in range(len(items))]
        semantic.SHARED_ROWS = 0
        ref = {k: one(p, users[k[0]], items[k[1]]) for k in work}
        semantic.SHARED_ROWS, sliced[:] = a.get("rows", 1_000_000), []
        with cf.ThreadPoolExecutor(8) as ex:  # at once, as a page's visuals and many people come
            got = dict(zip(work, ex.map(lambda k: one(p, users[k[0]], items[k[1]]), work)))
        out = {"n": len(work), "diff": [[k[0], items[k[1]], str(ref[k])[:300], str(got[k])[:300]] for k in work if not same(ref[k], got[k])],
               "rows": {f"{w} {label(i)}": got[w, i] for w, i in work}, "sliced": len(sliced), **tables(p)}
    elif a["cmd"] == "plan":
        shared = {}
        semantic.plan(p, a["q"], users[a["who"]].id, a["q"].pop("expressions", []), shared=shared)
        out = {"facts": sorted(f for f, _ in shared.values())}
    elif a["cmd"] == "plans":  # people with the same hidden columns share a plan; a new time zone is a new one
        from abi.semantic import planner
        calls, plan = [], planner.plan
        planner.plan = lambda *x, **k: calls.append(1) or plan(*x, **k)
        try:
            q = {"measures": ["quantity"], "dimensions": ["calendar.year", "calendar.month"], "limit": 7}
            sqls = [semantic.plan(p, q, users[w].id, shared={})[1] for w in ("one", "several", "one")]
            n, zone = len(calls), semantic._locale
            semantic._locale = lambda p_: {**zone(p_), "time_zone": "Asia/Tokyo"}
            try:
                semantic.plan(p, q, users["one"].id, shared={})
            finally:
                semantic._locale = zone
        finally:
            planner.plan = plan
        out = {"same": len(set(sqls)) == 1, "calls": n, "after_zone": len(calls)}
    elif a["cmd"] == "inject":  # a shared build that fails quoting the data (a café ID that isn't the person's)
        build, start = semantic._partials, time.time()
        semantic._partials = lambda p_, con, snap, fact, sql: build(p_, con, snap, fact, sql.replace(
            "SELECT ", 'SELECT CAST("cafe_id" AS INTEGER) AS "__bad", ', 1))
        try:
            got = one(p, users[a["who"]], ("q", a["q"]))
        finally:
            semantic._partials = build
        with db.connect() as c:
            log = c.execute("SELECT user_id, sql, error FROM query_log WHERE project = %s AND at >= to_timestamp(%s)",
                            [p, start]).fetchall()
        out = {"got": got, "theirs": [r for r in log if r["user_id"] == users[a["who"]].id],
               "system": [r["error"] for r in log if r["user_id"] is None and r["error"]]}
    elif a["cmd"] == "page":  # one read action for a page: the snapshot read once, SQL checked once for good
        n, sid, rc = {}, storage.snapshot_id, rls.refused_calls
        storage.snapshot_id = lambda con: n.__setitem__("snap", n["snap"] + 1) or sid(con)
        rls.refused_calls = lambda *x: n.__setitem__("check", n["check"] + 1) or rc(*x)
        try:
            runs = []
            for _ in range(2):
                n.update(snap=0, check=0)
                r = actions.run("visual_data", {"project": p, "report": "overview", "page": "overview"}, users[a["who"]], "rest")
                runs.append({"visuals": len(r["visuals"]), "rows": sum("rows" in v for v in r["visuals"]), **n})
        finally:
            storage.snapshot_id, rls.refused_calls = sid, rc
        sql, uid = 'SELECT count(*) FROM "orders"', users[a["who"]].id
        rls.check(p, uid, sql, {"orders"})
        try:
            rls.check(p, uid, sql, {"cafes"})  # the same SQL, without orders among the names it may read
            refused = None
        except actions.ActionFailed as e:
            refused = e.status
        out = {"runs": runs, "refused": refused}
    elif a["cmd"] == "audit":  # the person's audit rows since `since`
        with db.connect() as c:
            out = {"rows": [[r["action"], r["status"], (r["input"] or {}).get("visual"), r["detail"]] for r in c.execute(
                "SELECT action, status, input, detail FROM audit_log WHERE user_id = %s AND at >= to_timestamp(%s) ORDER BY id",
                [users[a["who"]].id, a["since"]]).fetchall()]}
    elif a["cmd"] == "inbound":  # a filter value an inbound message may carry, pushed into a shared build
        start = time.time()
        r = actions.run("semantic_query", {"project": p, "measures": ["revenue"], "dimensions": ["cafes.city"], "filters": [
            {"column": "orders.customer_id", "op": "<>", "value": a["marker"]}]}, users[a["who"]], "inbound")
        with db.connect() as c:
            log = c.execute("SELECT user_id, query, sql, error FROM query_log WHERE project = %s AND at >= to_timestamp(%s)",
                            [p, start]).fetchall()
        out = {"rows": len(r["rows"]), "built": sum(r_["user_id"] is None and (r_["query"] or {}).get("rls") == "partials" for r_ in log),
               "clear": [r_["sql"][:200] for r_ in log if a["marker"] in (r_["sql"] or "") + (r_["error"] or "")]}
    elif a["cmd"] == "budget":  # every kept partials table counts against SHARED_ROWS; one that doesn't fit isn't built past it
        con = storage.lake(p)
        snap = storage.snapshot_id(con)
        q = lambda k, n: f'SELECT "{k}", count(*) AS "__n{n}" FROM "orders" GROUP BY 1'
        held = semantic._held(con, snap)
        try:
            semantic.SHARED_ROWS = held + a["rows"]
            first = semantic._partials(p, con, snap, "orders", q("cafe_id", a["n"]))
            n = semantic._parts[con][snap, q("cafe_id", a["n"])][1]
            semantic.SHARED_ROWS = held + n + a["more"]
            start = time.time()
            second = semantic._partials(p, con, snap, "orders", q("customer_id", a["n"]))
            with db.connect() as c:
                logged = c.execute("SELECT rows FROM query_log WHERE project = %s AND user_id IS NULL AND at >= to_timestamp(%s)"
                                   " AND query->>'rls' = 'partials'", [p, start]).fetchall()
        finally:
            semantic.SHARED_ROWS = 1_000_000
        out = {"first": bool(first), "second": second, "logged": [r["rows"] for r in logged], "more": a["more"]}
    elif a["cmd"] == "race":  # a column rule committed after this action read hidden() and before its first digest()
        with db.connect() as c:
            owner = security.user(c, c.execute("SELECT id FROM users WHERE workspace_owner ORDER BY id LIMIT 1").fetchone()["id"])
        dig, fired, who = rls.digest, [], users[a["who"]]

        def digest(p_):
            if not fired:
                fired.append(rls.hidden(p_, who.id))  # the action's hidden columns, before the rule
                rule = {"project": p_, "id": "racedisc", "table": "orders", "columns": ["discount"]}
                try:
                    actions.run("set_column_rule", rule, owner, "rest")
                except actions.ActionFailed as e:
                    actions.run("set_column_rule", rule, owner, "rest", e.body["confirm_token"])
            return dig(p_)
        rls.digest = digest
        try:
            one(p, who, ("q", {"measures": ["revenue"], "dimensions": ["cafes.city"]}))
        finally:
            rls.digest = dig
        try:  # a later action: the column is hidden from them
            got = actions.run("sql_query", {"project": p, "sql": 'SELECT order_id, discount FROM "orders" LIMIT 3'}, who, "rest")["rows"]
        except actions.ActionFailed as e:
            got = e.status
        out = {"fired": [sorted(x) for x in fired], "hidden": {t: sorted(c) for t, c in rls.hidden(p, who.id).items()}, "sql": got}
    print(json.dumps(out, default=str), flush=True)
"""


class Driver:
    def __init__(self, users: dict):
        e = subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout
        self.users = users
        self.p = subprocess.Popen(["sh", "-c", f'{e}\nexec {PY} -c "$0"', DRIVER], cwd=ROOT, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)

    def __call__(self, cmd: str, project: str = P, **args) -> dict:
        self.p.stdin.write(json.dumps({"cmd": cmd, "project": project, "users": self.users, **args}) + "\n")
        self.p.stdin.flush()
        while (line := self.p.stdout.readline()) and not line.startswith("{"):  # anything else the engine prints
            pass
        assert line, "the engine driver stopped"
        return json.loads(line)

    def close(self):
        self.p.stdin.close()
        self.p.wait(30)


@pytest.fixture(scope="module")
def brew(tmp_path_factory):
    """P: Brewline (37,000 orders) with its Overview report and more measures; Viewers who see one café, several, a
    region (a rule on cafes.region, so orders through their cafés) and none (a café no row has). Gone at the end."""
    d = tmp_path_factory.mktemp("brew")
    brewline.generate(d, 0.005)
    duckdb.sql(f"COPY (SELECT * FROM read_parquet('{d}/orders/*.parquet')) TO '{d}/orders.parquet' (FORMAT parquet)")
    if P in {x["project"] for x in abi.call("list_projects")["projects"]}:
        drop([P])
    abi.load("demo", P, [str(d / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(d / "orders.parquet")])
    for i, x in MEASURES.items():
        abi.call("set_measure", project=P, id=i, name=i, expression=x)
    cafes = [r[0] for r in abi.call("sql_query", project=P, sql="SELECT cafe_id FROM cafes ORDER BY 1")["rows"]]
    regions = [r[0] for r in abi.call("sql_query", project=P, sql="SELECT DISTINCT region FROM cafes ORDER BY 1")["rows"]]
    stamp, made, tokens = time.strftime("%H%M%S"), {}, {}
    try:
        for w in WHO:
            name, pw = f"pi_rls_{w}_{stamp}", secrets.token_urlsafe(16)
            abi.call("create_user", name=name, password=pw)
            made[w] = name
            abi.call("set_role", project=P, user=name, role="viewer")
            tokens[w] = abi.login(name, pw)[1]["token"]
        ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}
        uid = {w: ids[made[w]] for w in WHO}
        sees = {"one": cafes[:1], "several": [cafes[1], cafes[4], cafes[8]], "region": cafes, "none": ["CF999"]}
        sure("set_rls_rule", project=P, id="cafe", table="cafes", column="cafe_id", users={uid[w]: v for w, v in sees.items()})
        sure("set_rls_rule", project=P, id="region", table="cafes", column="region",
             users={uid[w]: ["North"] if w == "region" else regions for w in WHO})
        drv = Driver(uid)
        yield {"uid": uid, "names": made, "tokens": tokens, "drv": drv, "cafes": cafes, "sees": sees, "dir": d}
        drv.close()
    finally:
        for name in made.values():
            sure("delete_user", name=name)
        drop([x for x in (P, f"{P}_pub") if x in {y["project"] for y in abi.call("list_projects")["projects"]}])


def exact(r: dict, shared: bool = True) -> None:
    assert r["n"] > 30 and not r["diff"], r["diff"][:3]
    assert (len(r["names"]) >= 5 and r["sliced"] > r["n"] / 2) is shared and not r["old"] and set(r["names"]) <= set(r["have"]), r


def test_every_visual_and_query_is_each_persons_own_after_new_data(brew):
    """The critical one: four restricted Viewers at once, each visual of the report and each query, with shared
    partials and without, equal to the cent; again after new data (a new snapshot: new tables, the old ones gone)."""
    drv = brew["drv"]
    r = drv("check", queries=QUERIES + WAS)
    exact(r)
    share = r["rows"]["several q:5"][1]  # the IGNORE expression: shares of the person's own total, never past their rule
    assert 1 <= len(share) <= 3 and abs(sum(float(x[2]) for x in share) - 1) < 1e-9, share
    assert r["rows"]["none v:kpi-orders_rows"][1] == [[0]] and r["rows"]["none q:1"][1] == [], "a rule no row passes: nothing"
    assert r["rows"]["one q:1"][1] != r["rows"]["several q:1"][1]
    first = set(r["names"])
    d = brew["dir"]
    duckdb.sql(f"COPY (SELECT * FROM '{d}/orders.parquet' WHERE order_date < DATE '2025-07-01') TO '{d}/orders2.parquet' (FORMAT parquet)")
    abi.upload(P, "orders", d / "orders2.parquet")
    r = drv("check", queries=QUERIES + WAS)
    exact(r)
    assert not first & set(r["names"]) and not first & set(r["have"]), "the old snapshot's tables are gone"


def test_what_doesnt_decompose_and_a_hidden_column_take_the_persons_own_read(brew):
    drv = brew["drv"]
    assert drv("plan", who="one", q={"measures": ["revenue"], "dimensions": ["cafes.city"]})["facts"] == ["orders"]
    assert drv("plan", who="one", q={"measures": ["revenue"], "dimensions": ["cafes.city"], "expressions": [SHARE]})["facts"] == ["orders", "orders"]
    for m in ("customers", "median_revenue", "avg_revenue"):  # DISTINCT_COUNT, MEDIAN; AVG isn't SUM/COUNT to the bit
        assert drv("plan", who="one", q={"measures": [m, "revenue"], "dimensions": ["cafes.city"]})["facts"] == [], m
    r = drv("check", queries=QUERIES + WAS, rows=10)  # partials of more than 10 rows: each person's own read
    assert r["n"] > 50 and not r["diff"] and r["sliced"] == 0, (r["diff"][:3], r["sliced"])
    sure("set_column_rule", project=P, id="nodiscount", table="orders", columns=["discount"])
    try:
        assert drv("plan", who="one", q={"measures": ["revenue"], "dimensions": ["cafes.city"]})["facts"] == []
        r = drv("check", queries=QUERIES + WAS)
        assert r["n"] > 50 and not r["diff"], r["diff"][:3]
        assert any(isinstance(v, str) and v.startswith("403") for v in r["rows"].values()), "the discount visual is refused"
    finally:
        sure("delete_rls_rule", project=P, id="nodiscount")


def test_people_who_see_different_rows_share_one_plan(brew):
    r = brew["drv"]("plans")
    assert r == {"same": True, "calls": 1, "after_zone": 2}, r


BUNDLE = r"""
import json, shutil, tempfile
from pathlib import Path
from abi.project import bundle
d = Path(tempfile.mkdtemp())
for i in range(3000):
    (d / ".git" / "objects" / f"{i % 30:02x}").mkdir(parents=True, exist_ok=True)
    (d / ".git" / "objects" / f"{i % 30:02x}" / f"o{i}").write_bytes(bytes([255, 0]))  # not text: read, it would fail
(d / "model").mkdir()
(d / "model" / "model.json").write_text('{"a": 1}')
(d / "docs" / "sub" / ".git").mkdir(parents=True)  # a nested repo
(d / "docs" / "sub" / ".git" / "HEAD").write_text("x")
(d / "docs" / "sub" / "a.md").write_text("# a\r\n")
(d / "tools" / ".git").parent.mkdir()
(d / "tools" / ".git").write_text("gitdir: elsewhere")  # a worktree's .git file
seen, rel = [], Path.relative_to
Path.relative_to = lambda self, *a: seen.append(str(self)) or rel(self, *a)  # each path the walk looked at
try:
    out = {"bundle": bundle.bundle(d)}
finally:
    Path.relative_to = rel
out["into_git"] = [p for p in seen if ".git" in Path(p).parts]
(d / "docs" / "link").symlink_to(d / "model")
try:
    bundle.bundle(d)
except ValueError as e:
    out["link"] = str(e)
shutil.rmtree(d)
print(json.dumps(out))
"""


def test_a_bundle_never_walks_into_git():
    """M3.6: bundle() walked the repo's objects (327 of 330 ms on a bench project) to skip them; it never enters .git."""
    r = subprocess.run(["sh", "-c", f'{subprocess.run(["sh", "-c", "scripts/engine.sh env"], cwd=ROOT, capture_output=True, text=True).stdout}'
                        f'\nexec {PY} -c "$0"', BUNDLE], cwd=ROOT, capture_output=True, text=True, timeout=120)
    out = json.loads(r.stdout or "null") or r.stderr[-2000:]
    assert out == {"bundle": {"docs/sub/a.md": "# a\r\n", "model/model.json": {"a": 1}}, "into_git": [], "link": "docs/link: symlinks can't be bundled."}, out


def test_a_failed_shared_build_never_reaches_the_person(brew):
    """A build that fails quoting a café ID (a bad cast): the person gets their own rows, and neither their answer nor
    their query_log rows hold anything of it; only the system read's row has the error."""
    q = {"measures": ["revenue"], "dimensions": ["cafes.city"]}
    r = brew["drv"]("inject", who="one", q=q)
    assert r["system"] and any(re.search(r"CF\d{3}", e) for e in r["system"]), r["system"]  # the injection did quote one
    mine = brew["sees"]["one"][0]
    text = json.dumps([r["got"], r["theirs"]])
    assert isinstance(r["got"], list) and set(re.findall(r"CF\d{3}", text)) <= {mine}, text[:500]
    assert r["theirs"] and all(x["error"] is None for x in r["theirs"]), r["theirs"]
    with abi.as_user(brew["tokens"]["one"]):
        assert [[c, str(v)] for c, v in abi.call("semantic_query", project=P, **q)["rows"]] == r["got"][1]  # DECIMAL as text


def visual(tok: str, v: str) -> list:
    with abi.as_user(tok):
        return abi.call("visual_data", project=P, report="overview", page="overview", visual=v)["rows"]


def owner_kpi(cafes: list | None) -> list:
    f = [{"column": "cafes.cafe_id", "op": "in", "value": cafes}] if cafes else []
    return abi.call("semantic_query", project=P, measures=["orders_rows"], filters=f)["rows"]


def test_through_the_engine_each_viewer_gets_only_theirs_and_every_change_at_once(brew):
    """Through the engine's REST door, Viewers at once: each KPI is the owner's figure for their cafés; restricted SQL
    can't name a shared table; a rule change, a role change and a column rule are seen on the next read."""
    t, sees, names = brew["tokens"], brew["sees"], brew["names"]
    with cf.ThreadPoolExecutor(8) as ex:
        got = dict(zip(WHO * 2, ex.map(lambda w: visual(t[w], "kpi-orders_rows"), WHO * 2)))
    for w in ("one", "several"):
        assert got[w] == owner_kpi(sees[w]), w
    assert got["none"] in ([[0]], [[None]], []), got["none"]
    with abi.as_user(t["one"]):
        for sql in ("SELECT * FROM memory.shared.p_0000000000000000", 'SELECT * FROM "__lake0"'):
            with pytest.raises(abi.ActionError) as e:
                abi.call("sql_query", project=P, sql=sql)
            assert e.value.status == 403, sql
    before = visual(t["one"], "kpi-orders_rows")
    rules = {r["id"]: r for r in abi.call("rls_rules", project=P)["rules"]}
    users = {u["id"]: u["values"] for u in rules["cafe"]["users"]}
    sure("set_rls_rule", project=P, id="cafe", table="cafes", column="cafe_id",
         users={**users, brew["uid"]["one"]: [brew["cafes"][2]]})
    assert visual(t["one"], "kpi-orders_rows") == owner_kpi([brew["cafes"][2]]) != before, "a rule change, at once"
    abi.call("set_role", project=P, user=names["one"], role="editor")
    assert visual(t["one"], "kpi-orders_rows") == owner_kpi(None), "an Editor reads every row"
    abi.call("set_role", project=P, user=names["one"], role="viewer")
    assert visual(t["one"], "kpi-orders_rows") == owner_kpi([brew["cafes"][2]]), "and a Viewer again only theirs"
    sure("set_column_rule", project=P, id="noqty", table="orders", columns=["quantity"])
    try:
        with pytest.raises(abi.ActionError) as e:
            visual(t["one"], "kpi-quantity")
        assert e.value.status == 403
    finally:
        sure("delete_rls_rule", project=P, id="noqty")
    assert visual(t["one"], "kpi-quantity") == abi.call("semantic_query", project=P, measures=["quantity"], filters=[
        {"column": "cafes.cafe_id", "op": "in", "value": [brew["cafes"][2]]}])["rows"]
    sure("set_rls_rule", project=P, id="cafe", table="cafes", column="cafe_id", users=users)


def test_a_page_is_one_read_with_each_visual_as_its_own(brew):
    """M3.6 round 2: a page's visuals in one visual_data call (the app's page) are each what their own call gives,
    for each Viewer and the owner, a refused visual (a column rule) among them as its own error; inside it the
    snapshot is read once and a planned SQL checked once (each was paid per visual by 50 people on pve1)."""
    t = brew["tokens"]
    page = lambda: abi.call("visual_data", project=P, report="overview", page="overview")  # noqa: E731
    vs = abi.call("list_reports", project=P)["reports"][0]["pages"][0]["visuals"]
    ids, n = [v["id"] for v in vs], sum("query" in v for v in vs)
    for w in (*WHO, None):
        with abi.as_user(t[w]) if w else contextlib.nullcontext():
            got = page()
            one = [abi.call("visual_data", project=P, report="overview", page="overview", visual=v) for v in ids]
        assert got["page"] == "overview" and json.dumps(got["visuals"], sort_keys=True) == json.dumps(one, sort_keys=True), w
    r = brew["drv"]("page", who="several")
    assert r == {"runs": [{"visuals": len(ids), "rows": n, "snap": 1, "check": r["runs"][0]["check"]},
                          {"visuals": len(ids), "rows": n, "snap": 1, "check": 0}], "refused": 403}, r
    sure("set_column_rule", project=P, id="noqty", table="orders", columns=["quantity"])
    try:
        with abi.as_user(t["one"]):
            got = {v["id"]: v for v in page()["visuals"]}
        assert got["kpi-quantity"]["status"] == 403 and "rows" not in got["kpi-quantity"], got["kpi-quantity"]
        assert got["kpi-orders_rows"]["rows"] == visual(t["one"], "kpi-orders_rows")
        since = time.time()
        with abi.as_user(t["one"]):
            page()
        rows = brew["drv"]("audit", who="one", since=since)["rows"]  # the refusal is audited as the visual's own call was
        refused = [r for r in rows if r[1] == 403]
        assert ["visual_data", 403, "kpi-quantity"] in [r[:3] for r in refused] and rows[-1][:3] == ["visual_data", 200, None], rows
        assert all(r[2] and "Column-level security" in r[3] for r in refused), rows
    finally:
        sure("delete_rls_rule", project=P, id="noqty")


def test_the_published_copy_is_sliced_the_same(brew):
    """A Viewer's page (Viewer mode) reads the published copy, a project of its own with the same rules and roles: its
    shared partials are its own, and every visual and query is each person's own there too."""
    pub = f"{P}_pub"
    try:
        abi.call("commit", project=P, message="rls shared test")
    except abi.ActionError as e:  # the rules' last change committed already
        assert e.status == 409, e
    hubs = set(app.HUBS)
    s = app.serve(P, 0, open_browser=False)
    port = s.server_address[1]
    key = re.search(r"KEY='([^']+)'", urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()).group(1)

    def page(path: str) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", b"{}", {"X-Abi-App": key, "Content-Type": "application/json",
                                                                             "X-Abi-Client": "rls", "X-Abi-Project": P})
        return json.loads(urllib.request.urlopen(req, timeout=300).read())
    try:
        r = page("/publish")
        if r.get("confirm"):
            r = page(f"/confirm/{r['confirm']['id']}")
        assert "result" in r, r
        # every table, not only orders: the worker loads the copy's tables one snapshot each in no set order (a set
        # of sources), so orders can land while cafes is still missing, and a person's own read then rightly sees no
        # rows (their filter goes through cafes); the check below compares two reads, so the data must not move
        count = lambda p: [abi.call("sql_query", project=p, sql=f"SELECT count(*) FROM {t}")["rows"]  # noqa: E731
                           for t in ("cafes", "customers", "products", "orders")]
        want = count(P)
        end = time.monotonic() + 240
        while time.monotonic() < end:  # the worker's minute, then the copy's load
            try:
                if count(pub) == want:
                    break
            except abi.ActionError:
                pass
            time.sleep(2)
        else:
            pytest.fail("the published copy's tables didn't load")
        r = brew["drv"]("check", project=pub, queries=QUERIES[:3] + WAS)
        exact(r)
    finally:
        s.shutdown()
        for k in set(app.HUBS) - hubs:
            app.HUBS.pop(k, None)
    shutil.rmtree(brew["dir"], ignore_errors=True)


def test_a_rule_committed_inside_a_read_never_leaves_its_views_unhidden(brew):
    """A column rule committed between a Viewer's first hidden() and first digest() inside one action: the RLS views
    cached for the new rules hide the column, so a later sql_query of it fails (it returned the column before)."""
    try:
        r = brew["drv"]("race", who="one")
        assert r["fired"] == [[]] and r["hidden"] == {"orders": ["discount"]}, r
        assert r["sql"] in (400, 403), r
    finally:
        sure("delete_rls_rule", project=P, id="racedisc")


def test_shared_builds_log_inbound_text_redacted_and_stay_within_their_budget(brew):
    """D-32: a shared build for an inbound message's read logs its SQL with the literal hashed, as the person's own
    rows are. ABI_RLS_SHARED_ROWS bounds every kept partials table together, and one that doesn't fit is never built
    past what is left (its system read holds at most that plus one row)."""
    marker = f"message-text-{secrets.token_hex(4)}"
    r = brew["drv"]("inbound", who="one", marker=marker)
    assert r["rows"] and r["built"] >= 1 and r["clear"] == [], r
    r = brew["drv"]("budget", who="one", rows=10_000, more=5, n=secrets.token_hex(3))
    assert r == {"first": True, "second": None, "logged": [6], "more": 5}, r


FOLLOW = {"objects": [{"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name"},
                      {"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "customer_id"}],
          "links": [{"id": "visited", "from": "customer", "to": "cafe", "name": "visited", "inverse": "visitors",
                     "via": ["orders-customer_id", "orders-cafe_id"], "rls": "follow"}]}
BY_CUSTOMER = [  # customers as the fact (their filter reads their key) and as a dimension of orders (never back along via)
    {"measures": ["customer_rows"], "dimensions": ["customers.segment"]},
    {"measures": ["customer_rows"]},
    {"measures": ["revenue", "orders_rows"], "dimensions": ["customers.segment"]},
    {"measures": ["revenue", "customers"], "dimensions": ["cafes.city", "customers.loyalty_member"]},
]


def test_a_follow_link_keeps_every_read_each_persons_own(brew):
    """1.2.6 M3: with customers following the cafés rule (rls: follow), every visual and query, the ones over customers
    too, is each person's own read with shared partials and without; customers are fewer, orders are not."""
    drv = brew["drv"]
    abi.call("set_measure", project=P, id="customer_rows", name="customer_rows", expression="COUNT(customers.customer_id)")
    try:
        mine = BY_CUSTOMER + QUERIES[:1]
        before = drv("check", queries=mine)
        sure("save_ontology", project=P, ontology=FOLLOW)
        exact(drv("check", queries=QUERIES + WAS))  # every visual and query, shared and sliced as without the link
        # the reads over customers: the same with and without shared partials (those over customers as the fact
        # never slice: partials grouped by their key are too big to share)
        r = drv("check", queries=mine)
        assert r["n"] > len(WHO) * len(mine) and not r["diff"], r["diff"][:3]
        for w in ("one", "several"):
            assert r["rows"][f"{w} q:1"][1][0][0] < before["rows"][f"{w} q:1"][1][0][0], "fewer customers"
            assert r["rows"][f"{w} q:4"] == before["rows"][f"{w} q:4"], "the same orders (anonymous ones too)"
        assert r["rows"]["none q:1"][1] == [[0]], "a rule no row passes: no customers"
    finally:
        sure("save_ontology", project=P, ontology={})
        sure("delete_measure", project=P, id="customer_rows")
