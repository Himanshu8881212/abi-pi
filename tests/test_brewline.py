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
