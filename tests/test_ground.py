"""Grounding check (copied ABI rules): an invented number or member is caught and masked; backed ones pass."""
import functools
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import ground

ROOT = Path(__file__).resolve().parent.parent

ROWS = [{"columns": ["category", "views"], "rows": [["Music", 1234567], ["Gaming", 98765]]}]


def test_invented_number_is_caught():
    bad = ground.check("Music has 1,234,567 views and Gaming 120,000.", ROWS, spans=True)
    assert bad["numbers"] == ["120,000"]
    assert "[not verified]" in ground.mask("Music has 1,234,567 views and Gaming 120,000.", bad)


def test_backed_numbers_rounding_and_question_pass():
    assert ground.check("Music leads with 1.2 million views; Gaming has 98,765. Top 2 shown.", ROWS,
                        asked="top 2 categories") == {"numbers": [], "names": []}


def test_invented_member_is_caught():
    assert ground.check("The top category is Atlantis with 98,765 views.", ROWS)["names"] == ["Atlantis"]


def test_cli_skips_answers_without_numbers():
    run = lambda req: json.loads(subprocess.run([sys.executable, str(Path(ground.__file__))], input=json.dumps(req),
                                                capture_output=True, text=True, check=True).stdout)
    assert run({"answer": "Atlantis is great.", "results": []}) == {"numbers": [], "names": []}
    out = run({"answer": "We have 42 videos.", "results": []})
    assert out["numbers"] == ["42"] and "42" not in out["masked"]


def test_clock_minutes_are_no_figure():
    """'every Monday at 8' became 'at 08:[not verified]': the minutes of a time were checked as a figure (REPORT #43)."""
    assert ground.check("The flow runs every Monday at 08:00 UTC.", ROWS, asked="every Monday at 8") == {"numbers": [], "names": []}
    assert ground.check("The flow runs every Monday at 09:00 UTC.", ROWS, asked="every Monday at 8")["numbers"] == ["09"]



def test_a_link_label_opens_like_a_sentence():
    """'[Download your dashboard PDF](…)' came back as '[[not verified] your dashboard PDF]'."""
    assert ground.check("[Download your dashboard PDF](/api/artifacts/p/x.pdf)", ROWS)["names"] == []
    assert ground.check("See [Atlantis Monthly](/x) with 98,765 views.", ROWS)["names"] == ["Atlantis Monthly"]  # mid-sentence: still a name


def test_the_other_side_of_a_yes_no_column_is_no_invented_name():
    """Release run 30 Oct: "Nonmembers" for `loyalty_member` = false was masked, and with it the plain word in prose."""
    res = [{"columns": ["loyalty_member", "average_ticket"], "rows": [[True, 5.6544], [False, 5.6574]]}]
    ans = "Members and nonmembers.\n\n| Loyalty status | Average ticket |\n|---|---:|\n| Members | 5.6544 |\n| Nonmembers | 5.6574 |\n| Non-members | 5.6574 |"
    assert ground.check(ans, res, fields={"dim_customers.loyalty_member"})["names"] == []
    assert "Nonbuyers" in ground.check("| Loyalty status | Average ticket |\n|---|---:|\n| Nonbuyers | 5.6574 |", res, fields={"dim_customers.loyalty_member"})["names"]


def test_the_plural_of_a_name_the_person_typed_is_theirs():
    """Release run 31 Oct: asked "every Monday at 8", the answer's "Mondays at 08:00" came back "[not verified] at 08:00"."""
    asked = "email me a weekly summary every Monday at 8"
    assert ground.check("Runs on **Mondays at 08:00 UTC**.", [], asked=asked)["names"] == []
    assert ground.check("Runs on **Tuesdays at 08:00 UTC**.", [], asked=asked)["names"] == ["Tuesdays"]


def test_a_status_word_opening_a_label_value_is_no_name():
    """Live run 31 Oct: "- **Status:** Saved **disabled**" came back "[not verified] **disabled**"."""
    for w in ("Saved", "Created", "Currently", "Kept", "Paused"):
        assert ground.check(f"- **Status:** {w} **disabled**; no email sent.", [])["names"] == [], w
    assert ground.check("- **Top region:** Narnia, by revenue.", [])["names"] == ["Narnia"], "a data name after a label is still checked"
    assert ground.check("- **Tier:** Platinum", [])["names"] == ["Platinum"], "data-like values stay checked"


def test_a_figure_in_a_definitions_title_grounds_as_written():
    """1.1 #6: answers about the reports themselves came back half-masked: "28-day" from a visual title is the
    project's own words. A figure of the data, an ID's digits or a rounded figure still need a result row."""
    rep = {"reports": [{"id": "daily_revenue_28d", "title": "Revenue over the last 28 days", "pages": [{"layout": {"w": 12}}]}]}
    check = ground.check
    assert check("The page shows revenue over the last 28 days.", [rep])["numbers"] == []
    assert check("Revenue was 17,327,168.70 over 12 weeks.", [rep])["numbers"] == ["17,327,168.70", "12"]
    assert check("It covers 30 days.", [rep])["numbers"] == ["30"]


# ---------- M2.2: grounding on Brewline (bench/brewline.py, seed 42, the bench's scale) ----------
# The rows are ABI's own answers in the 1.1 why and ask runs (DECIMALs as text), plus the change and share rows ABI's
# governed *_change measures and inline expressions return for them (M2.1, M2.3: the same figures to the cent).


def q(columns, rows, filters=(), measures=None):
    """A semantic_query result as ABI returns it."""
    names = measures or {c: c.replace("_", " ").capitalize() for c in columns if "." not in c and c != "grouping_id"}
    return {"columns": columns, "measures": names, "rows": rows, "filters": list(filters), "live": [], "time_zone": "UTC", "currency": None}


