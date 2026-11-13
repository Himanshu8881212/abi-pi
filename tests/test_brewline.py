"""bench/brewline.py, the bench's data: the same seed and scale give the same bytes, the shape is the 1.0 release
dataset's, the orders scale linearly, and known_answers.json is what a fresh DuckDB query over the files says."""
import hashlib
import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
MONTHS = [f"orders/orders_{y}_{m:02d}.parquet" for y in (2023, 2024, 2025) for m in range(1, 13)]


def brewline(out: Path, *args) -> dict:
    """Run the generator as the bench does (its own process); the SHA-256 of every file it wrote."""
    subprocess.run([sys.executable, str(ROOT / "bench/brewline.py"), str(out), *args], check=True, capture_output=True)
    return {f.relative_to(out).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(out.rglob("*")) if f.is_file()}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    d = tmp_path_factory.mktemp("brewline")
    brewline(d / "half", "--scale", "0.005")
    return {"out": d / "a", "a": brewline(d / "a", "--csv"), "b": brewline(d / "b", "--csv"), "half": d / "half"}


def test_the_same_seed_and_scale_give_the_same_bytes_in_the_release_shape(runs):
    out = runs["out"]
    assert runs["a"] == runs["b"]  # two processes, byte for byte
    assert sorted(runs["a"]) == sorted(["brewline.csv", "known_answers.json", "cafes/cafes.parquet", "customers/customers.parquet",
                                        "products/products.parquet", *MONTHS])
    db = duckdb.connect()
    t = {n: f"read_parquet('{out}/{n}/*.parquet')" for n in ("cafes", "customers", "products", "orders")}
    assert db.execute(f"SELECT count(*), count(DISTINCT cafe_id), count(DISTINCT city), list(DISTINCT region ORDER BY region), "
                      f"max(opened_on) < DATE '2025-01-01' FROM {t['cafes']}").fetchone() == (120, 120, 12, ["East", "North", "South", "West"], True)
    assert db.execute(f"SELECT count(DISTINCT customer_id) FROM {t['customers']}").fetchone()[0] == 60_000
    assert db.execute(f"SELECT count(*), list(DISTINCT category ORDER BY category), min(list_price) > 0 FROM {t['products']}").fetchone() == (
        12, ["Bakery", "Coffee", "Food", "Retail", "Tea"], True)
    n, ids, lost, wrong = db.execute(f"""SELECT count(*), count(DISTINCT o.order_id),
       count(*) FILTER (c.cafe_id IS NULL OR p.product_id IS NULL OR (o.customer_id IS NOT NULL AND u.customer_id IS NULL)),
       count(*) FILTER (o.unit_price != p.list_price OR o.revenue != round(o.quantity * o.unit_price * (1 - o.discount), 2))
     FROM {t['orders']} o LEFT JOIN {t['cafes']} c ON c.cafe_id = o.cafe_id LEFT JOIN {t['products']} p ON p.product_id = o.product_id
     LEFT JOIN {t['customers']} u ON u.customer_id = o.customer_id""").fetchone()
    assert 72_500 < n < 75_500 and ids == n and lost == 0 and wrong == 0  # --scale 0.01: about 74k orders, all joining
    assert db.execute(f"SELECT count(*) FROM read_parquet('{out}/orders/*.parquet', filename = true) "
                      "WHERE NOT ends_with(filename, strftime(order_date, 'orders_%Y_%m.parquet'))").fetchone()[0] == 0  # a file a month
    half = json.loads((runs["half"] / "known_answers.json").read_text())["rows"]["orders"]
    assert abs(half / n - 0.5) < 0.02  # linear in --scale


