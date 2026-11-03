#!/usr/bin/env python3
"""Brewline Coffee, a made-up café chain: seeded bench data anyone can make again (no data file from outside the repo).
Parquet like the 1.0 release run's: 120 cafés in 12 cities across four regions, 60,000 customers, 12 products, and
orders for 2023-2025 as one Parquet file per month. --scale 1 is about 7.4M orders (the release run), 0.01 about 74k.

The same seed and scale give byte-identical files with the same DuckDB: every random value is a hash of (seed, row,
field) and every file is written in a fixed order by one thread.

Planted for the "why" bench: in December 2025, the latest complete month, the North region's orders halve while the
rest of the chain dips a little for Christmas. known_answers.json beside the data (revenue by region per year to the
cent, the latest complete month, the member that moved most and its share of the change) comes from a fresh DuckDB
query over the written files, not from the generator's own tables.

    .venv/bin/python bench/brewline.py OUT [--scale 0.01] [--seed 42] [--csv]

    OUT/cafes/cafes.parquet, customers/customers.parquet, products/products.parquet, orders/orders_2023_01.parquet ...
    OUT/known_answers.json
    OUT/brewline.csv   (--csv) every order with its café, region and product, the product note quoted over several
                       lines with "" inside, as exports from real tools come
"""
import argparse
import json
import time
from pathlib import Path

import duckdb

ORDERS = 7_400_000  # at --scale 1
MONTHS = [f"{y}-{m:02d}" for y in (2023, 2024, 2025) for m in range(1, 13)]
CITIES = [("Berlin", "East"), ("Hamburg", "North"), ("Munich", "South"), ("Cologne", "West"), ("Frankfurt", "West"),
          ("Leipzig", "East"), ("Stuttgart", "South"), ("Dresden", "East"), ("Bremen", "North"), ("Düsseldorf", "West"),
          ("Nuremberg", "South"), ("Hanover", "North")]
PRODUCTS = [  # id, name, category, list price, unit cost, note
    ("P01", "Espresso", "Coffee", 2.40, 0.55, "Double shot, 18 g in, 36 g out."),
    ("P02", "Flat White", "Coffee", 3.60, 0.90, 'Our best seller.\nAsk for "extra hot" if you like it hotter.'),
    ("P03", "Cappuccino", "Coffee", 3.40, 0.85, None),
    ("P04", "Oat Latte", "Coffee", 3.90, 1.10, "Oat milk at no extra charge."),
    ("P05", "Cold Brew", "Coffee", 4.20, 1.00, 'Steeped 18 hours.\nServed over ice, "no sugar" unless asked.'),
    ("P06", "Chai Latte", "Tea", 3.70, 0.95, None),
    ("P07", "Matcha Latte", "Tea", 4.30, 1.40, None),
    ("P08", "Croissant", "Bakery", 2.80, 0.90, "Baked every morning.\nContains: wheat, butter, milk."),
    ("P09", "Banana Bread", "Bakery", 3.20, 0.85, None),
    ("P10", "Avocado Toast", "Food", 7.90, 2.60, None),
    ("P11", "House Beans 250g", "Retail", 11.50, 5.20, 'Whole beans, "Brewline House" blend;\nground on request.'),
    ("P12", "Reusable Cup", "Retail", 14.00, 4.50, None),
]
PLANT = ("region", "North", "2025-12", 0.5)  # the "why": North's orders halve in December 2025


def q(path) -> str:
    return str(path).replace("'", "''")