Y25 = [{"column": "calendar.year", "op": "=", "value": 2025}, {"column": "calendar.month", "op": "in", "value": [11, 12]}]
DEC = [{"column": "calendar.year", "op": "=", "value": 2025}, {"column": "calendar.month", "op": "=", "value": 12}]
NORTH = [{"column": "cafes.region", "op": "=", "value": "North"}]
CHANGES = {"revenue": "Revenue", "revenue_prev": "Revenue, previous period", "revenue_change": "Revenue change",
           "revenue_change_pct": "Revenue % change"}
BREW = {
    "ask": q(["cafes.region", "grouping_id", "revenue"], [["East", 0, "40774.45"], ["North", 0, "36360.52"], ["South", 0, "39285.46"],
             ["West", 0, "41718.55"], [None, 1, "158138.98"]],
             [{"column": "calendar.day", "op": ">=", "value": "2025-01-01"}, {"column": "calendar.day", "op": "<=", "value": "2025-12-31"}]),
    "months": q(["calendar.month", "revenue", "orders_rows", "quantity"], [[11, "14095.04", 2444, 2887], [12, "12043.64", 2050, 2447]], Y25),
    "latest": q(["calendar.year", "calendar.month", "revenue", "orders_rows"], [[2025, 12, "12043.64", 2050], [2025, 11, "14095.04", 2444],
                [2025, 10, "13461.00", 2361], [2025, 9, "13309.95", 2251]]),
    "years": q(["calendar.year", "calendar.month", "revenue", "orders_rows"], [[2023, m, *v] for m, v in enumerate([
        ("9580.98", 1652), ("9264.35", 1565), ("9753.85", 1711), ("9655.30", 1632), ("9540.78", 1613), ("9188.87", 1616),
        ("9625.63", 1628), ("10139.72", 1712), ("10136.48", 1705), ("10651.45", 1878), ("10598.55", 1866), ("11268.79", 1904)], 1)]
        + [[2024, m, *v] for m, v in enumerate([("11972.37", 2086), ("11881.08", 2110), ("12865.31", 2245), ("12381.20", 2140),
                                                  ("12720.38", 2183), ("11488.19", 2041), ("11798.03", 2071), ("12467.28", 2132)], 1)]
        + ["... 16 more omitted"]),
    "regions": q(["cafes.region", "calendar.month", "revenue", "orders_rows"], [
        ["East", 11, "3673.46", 640], ["East", 12, "3586.63", 594], ["North", 11, "3271.28", 576], ["North", 12, "1586.08", 280],
        ["South", 11, "3542.79", 606], ["South", 12, "3447.20", 576], ["West", 11, "3607.51", 622], ["West", 12, "3423.73", 600]], Y25),
    "category": q(["products.category", "calendar.month", "revenue", "orders_rows"], [
        ["Bakery", 11, "1470.24", 433], ["Bakery", 12, "1302.68", 375], ["Coffee", 11, "5204.28", 1254], ["Coffee", 12, "4167.02", 1001],
        ["Food", 11, "1286.12", 138], ["Food", 12, "1177.89", 122], ["Retail", 11, "4719.60", 310], ["Retail", 12, "4057.90", 272],
        ["Tea", 11, "1414.80", 309], ["Tea", 12, "1338.15", 280]], Y25),
    "format": q(["cafes.format", "calendar.month", "revenue", "orders_rows"], [
        ["campus", 11, "2052.50", 355], ["campus", 12, "1728.69", 301], ["mall", 11, "3424.79", 591], ["mall", 12, "2640.21", 475],
        ["station", 11, "5594.06", 984], ["station", 12, "5113.68", 863], ["street", 11, "3023.69", 514], ["street", 12, "2561.06", 411]], Y25),
    "cafes": q(["cafes.cafe_name", "calendar.month", "revenue", "orders_rows"], [
        ["Bremen Altstadt", 11, "55.09", 14], ["Bremen Altstadt", 12, "9.70", 3], ["Bremen Campus", 11, "73.01", 13],
        ["Bremen Campus", 12, "44.76", 6], ["Bremen Hafen", 11, "55.90", 7], ["Bremen Hafen", 12, "18.50", 5],
        ["Bremen Hbf", 11, "99.34", 18], ["Bremen Hbf", 12, "28.26", 9], ["Bremen Markt", 11, "60.28", 14], ["Bremen Markt", 12, "53.44", 7],
        ["Bremen Mitte", 11, "76.52", 9], ["Bremen Mitte", 12, "51.90", 11], ["Bremen Nord", 11, "73.87", 16], ["Bremen Nord", 12, "18.30", 5],
        ["Bremen Ost", 11, "171.22", 22], ["Bremen Ost", 12, "53.32", 11], ["Bremen Süd", 11, "182.48", 32], ["Bremen Süd", 12, "76.00", 11],
        ["Bremen West", 11, "71.30", 7], ["Bremen West", 12, "8.10", 2], "... 40 more omitted"], Y25 + NORTH),
    "cities": q(["cafes.city", "calendar.month", "revenue", "orders_rows"], [
        ["Bremen", 11, "919.01", 152], ["Bremen", 12, "362.28", 70], ["Hamburg", 11, "1138.17", 200], ["Hamburg", 12, "543.35", 98],
        ["Hanover", 11, "1214.10", 224], ["Hanover", 12, "680.45", 112]], Y25 + NORTH),
    "days": q(["calendar.day", "revenue", "orders_rows"], [["2025-12-31", "299.68", 49], ["2025-12-30", "432.71", 78], ["2025-12-29", "399.13", 77]]),
    "north_days": q(["calendar.day", "revenue", "orders_rows"], [["2025-12-01", "45.70", 10], ["2025-12-02", "70.99", 14]], DEC + NORTH),
    "north": q(["calendar.month", "revenue"], [[12, "1586.08"]], DEC + NORTH),
    "change": q(["calendar.year", "calendar.month", "cafes.region", "revenue", "revenue_prev", "revenue_change", "revenue_change_pct"], [
        [2025, 12, "North", "1586.08", "3271.28", "-1685.20", -0.5151500330145997],
        [2025, 12, "West", "3423.73", "3607.51", "-183.78", -0.050943725727718006],
        [2025, 12, "South", "3447.20", "3542.79", "-95.59", -0.026981559731172326],
        [2025, 12, "East", "3586.63", "3673.46", "-86.83", -0.023637115961518566]], DEC, CHANGES),
    "total": q(["calendar.year", "calendar.month", "revenue", "change", "change_pct"], [[2025, 12, "12043.64", "-2051.40", -0.14554055894839604]],
               DEC, {"revenue": "Revenue", "change": "[revenue] - PREVIOUS_PERIOD([revenue])",
                     "change_pct": "DIVIDE([revenue] - PREVIOUS_PERIOD([revenue]), ABS(PREVIOUS_PERIOD([revenue])))"}),
    "share": q(["calendar.year", "calendar.month", "cafes.region", "change", "share"], [
        [2025, 12, "North", "-1685.20", 0.8214877644535439], [2025, 12, "West", "-183.78", 0.089587598713074]], DEC,
        {"change": "[revenue] - PREVIOUS_PERIOD([revenue])", "share": "DIVIDE([revenue] - PREVIOUS_PERIOD([revenue]),"
         " IGNORE([revenue], cafes.region) - PREVIOUS_PERIOD(IGNORE([revenue], cafes.region)))"}),
    "products": q(["orders.product_id", "revenue"], [["P02", "2391.85"], ["P05", "1065.40"]], DEC),
    "cafe_ids": q(["orders.cafe_id", "revenue"], [["CF009", "51.90"]], DEC),
}
MODEL = {  # semantic_model of the bench's project, as the harness's digest reads it
    "tables": {"cafes": {"kind": "dimension", "key": ["cafe_id"]}, "calendar": {"kind": "dimension", "key": ["day"]},
               "customers": {"kind": "dimension", "key": ["customer_id"]}, "orders": {"kind": "fact", "key": ["order_id"]},
               "products": {"kind": "dimension", "key": ["product_id"]}},
    "relationships": [{"from": {"table": "orders", "columns": [c]}, "to": {"table": t, "columns": [k]}}
                      for c, t, k in [("order_date", "calendar", "day"), ("cafe_id", "cafes", "cafe_id"),
                                      ("customer_id", "customers", "customer_id"), ("product_id", "products", "product_id")]],
    "date_table": {"table": "calendar", "column": "day"},
    "columns": {"cafes": {"cafe_id": "VARCHAR", "cafe_name": "VARCHAR", "city": "VARCHAR", "region": "VARCHAR", "opened_on": "DATE",
                          "seats": "INTEGER", "format": "VARCHAR"},
                "calendar": {"day": "DATE", "year": "BIGINT", "quarter": "BIGINT", "month": "BIGINT", "week": "BIGINT", "week_year": "BIGINT"},
                "customers": {"customer_id": "VARCHAR", "segment": "VARCHAR", "home_country": "VARCHAR", "signup_date": "DATE",
                              "loyalty_member": "BOOLEAN"},
                "orders": {"order_id": "VARCHAR", "order_date": "DATE", "cafe_id": "VARCHAR", "customer_id": "VARCHAR",
                           "product_id": "VARCHAR", "quantity": "INTEGER", "revenue": "DECIMAL(10,2)"},
                "products": {"product_id": "VARCHAR", "product_name": "VARCHAR", "category": "VARCHAR", "list_price": "DECIMAL(6,2)",
                             "unit_cost": "DECIMAL(6,2)", "note": "VARCHAR"}},
    "measures": [{"id": i} for i in ("orders_rows", "quantity", "revenue", *list(CHANGES)[1:])], "datasets": []}