def test_known_answers_are_what_a_fresh_query_over_the_files_says(runs):
    out = runs["out"]
    k = json.loads((out / "known_answers.json").read_text(), parse_float=Decimal)
    db = duckdb.connect()
    o, c = f"read_parquet('{out}/orders/*.parquet')", f"read_parquet('{out}/cafes/*.parquet')"
    by = {}
    for region, year, revenue in db.execute(f"SELECT region, year(order_date), sum(revenue) FROM {o} o JOIN {c} c ON c.cafe_id = o.cafe_id "
                                            "GROUP BY 1, 2").fetchall():
        by.setdefault(region, {})[str(year)] = revenue
    assert k["revenue_by_region_year"] == by and sorted(by) == ["East", "North", "South", "West"] and all(len(y) == 3 for y in by.values())
    assert k["rows"]["orders"] == db.execute(f"SELECT count(*) FROM {o}").fetchone()[0]
    assert db.execute(f"SELECT max(order_date) FROM {o}").fetchone()[0] == date(2025, 12, 31) and k["latest_complete_month"] == "2025-12"
    m = {r: (nov, dec) for r, nov, dec in db.execute(f"""SELECT region, sum(revenue) FILTER (order_date < DATE '2025-12-01'),
       sum(revenue) FILTER (order_date >= DATE '2025-12-01') FROM {o} o JOIN {c} c ON c.cafe_id = o.cafe_id
     WHERE order_date >= DATE '2025-11-01' GROUP BY 1""").fetchall()}
    nov, dec = sum(v[0] for v in m.values()), sum(v[1] for v in m.values())
    north = m["North"][1] - m["North"][0]
    w = k["why"]
    assert (w["month"], w["previous_month"], w["dimension"], w["member"]) == ("2025-12", "2025-11", "region", "North")
    assert (w["revenue"], w["change"], w["member_change"]) == ({"2025-11": nov, "2025-12": dec}, dec - nov, north)
    assert max(m, key=lambda r: abs(m[r][1] - m[r][0])) == "North"  # the member that moved most
    assert w["change_pct"] == round((dec - nov) / nov * 100, 2) < -8  # a noticeable fall
    assert w["share_of_change"] == round(north / (dec - nov), 4) > Decimal("0.6")  # mostly North
    assert w["member_orders"] == dict(zip(("2025-11", "2025-12"), db.execute(f"""SELECT count(*) FILTER (order_date < DATE '2025-12-01'),
       count(*) FILTER (order_date >= DATE '2025-12-01') FROM {o} o JOIN {c} c ON c.cafe_id = o.cafe_id
     WHERE order_date >= DATE '2025-11-01' AND region = 'North'""").fetchone()))  # M2 review: what moved it, fewer orders
    # the CSV door's file: CSV_SAFE's quoting (scripts/abi.py before 0ba59da) reads the same orders, notes over two lines
    csv = (f"""read_csv('{out}/brewline.csv', header = true, quote = '"', escape = '"', strict_mode = false, """
           "types = {'revenue': 'DECIMAL(10,2)'})")
    assert db.execute(f"""SELECT count(*), count(*) FILTER (contains(product_note, chr(10)) AND contains(product_note, '"')) > 0
                          FROM {csv}""").fetchone() == (k["rows"]["orders"], True)
    assert {(r, str(y)): s for r, y, s in db.execute(f"SELECT region, year(order_date), sum(revenue) FROM {csv} GROUP BY 1, 2").fetchall()} == {
        (r, y): v for r, ys in by.items() for y, v in ys.items()}


def test_the_retail_plant_moves_a_product_category_and_the_default_data_stays(runs, tmp_path):
    """--plant retail (the second "why" dataset): every café's Retail orders halve in December 2025, so every region
    falls, none with most of the change, and known_answers.json, from a fresh query, names Retail among the categories;
    the default's data is the baselines' (North's two months as the 1.1.0 and M2 bench runs had them)."""
    out, db = tmp_path / "retail", duckdb.connect()
    brewline(out, "--plant", "retail")
    k = json.loads((out / "known_answers.json").read_text(), parse_float=Decimal)
    o = f"read_parquet('{out}/orders/*.parquet') o JOIN read_parquet('{out}/products/*.parquet') p ON p.product_id = o.product_id"
    m = {cat: (nov, dec, n_nov, n_dec) for cat, nov, dec, n_nov, n_dec in db.execute(f"""SELECT category,
       sum(revenue) FILTER (order_date < DATE '2025-12-01'), sum(revenue) FILTER (order_date >= DATE '2025-12-01'),
       count(*) FILTER (order_date < DATE '2025-12-01'), count(*) FILTER (order_date >= DATE '2025-12-01')
     FROM {o} WHERE order_date >= DATE '2025-11-01' GROUP BY 1""").fetchall()}
    w = k["why"]
    assert (w["month"], w["dimension"], w["member"]) == ("2025-12", "category", "Retail") and sorted(m) == ["Bakery", "Coffee", "Food", "Retail", "Tea"]
    assert w["member_revenue"] == {"2025-11": m["Retail"][0], "2025-12": m["Retail"][1]} and max(m, key=lambda c: abs(m[c][1] - m[c][0])) == "Retail"
    assert abs(m["Retail"][3] / m["Retail"][2] - 0.5) < 0.1 and all(v[3] / v[2] > 0.8 for c, v in m.items() if c != "Retail")  # only Retail halves
    assert w["share_of_change"] > Decimal("0.6") and w["change_pct"] < -8
    cafes = f"read_parquet('{out}/orders/*.parquet') o JOIN read_parquet('{out}/cafes/*.parquet') c ON c.cafe_id = o.cafe_id"
    fall = dict(db.execute(f"""SELECT region, sum(revenue) FILTER (order_date >= DATE '2025-12-01') - sum(revenue) FILTER (order_date < DATE '2025-12-01')
     FROM {cafes} WHERE order_date >= DATE '2025-11-01' GROUP BY 1""").fetchall())
    assert sorted(fall) == ["East", "North", "South", "West"] and all(0 < d / w["change"] < Decimal("0.4") for d in fall.values())  # no region drives it
    default = json.loads((runs["out"] / "known_answers.json").read_text())["why"]
    assert (default["member"], default["member_revenue"], default["revenue"]) == (
        "North", {"2025-11": 3271.28, "2025-12": 1586.08}, {"2025-11": 14095.04, "2025-12": 12043.64})