def generate(out, scale: float = 0.01, seed: int = 42, csv: bool = False) -> dict:
    if not scale >= 0.005:
        raise SystemExit("--scale must be 0.005 (about 37,000 orders) or more for the planted change to stand out")
    out = Path(out)
    for t in ("cafes", "customers", "products", "orders"):
        (out / t).mkdir(parents=True, exist_ok=True)
    db = duckdb.connect()
    db.execute("SET threads = 1")  # the bytes can't depend on the machine's cores (DuckDB doesn't promise it in parallel)
    db.execute(f"CREATE MACRO u(s, a, b, c) AS (hash({int(seed)}, s, a, b, c) >> 11)::DOUBLE / 9007199254740992")  # [0, 1)
    db.execute("CREATE TABLE city(k INT, city VARCHAR, region VARCHAR)")
    db.executemany("INSERT INTO city VALUES (?, ?, ?)", [(k, c, r) for k, (c, r) in enumerate(CITIES)])
    db.execute("CREATE TABLE products(product_id VARCHAR, product_name VARCHAR, category VARCHAR, list_price DECIMAL(6,2), "
               "unit_cost DECIMAL(6,2), note VARCHAR)")
    db.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?)", PRODUCTS)
    db.execute("""CREATE TABLE cafes AS SELECT i, printf('CF%03d', i + 1) AS cafe_id,
     city || ' ' || (['Mitte','Hbf','Altstadt','Nord','Süd','Ost','West','Campus','Hafen','Markt'])[1 + i // 12] AS cafe_name,
     city, region, DATE '2019-01-01' + floor(u(1, i, 1, 0) * 2000)::INT AS opened_on, 30 + floor(u(1, i, 2, 0) * 70)::INT AS seats,
     (['street','station','mall','campus'])[1 + floor(u(1, i, 3, 0) * 4)::INT] AS format
     FROM range(120) r(i) JOIN city ON k = i % 12 ORDER BY i""")
    db.execute("""CREATE TABLE customers AS SELECT printf('C%06d', i) AS customer_id,
     (['Student','Commuter','Remote worker','Tourist','Office'])[1 + floor(u(2, i, 1, 0) * 5)::INT] AS segment,
     (['Germany','Germany','Germany','Austria','Netherlands','France','USA'])[1 + floor(u(2, i, 2, 0) * 7)::INT] AS home_country,
     DATE '2019-01-01' + floor(u(2, i, 3, 0) * 2500)::INT AS signup_date, u(2, i, 4, 0) < 0.25 AS loyalty_member
     FROM range(1, 60001) r(i) ORDER BY i""")
    # orders per café and day: size x weekday x season x holidays x growth x the café's day x the plant, scaled to
    # ORDERS * scale in all; rounding up with a hash's probability keeps the expected count exact at any scale
    dim, member, month, factor = PLANT
    db.execute(f"""CREATE TABLE plan AS
     WITH d AS (SELECT CAST(t AS DATE) AS day, date_diff('day', DATE '2023-01-01', CAST(t AS DATE)) AS n
                FROM range(DATE '2023-01-01', DATE '2026-01-01', INTERVAL 1 DAY) r(t)),
     w AS (SELECT c.i, c.cafe_id, d.day, d.n,
       c.seats * (CASE c.format WHEN 'station' THEN 1.3 WHEN 'mall' THEN 1.1 WHEN 'campus' THEN 0.8 ELSE 1.0 END)
       * (CASE dayofweek(d.day) WHEN 0 THEN 0.75 WHEN 6 THEN 1.15 ELSE 1.0 END)
       * (1 + 0.08 * cos(2 * pi() * (dayofyear(d.day) - 30) / 365.25))
       * (CASE strftime(d.day, '%m-%d') WHEN '12-24' THEN 0.5 WHEN '12-25' THEN 0 WHEN '12-26' THEN 0 WHEN '12-31' THEN 0.6
          WHEN '01-01' THEN 0 ELSE 1 END)
       * (1 + 0.2 * d.n / 1095)
       * (0.85 + 0.3 * u(3, c.i, d.n, 0))
       * (CASE WHEN c.{dim} = '{member}' AND strftime(d.day, '%Y-%m') = '{month}' THEN {factor} ELSE 1 END) AS w
       FROM cafes c JOIN d ON d.day >= c.opened_on)
     SELECT i, cafe_id, day, n, floor({ORDERS * float(scale)} * w / sum(w) OVER () + u(4, i, n, 0))::INT AS k FROM w""")
    db.execute(f"COPY (SELECT * EXCLUDE (i) FROM cafes) TO '{q(out)}/cafes/cafes.parquet' (FORMAT parquet)")
    for t in ("customers", "products"):
        db.execute(f"COPY {t} TO '{q(out)}/{t}/{t}.parquet' (FORMAT parquet)")
    for ym in MONTHS:
        db.execute(f"""COPY (
         SELECT printf('O{ym.replace('-', '')}%07d', row_number() OVER (ORDER BY ordered_at, cafe_id, j)) AS order_id,
          order_date, ordered_at, cafe_id, customer_id, product_id, quantity, list_price AS unit_price, discount,
          round(quantity * list_price * (1 - discount), 2)::DECIMAL(10,2) AS revenue, payment
         FROM (SELECT day AS order_date, day + to_minutes(420 + floor(u(5, i, n, j) * 660)::INT) AS ordered_at, cafe_id, j,
           CASE WHEN u(6, i, n, j) < 0.6 THEN printf('C%06d', 1 + floor(u(7, i, n, j) * 60000)::INT) END AS customer_id,
           (['P01','P02','P02','P03','P03','P04','P04','P05','P06','P07','P08','P08','P09','P10','P11','P12'])
             [1 + floor(u(8, i, n, j) * 16)::INT] AS product_id,
           CASE WHEN u(9, i, n, j) < 0.85 THEN 1 WHEN u(9, i, n, j) < 0.97 THEN 2 ELSE 3 END AS quantity,
           (CASE WHEN u(10, i, n, j) < 0.1 THEN 0.10 ELSE 0 END)::DECIMAL(3,2) AS discount,
           (['card','card','card','mobile','cash'])[1 + floor(u(11, i, n, j) * 5)::INT] AS payment
           FROM plan, range(k) r(j) WHERE strftime(day, '%Y-%m') = '{ym}')
         JOIN products USING (product_id)
         ORDER BY ordered_at, cafe_id, j
        ) TO '{q(out)}/orders/orders_{ym.replace('-', '_')}.parquet' (FORMAT parquet)""")
    if csv:
        db.execute(f"""COPY (SELECT o.order_id, o.order_date, o.cafe_id, c.city, c.region, o.customer_id, o.product_id,
          p.product_name, p.category, p.note AS product_note, o.quantity, o.unit_price, o.discount, o.revenue
          FROM read_parquet('{q(out)}/orders/*.parquet') o JOIN cafes c USING (cafe_id) JOIN products p USING (product_id)
          ORDER BY o.order_id) TO '{q(out)}/brewline.csv' (HEADER)""")
    db.close()
    known = {"seed": int(seed), "scale": float(scale), **answers(out)}
    if (known["latest_complete_month"], known["why"]["member"]) != (month, member):
        raise SystemExit(f"the planted change did not show at seed {seed}, scale {scale}")
    (out / "known_answers.json").write_text(json.dumps(known, indent=1) + "\n")
    return known