ASK = "In project brew: what was total revenue by region in 2025? Give the figures."
WHY = "In project brew: why did revenue change in the latest complete month of data compared with the month before?"
AREAS = json.loads((ROOT / "harness/areas.json").read_text())
# the vocabulary .pi/extensions/abi/index.ts passes: tool IDs and the app's own words (its pages, sections, labels)
APP = re.search(r"const PAGES = (\[.*?\]);", (ROOT / ".pi/extensions/abi/index.ts").read_text())[1]
VOCAB = [*AREAS["core"], *(t for a in AREAS["areas"].values() for t in a["tools"]), "ABI", "Pi", "GPT", *json.loads(APP),
         *json.loads("[" + re.search(r"const APP = \[\.\.\.PAGES, (.*?)\];", (ROOT / ".pi/extensions/abi/index.ts").read_text(), re.S)[1] + "]")]


@functools.cache
def brew_db():
    """The bench project's dimension tables and calendar (bench/brewline.py's cafés, products), behind a fake sql_query."""
    import duckdb
    spec = importlib.util.spec_from_file_location("brewline", ROOT / "bench/brewline.py")
    spec.loader.exec_module(b := importlib.util.module_from_spec(spec))
    con = duckdb.connect()
    con.execute("CREATE TABLE cafes(cafe_id VARCHAR, cafe_name VARCHAR, city VARCHAR, region VARCHAR, opened_on DATE, seats INTEGER, format VARCHAR)")
    suffix = ["Mitte", "Hbf", "Altstadt", "Nord", "Süd", "Ost", "West", "Campus", "Hafen", "Markt"]
    con.executemany("INSERT INTO cafes VALUES (?, ?, ?, ?, DATE '2020-01-01', 40, ?)", [
        (f"CF{i + 1:03d}", f"{b.CITIES[i % 12][0]} {suffix[i // 12]}", *b.CITIES[i % 12], ["street", "station", "mall", "campus"][i % 4])
        for i in range(120)])
    con.execute("CREATE TABLE products(product_id VARCHAR, product_name VARCHAR, category VARCHAR, list_price DECIMAL(6,2), "
                "unit_cost DECIMAL(6,2), note VARCHAR)")
    con.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", b.PRODUCTS)
    con.execute("CREATE TABLE customers AS SELECT 'C000001' AS customer_id, 'Student' AS segment, 'Germany' AS home_country,"
                " DATE '2020-01-01' AS signup_date, true AS loyalty_member")
    con.execute("CREATE TABLE calendar AS SELECT d::DATE AS day, year(d) AS year, quarter(d) AS quarter, month(d) AS month,"
                " week(d) AS week, isoyear(d) AS week_year FROM generate_series(DATE '2023-01-01', DATE '2025-12-31', INTERVAL 1 DAY) g(d)")
    return con


