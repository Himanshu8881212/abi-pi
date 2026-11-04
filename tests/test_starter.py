"""The starter build (scripts/abi.py load, the agent's abi_load) on this machine's engine: a project's first build also
writes governed previous-period, change and % change measures for each SUM of a fact linked to the calendar (M2.3)."""
import json
import subprocess
import sys

import duckdb
import pytest
from conftest import drop, parquet

import abi

BREW, BARE, DATED, NAMED = "pi_starter_brew", "pi_starter_bare", "pi_starter_dated", "pi_starter_named"
TIMED = ("_prev", "_change", "_change_pct")


@pytest.fixture(scope="module", autouse=True)
def _throwaway():
    """This file's projects: an interrupted run's go first, this run's after."""
    gone = lambda: drop([p for p in (BREW, BARE, DATED, NAMED) if p in {x["project"] for x in abi.call("list_projects")["projects"]}])  # noqa: E731
    gone()
    yield
    gone()


def measures(p: str) -> list[dict]:
    return abi.call("semantic_model", project=p, all=True)["measures"]


def test_brewline_gets_change_measures_equal_to_duckdb_to_the_cent(tmp_path):
    """bench/brewline.py at its smallest scale, loaded as the bench does: revenue_prev, _change and _change_pct for
    the latest complete month by region are a hand-written DuckDB query's over the files, and ordering by the change
    puts the planted region first."""
    subprocess.run([sys.executable, abi.ROOT / "bench/brewline.py", tmp_path, "--scale", "0.005"], check=True, capture_output=True)
    duckdb.sql(f"COPY (SELECT * FROM read_parquet('{tmp_path}/orders/*.parquet')) TO '{tmp_path}/orders.parquet' (FORMAT parquet)")
    made = abi.load("demo", BREW, [str(tmp_path / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")]
                    + [str(tmp_path / "orders.parquet")])
    assert made["readiness"]["ready"] and made.get("report") == "overview" and not made.get("measure_errors"), made
    ms = {m["id"]: m for m in measures(BREW)}
    assert {"revenue_prev", "revenue_change", "revenue_change_pct"} <= set(ms) and all(m.get("description") for m in ms.values())
    assert ms["revenue_change_pct"]["expression"] == "DIVIDE([revenue] - PREVIOUS_PERIOD([revenue]), ABS(PREVIOUS_PERIOD([revenue])))"
    w = json.loads((tmp_path / "known_answers.json").read_text())["why"]
    year, month = map(int, w["month"].split("-"))  # 2025-12, the latest complete month
    want = {r: v for r, *v in duckdb.sql(f"""SELECT region, now, before, now - before, (now - before) / abs(before) FROM (
        SELECT c.region, sum(o.revenue) FILTER (strftime(o.order_date, '%Y-%m') = '{w["month"]}') AS now,
               sum(o.revenue) FILTER (strftime(o.order_date, '%Y-%m') = '{w["previous_month"]}') AS before
        FROM read_parquet('{tmp_path}/orders/*.parquet') o JOIN read_parquet('{tmp_path}/cafes/*.parquet') c ON c.cafe_id = o.cafe_id
        GROUP BY 1)""").fetchall()}
    q = {"measures": ["revenue", "revenue_prev", "revenue_change", "revenue_change_pct"],
         "dimensions": ["calendar.year", "calendar.month", "cafes.region"],
         "filters": [{"column": "calendar.year", "op": "=", "value": year}, {"column": "calendar.month", "op": "=", "value": month}]}
    rows = abi.call("semantic_query", project=BREW, **q)["rows"]
    got = {r: v for _, _, r, *v in rows}
    cents = lambda d: {r: [round(float(x), 2) for x in v[:3]] for r, v in d.items()}  # noqa: E731
    assert len(rows) == 4 and cents(got) == cents(want), (rows, want)
    assert all(abs(got[r][3] - float(want[r][3])) < 1e-9 for r in want), (rows, want)
    top = abi.call("semantic_query", project=BREW, **q, order_by=[{"field": "revenue_change", "desc": False}], limit=1)["rows"]
    assert top[0][2] == w["member"] == "North", top


def test_a_project_with_no_date_gets_no_change_measures(tmp_path):
    (tmp_path / "orders.csv").write_text("order_id,region,amount\n1,North,10\n2,South,20\n3,North,5\n")
    made = abi.load("demo", BARE, parquet(tmp_path / "orders.csv"))
    ids = [m["id"] for m in measures(BARE)]
    assert made.get("report") == "overview" and "amount" in ids and not [i for i in ids if i.endswith(TIMED)], (made, ids)


def test_a_dated_build_reports_without_them_and_running_it_again_adds_nothing_unasked(tmp_path):
    """The change measures come after the count and the SUM, so the Overview's KPIs (no date to step back from) are
    those two, and every visual reads. The person's own amount_prev, a deleted % change: the build again (same file)
    keeps both as they are."""
    f = tmp_path / "orders.csv"
    f.write_text("order_id,order_date,region,amount\n" + "".join(
        f"{i},2025-{1 + i % 3:02d}-{1 + i % 28:02d},{('North', 'South')[i % 2]},{10 * i}\n" for i in range(1, 41)))
    made = abi.load("demo", DATED, parquet(f))
    assert [m["id"] for m in measures(DATED)] == ["orders_rows", "amount", "amount_prev", "amount_change", "amount_change_pct"], made
    rep = abi.call("list_reports", project=DATED, report="overview")["reports"][0]
    vis = rep["pages"][0]["visuals"]
    assert [v["id"] for v in vis if v["type"] == "kpi"] == ["kpi-orders_rows", "kpi-amount"]
    assert all(abi.call("visual_data", project=DATED, report="overview", page="overview", visual=v["id"])["rows"] for v in vis)
    abi.call("set_measure", project=DATED, id="amount_prev", name="Amount last year", expression="PREVIOUS_YEAR([amount])")
    with pytest.raises(abi.ActionError) as e:  # destructive: this test's own measure, so it answers the confirmation
        abi.call("delete_measure", project=DATED, id="amount_change_pct")
    abi.call("delete_measure", confirm=e.value.body["confirm_token"], project=DATED, id="amount_change_pct")
    before = measures(DATED)
    again = abi.load("demo", DATED, parquet(f))
    assert again["measures"] == [] and measures(DATED) == before, again


def test_a_column_named_like_a_change_measure_keeps_its_sum(tmp_path):
    """amount_prev is a column here: its SUM keeps the ID (it isn't replaced by amount's previous period), and amount's
    change still steps back a period."""
    f = tmp_path / "orders.csv"
    f.write_text("order_id,order_date,amount,amount_prev\n" + "".join(f"{i},2025-0{1 + i % 2}-1{i % 9},{i},{2 * i}\n" for i in range(1, 21)))
    abi.load("demo", NAMED, parquet(f))
    ms = {m["id"]: m["expression"] for m in measures(NAMED)}
    assert ms["amount_prev"] == "SUM(orders.amount_prev)" and ms["amount_change"] == "[amount] - PREVIOUS_PERIOD([amount])", ms