def answers(out) -> dict:
    """What the written files say, from a fresh DuckDB (never the generator's tables): rows, revenue by region per year
    to the cent, the latest complete month and, against the month before, the planted dimension's member that moved
    most with its share of the change."""
    db = duckdb.connect()
    o, c = f"read_parquet('{q(out)}/orders/*.parquet')", f"read_parquet('{q(out)}/cafes/*.parquet')"
    rows = {t: db.execute(f"SELECT count(*) FROM read_parquet('{q(out)}/{t}/*.parquet')").fetchone()[0]
            for t in ("cafes", "customers", "products", "orders")}
    by = {}
    for region, year, revenue in db.execute(f"SELECT region, year(order_date), sum(revenue) FROM {o} JOIN {c} USING (cafe_id) "
                                            "GROUP BY ALL ORDER BY ALL").fetchall():
        by.setdefault(region, {})[str(year)] = float(revenue)
    month, prev = db.execute(f"""SELECT strftime(m, '%Y-%m'), strftime(m - INTERVAL 1 MONTH, '%Y-%m')
     FROM (SELECT date_trunc('month', max(order_date) + 1) - INTERVAL 1 MONTH AS m FROM {o})""").fetchone()
    dim = PLANT[0]
    ch = db.execute(f"""SELECT {dim}, sum(revenue) FILTER (strftime(order_date, '%Y-%m') = '{prev}') AS a,
       sum(revenue) FILTER (strftime(order_date, '%Y-%m') = '{month}') AS b
     FROM {o} JOIN {c} USING (cafe_id) WHERE strftime(order_date, '%Y-%m') IN ('{prev}', '{month}')
     GROUP BY ALL ORDER BY abs(b - a) DESC, {dim}""").fetchall()
    a, b = sum(r[1] for r in ch), sum(r[2] for r in ch)
    member, ma, mb = ch[0]
    return {"rows": rows, "revenue_by_region_year": by, "latest_complete_month": month,
            "why": {"measure": "revenue", "month": month, "previous_month": prev, "revenue": {prev: float(a), month: float(b)},
                    "change": float(b - a), "change_pct": float(round((b - a) / a * 100, 2)), "dimension": dim, "member": member,
                    "member_revenue": {prev: float(ma), month: float(mb)}, "member_change": float(mb - ma),
                    "share_of_change": float(round((mb - ma) / (b - a), 4))}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--scale", type=float, default=0.01, help="1 is about 7.4M orders (default 0.01, at least 0.005)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--csv", action="store_true", help="also write brewline.csv")
    a = ap.parse_args()
    t0 = time.time()
    k = generate(a.out, a.scale, a.seed, a.csv)
    w = k["why"]
    print(f"{k['rows']['orders']:,} orders in {time.time() - t0:.1f} s; {w['month']} revenue {w['change_pct']:+}% on "
          f"{w['previous_month']}, {w['member']} {w['share_of_change']:.0%} of the change")