def read(project, sql, params=(), types=()):
    """sql_query as ABI answers it: the parameters cast as it casts them (701 DOUBLE, 1082 DATE, else text), JSON rows."""
    cast = {701: float, 1082: date.fromisoformat}
    rows = brew_db().cursor().execute(sql, [cast.get(t, str)(v) for v, t in zip(params, [*types, *[0] * len(params)])]).fetchall()
    return json.loads(json.dumps([list(r) for r in rows], default=lambda x: float(x) if isinstance(x, Decimal) else str(x)))


def run(answer, *keys, asked=WHY, results=None):
    return ground.run({"answer": answer, "results": results or [BREW[k] for k in keys], "notes": [], "pending": [], "asked": "\n" + asked,
                       "vocab": VOCAB, "today": "2026-11-04", "project": "brew", "model": MODEL})


GOOD = [  # (answer, the results it rests on): written as the bench's answers are, rounded and labelled as people write
    ("Total revenue in 2025 was 158,138.98. By region:\n\n| Region | Revenue |\n|---|---|\n| West | 41,718.55 |\n| East | 40,774.45 |\n"
     "| South | 39,285.46 |\n| North | 36,360.52 |\n| **Total** | **158,138.98** |\n\nThese figures cover calendar days from 2025-01-01 to"
     " 2025-12-31 and come from the model's `revenue` measure.", ["ask"]),
    ("I filtered calendar.day to 2025-01-01 through 2025-12-31: West 41,718.55, East 40,774.45, South 39,285.46, North 36,360.52.", ["ask"]),
    ("Revenue in 2025 came to about 158.1 thousand: West 41.7k, East 40.8k, South 39.3k and North 36.4k.", ["ask"]),
    ("2025 revenue was 0.16 million; West led at 41,719 and North trailed at 36,361.", ["ask"]),
    ("The query counted orders dated on or after 2025-01-01 and on or before 2025-12-31, so 158,138.98 is the whole of 2025.", ["ask"]),
    ("Through December 31, 2025, revenue totalled 158,138.98.", ["ask"]),
    ("€158,139 in 2025: West €41,718.55, East €40,774.45.", ["ask"]),
    ("West had the most revenue in 2025 (41,718.55), North the least (36,360.52).", ["ask"]),
    ("Revenue fell in December 2025, the latest complete month, to 12,043.64 from 14,095.04 in November.", ["months"]),
    ("Orders fell from 2,444 to 2,050 and quantity from 2,887 to 2,447.", ["months"]),
    ("**North drove most of the drop**: 3,271.28 in November, 1,586.08 in December.", ["regions"]),
    ("North's revenue roughly halved, from 3,271.28 to 1,586.08, while East (3,673.46 to 3,586.63), South (3,542.79 to 3,447.20) and"
     " West (3,607.51 to 3,423.73) slipped only slightly.", ["regions"]),
    ("North fell 51.5%, from 3,271.28 to 1,586.08.", ["change"]),
    ("North changed by −51.5% (−1,685.20).", ["change"]),
    ("North had a 52% fall, the steepest of any region.", ["change"]),
    ("North was down 1,685.20 on November.", ["change", "regions"]),
    ("Revenue fell 14.6% (2,051.40) to 12,043.64.", ["total"]),
    ("Revenue fell 14.55%, from 14,095.04 to 12,043.64.", ["total", "months"]),
    ("North accounts for 82% of the drop.", ["share"]),
    ("North's share of the fall was 82.1%; West's 9.0%.", ["share"]),
    ("East slipped 2.4%, South 2.7% and West 5.1%.", ["change"]),
    ("| Region | Nov | Dec | Change | % change |\n|---|---:|---:|---:|---:|\n| North | 3,271.28 | 1,586.08 | −1,685.20 | −51.5% |\n"
     "| West | 3,607.51 | 3,423.73 | −183.78 | −5.1% |\n| South | 3,542.79 | 3,447.20 | −95.59 | −2.7% |\n"
     "| East | 3,673.46 | 3,586.63 | −86.83 | −2.4% |", ["change", "regions"]),
    ("North −51.52%, West −5.09%, South −2.70%, East −2.36%.", ["change"]),
    ("The drop is concentrated in North: −1,685.20 of the −2,051.40 total.", ["change", "total"]),
    ("December revenue: 12,043.64 (−14.6% on November's 14,095.04).", ["total", "months"]),
    ("Revenue fell 2.05k to 12.04k.", ["total"]),
    ("The ranking by change puts North first (−1,685.20), then West (−183.78).", ["change"]),
    ("Bremen Süd went from 182.48 to 76.00 and Bremen Ost from 171.22 to 53.32.", ["cafes"]),
    ("The North-region cafés all fell: Bremen Hbf went from 99.34 to 28.26.", ["cafes"]),
    ("I only saw 20 of the 60 North café rows, so the rest are unchecked.", ["cafes"]),
    ("Data ends on 2025-12-31, so I treated December as complete.", ["days", "months"]),
    ("The last day with orders, 2025-12-31, had 49 orders and 299.68 in revenue.", ["days"]),
    ("Coffee fell from 5,204.28 to 4,167.02 and Retail from 4,719.60 to 4,057.90; every category declined.", ["category"]),
    ("Tea (1,414.80 to 1,338.15), Bakery (1,470.24 to 1,302.68) and Food (1,286.12 to 1,177.89) dipped less.", ["category"]),
    ("By format, station cafés fell from 5,594.06 to 5,113.68 and mall cafés from 3,424.79 to 2,640.21.", ["format"]),
    ("Hamburg went from 1,138.17 to 543.35, Hanover from 1,214.10 to 680.45 and Bremen from 919.01 to 362.28.", ["cities"]),
    ("Revenue fell from 14,095.04 to 12,043.64. I can save this breakdown as an Explore analysis or set up a Monitor check on North.",
     ["months", "regions"]),
    ("Revenue fell from 14.1k to 12.0k. You can keep the chart in My analyses.", ["months"]),
    ("Revenue fell from 14,095.04 to 12,043.64.\n- **Volume:** Total orders went from 2,444 to 2,050.\n"
     "- **Overall:** Overall quantity fell from 2,887 to 2,447.", ["months"]),
    ("December 2025's 12,043.64 is above other Decembers: 11,268.79 in 2023.", ["years", "months"]),
    ("I saw 20 of the 36 months in the history; 2023 ended at 11,268.79.", ["years"]),
    ("North's December started at 45.70 on 2025-12-01 and 70.99 on 2025-12-02.", ["north_days"]),
    ("North's orders roughly halved, from 576 to 280.", ["regions"]),
    ("Revenue was 12.04 thousand in December against 14.1 thousand in November.", ["months"]),
    ("The latest complete month is December 2025 (12,043.64); November had 14,095.04.", ["latest"]),
    ("October 2025 had 13,461.00 and September 13,309.95.", ["latest"]),
    ("North took 1,586.08 in December.", ["north"]),
    ("From 1 November to 31 December 2025 North lost 1,685.20.", ["change", "regions"]),
    ("Flat White (P02) sold 2,391.85 in December; Cold Brew (P05) 1,065.40.", ["products"]),
    ("CF009 (Bremen Mitte, North) took 51.90 in December.", ["cafe_ids"]),
    ("In 2025, North's revenue was 36,360.52 of 158,138.98.", ["ask"]),
    ("North fell by half in December (1,586.08 against 3,271.28).", ["regions"]),
    ("Orders fell from 2.4k to 2.1k.", ["months"]),
    ("Revenue dropped from 14.1 thousand to 12 thousand.", ["months"]),
    ("As fractions, North's change was −0.515 and West's −0.051.", ["change"]),
    ("Revenue was €0.16M in 2025.", ["ask"]),
    ("North fell to 1,586.08; check whether North's December orders are complete.", ["regions"]),  # a possessive ends a name
    ("That is a change of −2,051.40 (−14.6%, per ABI's Revenue % change).", ["total", "change"]),
    ("I can build a North-by-café drill-down report from North's 1,586.08.", ["regions"]),
    ("- **Timing in North:** Weekly North revenue was 1,586.08 in December.", ["regions"]),  # a common word opening a name
    ("- **North cafés:** Several fell sharply: Bremen Süd went from 182.48 to 76.00.", ["cafes"]),
    ("**What I can't tell from the data:** Why North fell to 1,586.08.", ["regions"]),  # interrogatives open a clause
    ("In November North took 3,271.28 and in December 1,586.08.", ["regions"]),  # a month opening a member's name
    ("| Café | December revenue |\n|---|---:|\n| Bremen Süd | 76.00 |\n| Bremen Ost | 53.32 |", ["cafes"]),  # cafes.cafe_name
    ("December took 12,043.64; 2025-12-31 was the last day, after the Christmas and New Year closures.", ["months", "days"]),
]