J = "pi_journey"


def test_ontology_journey(tmp_path):
    """1.2.6 item 31 (needs the engine and its worker at :8780, no LLM): Brewline from the load to the object pages, as
    the workspace owner (who sees every row) and as a Viewer of the North region (a row rule on cafés). The draft
    (`propose_ontology`, which saves nothing), the agent's refinement, the person's approval (the 409 with its preview,
    then their yes), a café's page, a Viewer's action, an alert per café for the people its data names, a yes/no
    prediction on the customer's page (Brewline has no churn column: the loyalty flag stands in), one merge of two
    customer records and one plan. What the Viewer gets is made of North cafés, and of customers who visited one; a
    customer's prediction, made from all of that customer's orders, stays hidden from them (a follow link opens no
    derived table)."""
    import secrets
    import time

    from conftest import drop
    from test_engine import engine
    from test_object_flows import TICK
    from test_ontology_nobreak import brew
    from test_pages import sure

    import abi

    def call(name: str, /, **a):
        return abi.call(name, project=J, **a)

    sql = lambda q: call("sql_query", sql=q)["rows"]  # noqa: E731
    row = lambda r: dict(zip(r["columns"], r["rows"][0]))  # noqa: E731

    def north(name: str, /, **a) -> dict:
        with abi.as_user(tok):
            return call(name, **a)

    def refused(status: int, name: str, /, mine: bool = True, **a) -> dict:
        with pytest.raises(abi.ActionError) as e:
            (north if mine else call)(name, **a)
        assert e.value.status == status, e.value.body
        return e.value.body

    def ended(model: str, kind: str, run: str) -> None:
        end = time.monotonic() + 900
        while not ((r := next(m for m in call("ml_models")["models"] if m["id"] == model)[kind]) and r["run"] == run and r["status"] in ("ok", "failed")):
            assert time.monotonic() < end, r
            time.sleep(1)
        assert r["status"] == "ok", r["error"]

    def plan(token: str | None) -> dict:
        with abi.as_user(token):
            run, end = call("run_optimizer", optimizer="seats")["run"], time.monotonic() + 600
            while (r := call("optimizers", optimizer="seats")).get("run") != run or r["status"] in ("queued", "running"):
                assert time.monotonic() < end, r
                time.sleep(1)
        return r

    # ---- the load, and a person who sees the North region only
    brew(J, tmp_path)
    name, pw = f"pi_journey_north_{time.strftime('%H%M%S')}", secrets.token_urlsafe(16)
    mail = f"north@journey-{secrets.token_hex(3)}.example.test"
    uid = abi.call("create_user", name=name, password=pw)["id"]
    try:
        abi.call("set_user_email", name=name, email=mail)
        abi.call("set_role", project=J, user=name, role="viewer")
        tok = abi.login(name, pw)[1]["token"]
        sure("set_rls_rule", project=J, id="region", table="cafes", column="region", users={str(uid): ["North"]})
        n1, n2 = (x[0] for x in sql("SELECT cafe_id FROM cafes WHERE region = 'North' ORDER BY cafe_id LIMIT 2"))
        s1 = sql("SELECT min(cafe_id) FROM cafes WHERE region = 'South'")[0][0]
        names = dict(sql("SELECT cafe_id, cafe_name FROM cafes"))
        north_n = sql("SELECT count(*) FROM cafes WHERE region = 'North'")[0][0]
        sure("create_input_table", project=J, table="cafe_contacts", key=["cafe_id"], columns=[
            {"name": "cafe_id", "type": "VARCHAR"}, {"name": "manager", "type": "VARCHAR"}, {"name": "alarm", "type": "INTEGER"},
            {"name": "visit_reason", "type": "VARCHAR"}])
        call("add_relationship", **{"from": "cafe_contacts.cafe_id", "to": "cafes.cafe_id"})
        call("edit_cells", table="cafe_contacts", add=[{"cafe_id": c, "manager": mail, "alarm": 0} for c in (n1, s1)])
        call("set_measure", id="alarm", name="Alarm", expression="SUM(cafe_contacts.alarm)")

        # ---- the draft: nothing is saved; the Viewer's draft is made of what they see
        onto_file = abi.ABI_PROJECTS / J / "model/ontology.json"
        draft = call("propose_ontology")["ontology"]
        types = {x["table"]: x for x in draft["objects"]}
        assert {"cafes", "customers", "orders", "products"} <= set(types) and not onto_file.exists()
        assert [(ln["from"], ln["to"]) for ln in draft["links"] if ln.get("rls") == "follow" and ln["from"] == types["customers"]["id"]] == [
            (types["customers"]["id"], types["cafes"]["id"])], "a rule on cafés: customers are proposed to follow it"
        assert north("propose_ontology")["ontology"]["objects"] and not onto_file.exists()

        # ---- the refinement (the agent's part), then the person's approval
        cafe, customer = types["cafes"], types["customers"]
        cafe |= {"name": "Café", "plural": "Cafés", "description": "One Brewline shop.", "measures": ["revenue", "orders_rows", "alarm"],
                 "properties": [p for p in cafe.get("properties", []) if p["column"] != "cafe_contacts.manager"]
                 + [{"column": "cafe_contacts.manager", "name": "Manager", "meaning": "email"}]}
        customer |= {"name": "Customer", "plural": "Customers", "measures": ["revenue", "orders_rows"], "resolve": {"exact": [{"column": "segment"}]}}
        draft["actions"] = [{"id": "flag_visit", "object": cafe["id"], "name": "Flag for a visit", "role": "viewer", "confirm": True,
                             "inputs": {"type": "object", "required": ["reason"], "additionalProperties": False,
                                        "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 500}}},
                             "writes": {"table": "cafe_contacts", "set": {"visit_reason": "{{ data.inputs.reason }}"}}}]
        assert "needs the editor role" in refused(403, "save_ontology", ontology=draft)["message"]
        ask = refused(409, "save_ontology", mine=False, ontology=draft)
        assert ask["confirm_token"] and ask["preview"] and not onto_file.exists(), "asked first: who sees less, and what Viewers may run"
        abi.call("save_ontology", confirm=ask["confirm_token"], project=J, ontology=draft)
        assert onto_file.exists() and {x["id"] for x in call("ontology")["objects"]} >= {cafe["id"], customer["id"]}
        ct, cu = cafe["id"], customer["id"]

        # ---- a café's page, as both
        mine, theirs = call("get_object", type=ct, key=[n1]), north("get_object", type=ct, key=[n1])
        p = row(mine)
        assert (mine["title"], p["region"], p["cafe_contacts.manager"]) == (names[n1], "North", mail)
        assert p["revenue"] == pytest.approx(float(sql(f"SELECT sum(revenue) FROM orders WHERE cafe_id = '{n1}'")[0][0]))
        assert theirs["title"] == mine["title"] and row(theirs)["revenue"] == p["revenue"], "a North café: the same page"
        assert call("get_object", type=ct, key=[s1])["title"] == names[s1]
        assert refused(404, "get_object", type=ct, key=[s1]).keys() == refused(404, "get_object", type=ct, key=["CF-NONE"]).keys()
        seen = north("list_objects", type=ct, limit=1000)
        assert len(seen["rows"]) == north_n and {dict(zip(seen["columns"], x))["region"] for x in seen["rows"]} == {"North"}
        assert len(call("list_objects", type=ct, limit=1000)["rows"]) == len(names)
        visited = {x[0] for x in sql("SELECT DISTINCT o.customer_id FROM orders o JOIN cafes c USING (cafe_id) WHERE c.region = 'North'"
                                     " AND o.customer_id IS NOT NULL")}
        far = sql("SELECT min(customer_id) FROM orders WHERE customer_id IS NOT NULL AND customer_id NOT IN (SELECT o.customer_id FROM orders o"
                  " JOIN cafes c USING (cafe_id) WHERE c.region = 'North' AND o.customer_id IS NOT NULL)")[0][0]
        got = north("list_objects", type=cu, limit=50)
        assert got["rows"] and {dict(zip(got["columns"], x))["customer_id"] for x in got["rows"]} <= visited, "customers follow the cafés they visited"
        assert call("get_object", type=cu, key=[far])["key"] == [far]
        refused(404, "get_object", type=cu, key=[far])

        # ---- a Viewer's action: on a café they see, after their yes; never on one they don't
        ask = refused(409, "run_object_action", type=ct, key=[n1], action="flag_visit", inputs={"reason": "Check the grinder"})
        assert ask["preview"]["writes"]["changes"] == [{"column": "visit_reason", "old": None, "new": "Check the grinder"}]
        with abi.as_user(tok):
            done = abi.call("run_object_action", confirm=ask["confirm_token"], project=J, type=ct, key=[n1], action="flag_visit",
                            inputs={"reason": "Check the grinder"})
        assert done["write"]["status"] == "active" and sql(f"SELECT visit_reason FROM cafe_contacts WHERE cafe_id = '{n1}'") == [["Check the grinder"]]
        refused(404, "run_object_action", type=ct, key=[s1], action="flag_visit", inputs={"reason": "x"})
        assert sure("run_object_action", project=J, type=ct, key=[s1], action="flag_visit", inputs={"reason": "Owner's visit"})["write"]["status"] == "active"
        who = {x[5] for x in call("edit_history", table="cafe_contacts", key=[n1])["rows"]}
        assert name in who, "the edit log names the Viewer"

        # ---- an alert per café, for the person its data names: sent only for a café that person sees
        sure("save_flow", project=J, flow={"id": "cafe_alarm", "name": "Café alarm", "enabled": False, "trigger": {
            "type": "data_condition", "query": {"measures": ["alarm"]}, "measure": "alarm", "op": ">", "value": 0, "per": ct,
            "cooldown_minutes": 60, "recipients": {"property": "cafe_contacts.manager"}},
            "steps": [{"id": "post", "type": "action", "action": "post_message", "input": {
                "channel": "inbox", "text": "Alarm at {{ data.trigger.title }}", "data": {"measures": ["alarm"], "dimensions": ["cafes.cafe_name"]}}}]})
        for raised in (False, True):
            if raised:
                call("edit_cells", table="cafe_contacts", edits=[{"key": [c], "column": "alarm", "value": 1} for c in (n1, s1)])
            r = engine(TICK, timeout=600, stdin=json.dumps({"project": J, "flows": ["cafe_alarm"], "state": "cafe_alarm", "ahead": 0}))
            assert r.returncode == 0, r.stderr[-2000:]
        end = time.monotonic() + 300
        while len(runs := call("flow_runs", flow="cafe_alarm", limit=50)["runs"]) < 2 or any(x["status"] in ("queued", "running") for x in runs):
            assert time.monotonic() < end, runs
            time.sleep(1)
        by = {x["object"]["key"][0]: x for x in runs}
        to = lambda x: next(s for s in x["steps"] if s["step"] == "_recipients")["output"]  # noqa: E731
        assert set(by) == {n1, s1} and by[n1]["status"] == "ok" and to(by[n1]) == {"to": [mail], "skipped": []}, runs
        assert by[s1]["status"] == "stopped" and to(by[s1])["to"] == [] and "can't see this" in to(by[s1])["skipped"][0]["why"], by[s1]
        posts = [m for m in north("inbox")["messages"] if m["text"].startswith("Alarm at ")]
        assert [m["text"] for m in posts] == [f"Alarm at {names[n1]}"] and names[s1] not in json.dumps(posts, ensure_ascii=False)

        # ---- a yes/no prediction per customer, on the customer's page
        call("save_dataset", id="customer_features", name="Customer features", query={
            "measures": ["revenue", "orders_rows"], "dimensions": ["customers.customer_id", "customers.segment", "customers.loyalty_member"]})
        call("save_ml_model", ml={"id": "churn", "name": "Churn risk", "task": "classification", "dataset": "customer_features", "object": cu,
                                  "target": "customers.loyalty_member", "features": ["revenue", "orders_rows", "customers.segment"]})
        ended("churn", "latest_run", call("train_model", model="churn")["run"])
        ended("churn", "latest_score", call("score_model", model="churn")["run"])
        [prop] = [p for x in call("ontology")["objects"] if x["id"] == cu for p in x["properties"] if p["column"] == "ml_churn.probability"]
        assert (prop["name"], prop["meaning"]) == ("Churn risk", "probability")
        top = sql("SELECT customers_customer_id, probability FROM ml_churn ORDER BY probability DESC, 1 LIMIT 200")
        near, chance = next((k, v) for k, v in top if k in visited)
        assert row(call("get_object", type=cu, key=[near]))["ml_churn.probability"] == pytest.approx(chance)
        # the Viewer sees customers through the cafés they visited; a prediction made from every order stays hidden from them
        assert row(north("get_object", type=cu, key=[near]))["ml_churn.probability"] == "[hidden by row-level security]"
        assert row(call("get_object", type=cu, key=[far])).get("ml_churn.probability") is not None
        refused(404, "get_object", type=cu, key=[far])

        # ---- one merge: two records of customers who bought in the North and elsewhere become one customer, for both
        a, b = (x[0] for x in sql("SELECT o.customer_id FROM orders o JOIN cafes c USING (cafe_id) WHERE o.customer_id IS NOT NULL GROUP BY 1"
                                  " HAVING count(*) FILTER (WHERE c.region = 'North') > 0 AND count(*) FILTER (WHERE c.region <> 'North') > 0"
                                  " ORDER BY 1 LIMIT 2"))
        both = {"measures": ["revenue"], "dimensions": ["customers.customer_id"], "filters": [{"column": "customers.customer_id", "op": "in", "value": [a, b]}]}
        was, was_north = dict(call("semantic_query", **both)["rows"]), dict(north("semantic_query", **both)["rows"])
        assert set(was) == set(was_north) == {a, b} and sum(was_north.values()) < sum(was.values()), "the Viewer's part is the North's"
        assert "needs the editor role" in refused(403, "decide_same", type=cu, keys=[a, b], decision="same")["message"]
        one = str(sure("decide_same", project=J, type=cu, keys=[a, b], decision="same")["id"])
        assert one in (a, b)
        now, now_north = dict(call("semantic_query", **both)["rows"]), dict(north("semantic_query", **both)["rows"])
        assert now == {one: pytest.approx(sum(was.values()))} and now_north == {one: pytest.approx(sum(was_north.values()))}
        gone = b if one == a else a
        assert call("get_object", type=cu, key=[gone])["key"] == [one] and north("get_object", type=cu, key=[gone])["key"] == [one]

        # ---- one plan: seats to the cafés that earn most, within a total. The owner's is proposed; the Viewer's is of the North
        call("save_optimizer", optimizer={"id": "seats", "name": "Seats per café", "over": {"type": ct},
                                          "decide": {"name": "Seats", "write": "seats", "max": 100, "integer": True},
                                          "objective": {"goal": "maximize", "name": "Revenue", "per_unit": {"measure": "revenue"}},
                                          "limits": [{"name": "Seats in total", "max": 1000}]})
        seats = sql("SELECT cafe_id, seats FROM cafes ORDER BY 1")
        full, part = plan(None), plan(tok)
        objects = lambda r: {x[1]: x[2] for x in r["rows"] if x[5] is not None}  # noqa: E731
        best = lambda where: sql(f"SELECT c.cafe_name FROM orders o JOIN cafes c USING (cafe_id) {where} GROUP BY 1 ORDER BY sum(o.revenue) DESC LIMIT 10")  # noqa: E731
        assert full["outcome"] == "best" and len(objects(full)) == len(names) and full["proposal"]["changes"] > 0
        assert {k for k, v in objects(full).items() if v} == {x[0] for x in best("")} and set(objects(full).values()) == {0, 100}
        assert part["outcome"] == "best" and "proposal" not in part and len(objects(part)) == north_n
        assert {k for k, v in objects(part).items() if v} == {x[0] for x in best("WHERE c.region = 'North'")}
        assert names[s1] not in objects(part) and "nothing is changed" in part["text"]
        assert sql("SELECT cafe_id, seats FROM cafes ORDER BY 1") == seats, "a plan changes nothing"
        assert full["proposal"]["batch"] in {p["batch"] for p in call("edit_proposals")["proposals"]}
    finally:
        try:
            sure("delete_user", name=name)
        finally:
            drop([J])