def test_brewline_answers_written_as_people_write_them_are_never_masked(monkeypatch):
    """M2.2's known-good set: rounding (k, thousand, million, 0-2 decimals), percentages from ABI's change and share
    rows (with or without a minus), members (non-ASCII, hyphenated, lowercase, the query's own filter), code labels,
    the query's own date range and its inclusive end, the cap's counts and the app's own words: no false mask."""
    monkeypatch.setattr(ground, "_sql", read)
    assert len(GOOD) >= 50
    bad = {a: (out["numbers"], out["names"]) for a, keys in GOOD if (out := run(a, *keys, asked=ASK if keys == ["ask"] else WHY))["numbers"] or out["names"]}
    assert not bad, bad


INVENTED = [  # (answer, the figure or name no row backs, the results it was written beside)
    ("Revenue fell 15.2% in December.", "15.2%", ["months"]),
    ("Revenue fell by 2,051.40 to 12,043.64.", "2,051.40", ["months", "regions"]),  # its own subtraction
    ("North accounts for 82% of the drop.", "82%", ["regions"]),  # its own share
    ("North fell 52%.", "52%", ["regions", "cafes"]),  # 1.1 grounded it through Bremen Mitte's 51.90
    ("East fell 2%.", "2%", ["regions", "cafes"]),  # ... and these through order counts of 2, 3 and 5
    ("South fell 3%.", "3%", ["regions", "cafes"]),
    ("West fell 5%.", "5%", ["regions", "cafes"]),
    ("Revenue fell 14.6%.", "14.6%", ["months", "regions"]),  # the M2.1 live runs' own total change
    ("Revenue was 13,250.00 in December.", "13,250.00", ["months"]),
    ("North took 1,600.50 in December.", "1,600.50", ["regions"]),
    ("Revenue rose to 15.3k.", "15.3k", ["months"]),
    ("The average order was 5.87 in December.", "5.87", ["months"]),  # 12,043.64 / 2,050
    ("Orders fell by 394.", "394", ["months"]),  # 2,444 - 2,050
    ("North fell 48.5%.", "48.5%", ["change"]),  # beside the real -51.5%
    ("North fell −52.5%.", "−52.5%", ["change"]),
    ("West's change was −183.87.", "−183.87", ["change"]),
    ("Bremen Mitte fell 32.2%.", "32.2%", ["cafes"]),  # (51.90 - 76.52) / 76.52
    ("Revenue reached 158,200.00 in 2025.", "158,200.00", ["ask"]),
    ("Total revenue was 160k.", "160k", ["ask"]),
    ("The last three months total 39,600.61.", "39,600.61", ["latest"]),  # a sum
    ("North's share was 0.83.", "0.83", ["share"]),
    ("Bremen Zentrum fell from 99.34 to 28.26.", "Zentrum", ["cafes"]),  # no such café
    ("Revenue was 12,043.64. Hamburg Hbf fell to 28.26.", "Hamburg Hbf", ["months", "cafes"]),  # a café, not in these rows
    ("Station cafés fell to 5,113.68.", "station", ["regions"]),  # a member (format) at a sentence start: the members check
    ("In December, North's Atlantis café took 76.00.", "Atlantis", ["cafes"]),  # a possessive splits it, the invented part stays flagged
    ("North fell to 1,586.08 and the Atlantis-by-sea cafés to 9.70.", "Atlantis-by-sea", ["regions", "cafes"]),
    ("- **Timing:** Weekly Atlantis revenue was 1,586.08.", "Weekly Atlantis", ["regions"]),
    ("In November Atlantis took 3,271.28.", "November Atlantis", ["regions"]),
]


def test_invented_figures_and_names_are_always_masked(monkeypatch):
    """M2.2's seeded set: figures no row holds, the model's own arithmetic (differences, shares, % changes, sums,
    averages), a percentage that only shares digits with a row value, and members no row of these results backs."""
    monkeypatch.setattr(ground, "_sql", read)
    for answer, fig, keys in INVENTED:
        out = run(answer, *keys, asked=ASK if keys == ["ask"] else WHY)
        assert fig in out["numbers"] + out["names"], (answer, out)
        assert fig not in out["masked"] and "[not verified]" in out["masked"], out["masked"]


def test_seeded_invented_figures_are_masked_100_percent(monkeypatch):
    """200 figures drawn with a fixed seed, kept only where no row value of the why run's results is within one unit
    (or, for a percentage, any ratio x 100 within one point), as plain numbers, thousands, k and percentages: all masked."""
    monkeypatch.setattr(ground, "_sql", read)
    keys = ["months", "latest", "regions", "category", "format", "cafes", "cities", "days", "change", "total", "share"]
    have = []
    ground._walk([BREW[k]["rows"] for k in keys], have, [])
    rng, made = random.Random(22), []
    while len(made) < 200:
        x = rng.uniform(1, 20000)
        shape = rng.choice(["{:,.2f}", "{:.1f}%", "{:,.0f}", "{:.1f}k"])
        v = x / 1000 if shape.endswith("k") else x / 200 if shape.endswith("%") else x
        text = shape.format(v)
        value = float(text.rstrip("%k").replace(",", "")) * (1000 if shape.endswith("k") else 1)
        near = [h * 100 for h in have] + have if shape.endswith("%") else have
        if all(abs(abs(h) - value) > (1 if not shape.endswith("k") else 100) for h in near):
            made.append(text)
    for fig in made:
        out = run(f"Revenue in that cut was {fig} after the drop.", *keys)
        assert fig in out["numbers"], (fig, out)


def test_a_percentage_grounds_only_from_a_ratio_or_a_percent_column():
    """1.1 grounded "52%" through a revenue of 51.90 (40-46 of 99 made-up whole percentages grounded on a why run's
    rows); a % change of -0.515 grounded "-51.5%" but not "fell 51.5%". Now: ratio x 100 with any sign written without a
    minus, a percent column's own value, the person's own words; never a plain value with the same digits."""
    pct = q(["cafes.region", "revenue_change_pct", "discount_pct"], [["North", -0.5151500330145997, 12.5]], measures={
        "revenue_change_pct": "Revenue % change", "discount_pct": "Discount %"})
    assert ground.check("North fell 51.5%.", [pct])["numbers"] == []
    assert ground.check("North changed −51.5%, −52% rounded.", [pct])["numbers"] == []
    assert ground.check("North rose 51.5%... no: changed +51.5%.", [pct])["numbers"] == []  # the sign is checked only with a minus
    assert ground.check("North changed −48.5%, or −0.5%.", [pct])["numbers"] == ["−48.5%", "−0.5%"]  # a fraction's value is no %
    assert ground.check("Discounts were 12.5% of list price.", [pct])["numbers"] == []  # a percent column as it is
    big = q(["cafes.region", "growth_pct"], [["North", 1.5]])
    assert ground.check("North grew 150%.", [big])["numbers"] == []  # above 100%: a percent column's
    plain = q(["cafes.cafe_name", "revenue", "orders_rows"], [["Bremen Mitte", "51.90", 2], ["Bremen West", "0.42", 3]])
    assert ground.check("North fell 52%; West 2%, 3%.", [plain])["numbers"] == ["52%", "2%", "3%"]
    assert ground.check("Bremen West's 0.42 is 42% of a euro.", [plain])["numbers"] == []  # a ratio in a plain column
    assert ground.check("Above 5%, alert me.", [plain], asked="alert me when it falls more than 5%")["numbers"] == []


def test_the_query_s_own_filters_dates_and_code_labels_ground_through_abi(monkeypatch):
    """REPORT.md: the harness copy dropped ABI's filtered and members; 1.1 masked the ask task's own end date in 19 of
    20 runs. Read through ABI as the person (sql_query): a filter value found in its column grounds, an invented one
    doesn't; the first and last day a query's date filters keep ground ("2025-01-01 to 2025-12-31"); a code's label in
    its dimension row grounds ("Flat White" for P02); a member no row of these results backs is flagged even at a
    sentence start or in lowercase; a read ABI refuses (RLS, a hidden column) grounds nothing."""
    calls = []
    monkeypatch.setattr(ground, "_sql", lambda *a: calls.append(a) or read(*a))
    atlantis = q(["calendar.month", "revenue"], [[12, None]], DEC + [{"column": "cafes.region", "op": "=", "value": "Atlantis"}])
    assert run("North took 1,586.08 in December.", "north")["names"] == []
    assert run("North took 1,586.08 and Atlantis nothing.", results=[atlantis, BREW["north"]])["names"] == ["Atlantis"]
    out = ground.filtered("brew", MODEL, [BREW["ask"], BREW["north"], BREW["regions"]], read)
    assert ["2025-01-01", "2025-12-31"] in [r["rows"][0] for r in out if isinstance(r, dict) and r["columns"] == ["calendar.day"] * 2]
    assert ["2025-11-01", "2025-12-31"] in [r["rows"][0] for r in out if isinstance(r, dict) and r["columns"] == ["calendar.day"] * 2]
    assert "North" in out and {"columns": ["calendar.year"], "rows": [[2025]]} in out
    assert {"Flat White", "Coffee", "Cold Brew"} <= set(ground.labels("brew", MODEL, [BREW["products"]], read))
    assert run("Flat White (P02) sold 2,391.85.", "products")["names"] == []
    assert run("P02 is the Latte: 2,391.85.", "products")["names"] == ["Latte"]  # a label P02's row doesn't have
    members, fields = ground.scope("brew", MODEL, read)
    assert {"Bremen Süd", "North", "station", "Flat White", "P02", "CF009"} <= members and {"cafe_name", "revenue_change_pct"} <= fields
    assert run("Revenue was 12,043.64. station cafés did worst.", "months")["names"] == ["station"]  # lowercase member
    assert run("Revenue was 12,043.64. station cafés did worst.", "months", "format")["names"] == []
    assert calls and {c[0] for c in calls} == {"brew"}  # every read went to ABI (`_sql`: sql_query as the person)
    monkeypatch.setattr(ground, "_sql", lambda *a: [])  # ABI refused every read
    assert run("These cover 2025-01-01 to 2025-12-31.", "ask", asked=ASK)["numbers"] == ["12", "31"]


def test_the_retry_names_what_failed_and_lists_the_rows_to_rewrite_from(monkeypatch):
    """The retry said "answer again from the start": it now names the masked figures and lists the rows of this
    exchange and the one before (newest first) that the answer may quote; the person never sees it."""
    monkeypatch.setattr(ground, "_sql", read)
    out = run("Revenue fell 14.6% to 12,043.64; North fell from 3,271.28 to 1,586.08, most of it in Atlantis.", "months", "regions")
    assert out["numbers"] == ["14.6%"] and out["names"] == ["Atlantis"]
    r = out["retry"]
    assert "14.6%, Atlantis" in r and "Rewrite the answer" in r and "[not verified]" in r
    assert '["cafes.region", "calendar.month", "revenue", "orders_rows"]' in r and '["North", 12, "1586.08", 280]' in r
    assert r.index("cafes.region") < r.index('"quantity"')  # the newest result first
    assert out["final"] == ground.mask(out["masked"], {"spans": []}) and "14.6%" not in out["final"]
    long = q(["cafes.cafe_name", "revenue"], [[f"Café {i}", f"{i}.00"] for i in range(2000)])
    assert len(ground.retry({"numbers": ["1"], "names": []}, [long])) < ground.ROWS + 2000


# .pi/extensions/abi/index.ts as Pi runs it (a stand-in Pi, as test_app's EXT), against a fake ABI on a local port: the
# person's messages, ABI's tool results and the model's answers go in; what Pi would show and send back comes out
DRIVE = r"""
import { registerHooks } from "node:module";
registerHooks({ resolve: (s, c, next) => s.startsWith("@earendil-works/")
  ? { url: "data:text/javascript,export const Type = new Proxy({}, { get: () => () => ({}) });", shortCircuit: true } : next(s, c) });
globalThis.__dirname = process.argv[1];
const on = {}, pi = new Proxy({}, { get: (_, k) => k === "on" ? (e, f) => (on[e] ??= []).push(f) : () => [] });
await (await import(process.argv[2])).default(pi);
const ctx = { hasUI: false, cwd: process.cwd(), ui: { setStatus() {}, notify() {} }, sessionManager: { getBranch: () => [] } };
const fire = async (e, ev) => { let r; for (const f of on[e] ?? []) r = (await f(ev, ctx)) ?? r; return r; };
await fire("session_start", {});
const out = [];
let n = 0;
for (const x of JSON.parse(process.argv[3])) {
  const sections = {};
  await fire("before_agent_start", { messageType: "user", prompt: x.prompt, systemPromptOptions: { sections } });
  for (const result of x.results) {
    const ev = { toolCallId: `t${n++}`, toolName: "mcp__abi__semantic_query", input: { project: "brew" } };
    await fire("tool_call", ev);
    await fire("tool_result", { ...ev, isError: false, content: [{ type: "text", text: JSON.stringify({ result }) }] });
  }
  const shown = [];
  for (const a of x.answers) {
    const m = await fire("message_end", { message: { role: "assistant", content: [{ type: "text", text: a }] } });
    const s = await fire("agent_before_settle", {});
    shown.push({ text: m ? m.message.content.at(-1).text : null, retry: s ? s.entries[0].content : null });
  }
  out.push({ context: sections.abi_context, shown });
}
console.log(JSON.stringify(out));
"""


class FakeABI(BaseHTTPRequestHandler):
    """The REST actions the harness and ground.py call, for project brew; sql_query reads brew_db() as ABI would."""
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            self.reply(200, {"list_projects": lambda: {"projects": [{"project": "brew"}]}, "semantic_model": lambda: MODEL,
                             "project_locale": lambda: {"time_zone": "UTC", "currency": None},
                             "sql_query": lambda: {"rows": read(body["project"], body["sql"], body.get("params", []), body.get("types", []))},
                             }[self.path.rsplit("/", 1)[-1]]())
        except Exception as e:  # noqa: BLE001 (any other action, or SQL DuckDB refuses: ABI's 400)
            self.reply(400, {"detail": {"message": str(e)}})

    def do_GET(self):
        self.reply(200, [])  # /api/memory: no project memory

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_the_harness_grounds_an_answer_on_this_exchange_and_the_one_before():
    """index.ts kept every tool result of the chat as a grounding source (state.results was never reset), so a figure
    from long ago still grounded; its digest listed calendar.day only (the filter tested the table's name), so the
    model grouped by month without a year. Driven as Pi runs it: the ask task's own end date grounds through ABI; a
    draft's own % change is masked and the hidden retry names it and lists the rows; the previous exchange's figures
    ground; one from three exchanges back is masked, and stays masked when the retry repeats it."""
    if not shutil.which("node"):
        pytest.skip("no node")
    chat = [
        {"prompt": ASK, "results": [BREW["ask"]], "answers": [
            "Total revenue in 2025 was 158,138.98: West 41,718.55, East 40,774.45, South 39,285.46 and North 36,360.52."
            " These figures cover calendar days from 2025-01-01 to 2025-12-31."]},
        {"prompt": WHY, "results": [BREW["months"], BREW["regions"]], "answers": [
            "Revenue fell 14.6% in December 2025, from 14,095.04 to 12,043.64; North drove most of it, from 3,271.28 to 1,586.08."
            " I can save this as an Explore analysis.",
            "Revenue fell from 14,095.04 to 12,043.64 in December 2025; North drove most of it, from 3,271.28 to 1,586.08."]},
        {"prompt": "In project brew: and by product category?", "results": [BREW["category"]], "answers": [
            "Coffee fell most, from 5,204.28 to 4,167.02; by region, North's 1,586.08 was the low point."]},
        {"prompt": "In project brew: sum it up for me.", "results": [], "answers": [
            "Revenue in 2025 was 158,138.98, and Coffee fell to 4,167.02 in December.",
            "Revenue in 2025 was 158,138.98, and Coffee fell to 4,167.02 in December."]},
    ]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeABI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ABI_APP_", "ABI_PI_AGENT", "ABI_PI_ALLOWED", "ABI_PI_MODE"))}
    ext = ROOT / ".pi/extensions/abi/index.ts"
    try:
        r = subprocess.run(["node", "--experimental-strip-types", "--input-type=module", "-e", DRIVE, str(ext.parent), str(ext), json.dumps(chat)],
                           cwd=ROOT, capture_output=True, text=True, timeout=120, env={
                               **env, "ABI_URL": f"http://127.0.0.1:{srv.server_address[1]}", "ABI_TOKEN": "test-only",
                               "ABI_USER": "m22-test", "ABI_PI_LLM_BASE": "http://127.0.0.1:9/v1"})
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr[-2000:]
    ask, why, category, recap = json.loads(r.stdout.splitlines()[-1])
    assert "calendar.year" in ask["context"] and "calendar.month" in ask["context"]  # the digest
    assert ask["shown"] == [{"text": None, "retry": None}]  # "to 2025-12-31": the query's own range, read through ABI
    first, second = why["shown"]
    assert "[not verified]" in first["text"] and "14.6%" not in first["text"] and "Explore" in first["text"]
    assert "14.6%" in first["retry"] and '["North", 12, "1586.08", 280]' in first["retry"] and "[not verified]" in first["retry"]
    assert second == {"text": None, "retry": None}
    assert category["shown"] == [{"text": None, "retry": None}]  # North's 1,586.08: the exchange before
    first, second = recap["shown"]
    assert "158,138.98" not in first["text"] and "4,167.02" in first["text"]  # three exchanges back, the one before
    assert "what no tool row backs: 158,138.98." in first["retry"] and '"4167.02"' in first["retry"] and "158138.98" not in first["retry"]
    assert "158,138.98" not in second["text"] and "_Grounding: 1 figure(s)" in second["text"] and second["retry"] is None
