#!/usr/bin/env python3
"""Brewline Coffee, a made-up café chain: seeded bench data anyone can make again (no data file from outside the repo).
Parquet like the 1.0 release run's: 120 cafés in 12 cities across four regions, 60,000 customers, 12 products, and
orders for 2023-2025 as one Parquet file per month. --scale 1 is about 7.4M orders (the release run), 0.01 about 74k.

The same seed and scale give byte-identical files with the same DuckDB: every random value is a hash of (seed, row,
field) and every file is written in a fixed order by one thread.

Planted for the "why" bench: in December 2025, the latest complete month, the North region's orders halve while the
rest of the chain dips a little for Christmas. --plant retail plants another change instead: every café's Retail
orders (beans and cups) halve that month, so every region falls, none with most of the change: it sits in a product
category.
known_answers.json beside the data (revenue by region per year to the cent, the latest complete month, the member
that moved most, its share of the change and its orders in both months) comes from a fresh DuckDB query over the
written files, not from the generator's own tables.

--supply (1.2.7) adds a supply process, from a café's purchase order to the paid invoice: 8 suppliers, purchase
orders with the times they were placed, approved and (a few) cancelled, deliveries (one delivery carries several
orders of its supplier), and supply_events with supply_event_orders saying which orders an event is about. --scale 1
is about 1.05M purchase orders and 5.1M events. Nothing is written at or after 1 January 2026, so the latest orders
are still open. Planted, and in known_answers.json under "supply" from a fresh query: one supplier is slow between
"Order approved" and "Goods received", some invoices come twice (rework on "Invoice received"), three deviations
(paid before the goods came, never approved, delivered after it was cancelled) and the happy path's share.

    .venv/bin/python bench/brewline.py OUT [--scale 0.01] [--seed 42] [--csv] [--plant north|retail] [--supply]

    OUT/cafes/cafes.parquet, customers/customers.parquet, products/products.parquet, orders/orders_2023_01.parquet ...
    OUT/known_answers.json
    OUT/brewline.csv   (--csv) every order with its café, region and product, the product note quoted over several
                       lines with "" inside, as exports from real tools come
    OUT/suppliers/, purchase_orders/, deliveries/, supply_events/, supply_event_orders/   (--supply) one Parquet file each
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
PLANTS = {  # the "why": (dimension, member, month, factor) the member's orders that month are multiplied by
    "north": ("region", "North", "2025-12", 0.5),  # the default: North's orders halve in December 2025
    "retail": ("category", "Retail", "2025-12", 0.5),  # every café's Retail orders halve in December 2025
}
MEMBERS = {"region": sorted({r for _, r in CITIES}), "category": sorted({p[2] for p in PRODUCTS})}  # a plant's dimension
PURCHASE_ORDERS = 1_050_000  # --supply at --scale 1: about 5.1M events
SUPPLIERS = [  # id, name, what it supplies, country
    ("S01", "Nordbohne Rösterei", "Coffee beans", "Germany"), ("S02", "Molkerei Wiesengrund", "Milk", "Germany"),
    ("S03", "Backhaus Lindner", "Bakery", "Germany"), ("S04", "Haferquell", "Oat drinks", "Austria"),
    ("S05", "Van Dijk Thee", "Tea", "Netherlands"), ("S06", "Becherwerk", "Cups and packaging", "Germany"),
    ("S07", "Sirop Lumière", "Syrups", "France"), ("S08", "Frischekiste", "Fruit and vegetables", "Germany"),
]
SLOW = "S04"  # planted: its deliveries take 9 to 20 days after the approval, the others' 2 to 6
HAPPY = ["Order placed", "Order approved", "Goods received", "Invoice received", "Invoice paid"]
SUPPLY = ("suppliers", "purchase_orders", "deliveries", "supply_events", "supply_event_orders")


def q(path) -> str:
    return str(path).replace("'", "''")


def generate(out, scale: float = 0.01, seed: int = 42, csv: bool = False, plant: str = "north", supply: bool = False) -> dict:
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
    # orders per café and day: size x weekday x season x holidays x growth x the café's day x a café's plant, scaled
    # to ORDERS * scale in all; rounding up with a hash's probability keeps the expected count exact at any scale
    dim, member, month, factor = PLANTS[plant]
    cafe = dim != "category"  # a café's plant scales its café-days; a product's keeps each of its orders by chance
    plant_w = f"CASE WHEN c.{dim} = '{member}' AND strftime(d.day, '%Y-%m') = '{month}' THEN {factor} ELSE 1 END" if cafe else "1"
    keep = "" if cafe else f" WHERE {dim} != '{member}' OR u(12, cafe_id, order_date, j) < {factor}"
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
       * ({plant_w}) AS w
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
         JOIN products USING (product_id){keep if ym == month else ''}
         ORDER BY ordered_at, cafe_id, j
        ) TO '{q(out)}/orders/orders_{ym.replace('-', '_')}.parquet' (FORMAT parquet)""")
    if csv:
        db.execute(f"""COPY (SELECT o.order_id, o.order_date, o.cafe_id, c.city, c.region, o.customer_id, o.product_id,
          p.product_name, p.category, p.note AS product_note, o.quantity, o.unit_price, o.discount, o.revenue
          FROM read_parquet('{q(out)}/orders/*.parquet') o JOIN cafes c USING (cafe_id) JOIN products p USING (product_id)
          ORDER BY o.order_id) TO '{q(out)}/brewline.csv' (HEADER)""")
    if supply:
        _supply(db, out, scale)
    db.close()
    known = {"seed": int(seed), "scale": float(scale), **answers(out, dim)}
    if (known["latest_complete_month"], known["why"]["member"]) != (month, member):
        raise SystemExit(f"the planted change did not show at seed {seed}, scale {scale}")
    if supply and known["supply"]["slow"]["supplier"] != SLOW:
        raise SystemExit(f"the planted slow supplier did not show at seed {seed}, scale {scale}")
    (out / "known_answers.json").write_text(json.dumps(known, indent=1) + "\n")
    return known


def _supply(db, out: Path, scale: float) -> None:
    """--supply: the five tables of the supply process, after the others (their bytes stay as they were). Every time is
    a hash of (seed, row, field), in whole minutes; a step comes at least an hour after the one before."""
    for t in SUPPLY:
        (out / t).mkdir(parents=True, exist_ok=True)
    db.execute("CREATE TABLE suppliers(supplier_id VARCHAR, supplier_name VARCHAR, supplies VARCHAR, country VARCHAR)")
    db.executemany("INSERT INTO suppliers VALUES (?, ?, ?, ?)", SUPPLIERS)
    # a purchase order: placed, approved 30 minutes to 2 days later, ready to ship then. `plant` picks the planted
    # deviation: below 0.010 paid before the goods come, to 0.016 never approved, to 0.020 cancelled and delivered anyway.
    # A delivery (`trip`) carries the orders of one supplier that became ready one after another: 2.5 on average
    db.execute(f"""CREATE TABLE po AS
     WITH a AS (SELECT i, printf('S%02d', 1 + floor(u(20, i, 1, 0) * {len(SUPPLIERS)})::INT) AS supplier_id,
        printf('CF%03d', 1 + floor(u(20, i, 2, 0) * 120)::INT) AS cafe_id,
        TIMESTAMP '2023-01-02 06:00:00' + to_minutes(floor(u(20, i, 3, 0) * 1560000)::BIGINT) AS placed_at,
        round(40 + u(20, i, 4, 0) * 960, 2)::DECIMAL(10,2) AS amount, u(20, i, 5, 0) AS plant
       FROM range({round(PURCHASE_ORDERS * float(scale))}) r(i)),
     b AS (SELECT *, CASE WHEN plant >= 0.010 AND plant < 0.016 THEN NULL
        ELSE placed_at + to_minutes(30 + floor(u(21, i, 1, 0) * 2850)::BIGINT) END AS approved_at FROM a),
     c AS (SELECT *, coalesce(approved_at, placed_at) AS ready_at, CASE WHEN plant >= 0.016 AND plant < 0.020
        THEN approved_at + to_minutes(60 + floor(u(21, i, 2, 0) * 1380)::BIGINT) END AS cancelled_at FROM b)
     SELECT *, printf('PO%07d', row_number() OVER (ORDER BY placed_at, i)) AS po_id,
      sum((u(22, i, 1, 0) < 0.4)::INT) OVER (PARTITION BY supplier_id ORDER BY ready_at, i) AS trip FROM c""")
    # a delivery leaves 4 to 40 hours before it arrives; unloading takes 10 to 59 minutes
    db.execute(f"""CREATE TABLE dl AS
     WITH g AS (SELECT supplier_id, trip, max(ready_at) AS ready_at, sum(amount)::DECIMAL(12,2) AS amount FROM po GROUP BY 1, 2),
     t AS (SELECT *, ready_at + to_minutes((CASE WHEN supplier_id = '{SLOW}' THEN 12960 + floor(u(23, supplier_id, trip, 1) * 15840)
        ELSE 2880 + floor(u(23, supplier_id, trip, 1) * 5760) END)::BIGINT) AS received_at FROM g)
     SELECT *, received_at - to_minutes(240 + floor(u(23, supplier_id, trip, 2) * 2160)::BIGINT) AS dispatched_at,
      received_at + to_minutes(10 + floor(u(23, supplier_id, trip, 3) * 50)::BIGINT) AS unloaded_at,
      (['Road', 'Rail', 'Courier'])[1 + floor(u(23, supplier_id, trip, 4) * 3)::INT] AS transport,
      printf('D%07d', row_number() OVER (ORDER BY received_at, supplier_id, trip)) AS delivery_id FROM t""")
    # the invoice comes 1 hour to 5 days after unloading, for 6 % a corrected one 1 to 4 days later (the rework), and is
    # paid 1 to 20 days after the last one; a prepaid order (planted) is paid within a day of its approval
    db.execute("""CREATE TABLE pt AS
     WITH a AS (SELECT p.i, p.po_id, p.amount, p.plant, p.ready_at, d.delivery_id,
        d.unloaded_at + to_minutes(60 + floor(u(24, p.i, 1, 0) * 7140)::BIGINT) AS invoice_at
       FROM po p JOIN dl d USING (supplier_id, trip)),
     b AS (SELECT *, CASE WHEN u(24, i, 2, 0) < 0.06 THEN invoice_at + to_minutes(1440 + floor(u(24, i, 3, 0) * 4320)::BIGINT) END AS invoice2_at FROM a)
     SELECT *, CASE WHEN plant < 0.010 THEN ready_at + to_minutes(60 + floor(u(24, i, 4, 0) * 1380)::BIGINT)
      ELSE coalesce(invoice2_at, invoice_at) + to_minutes(1440 + floor(u(24, i, 4, 0) * 27360)::BIGINT) END AS paid_at FROM b""")
    who = "(['Mara', 'Jonas', 'Elif', 'Tomasz', 'Aylin', 'Ben'])[1 + floor(u(25, kind, started_at, n) * 6)::INT]"
    db.execute(f"""CREATE TABLE ev AS
     SELECT printf('E%08d', row_number() OVER (ORDER BY started_at, kind, n)) AS event_id, *, {who} AS done_by FROM (
      SELECT 1 AS kind, 'Delivery dispatched' AS activity, dispatched_at AS started_at, NULL::TIMESTAMP AS ended_at, delivery_id,
       NULL AS po_id, NULL::DECIMAL(12,2) AS amount, delivery_id AS n FROM dl
      UNION ALL SELECT 2, 'Goods received', received_at, unloaded_at, delivery_id, NULL, amount, delivery_id FROM dl
      UNION ALL SELECT 3, 'Invoice received', invoice_at, NULL, NULL, po_id, amount, po_id FROM pt
      UNION ALL SELECT 4, 'Invoice received', invoice2_at, NULL, NULL, po_id, amount, po_id FROM pt WHERE invoice2_at IS NOT NULL
      UNION ALL SELECT 5, 'Invoice paid', paid_at, NULL, NULL, po_id, amount, po_id FROM pt)
     WHERE started_at < TIMESTAMP '2026-01-01'""")
    copy = lambda t, sql: db.execute(f"COPY ({sql}) TO '{q(out)}/{t}/{t}.parquet' (FORMAT parquet)")  # noqa: E731
    copy("suppliers", "SELECT * FROM suppliers ORDER BY supplier_id")
    copy("purchase_orders", "SELECT po_id, cafe_id, supplier_id, placed_at, approved_at, cancelled_at, amount FROM po ORDER BY po_id")
    copy("deliveries", "SELECT delivery_id, supplier_id, transport FROM dl WHERE dispatched_at < TIMESTAMP '2026-01-01' ORDER BY delivery_id")
    copy("supply_events", "SELECT event_id, activity, started_at, ended_at, delivery_id, done_by, amount FROM ev ORDER BY event_id")
    copy("supply_event_orders", """SELECT e.event_id, coalesce(e.po_id, p.po_id) AS po_id FROM ev e LEFT JOIN pt p
      ON e.kind = 2 AND p.delivery_id = e.delivery_id WHERE e.kind > 1 ORDER BY 1, 2""")


def _supply_answers(out) -> dict:
    """The planted answers of --supply, from the written files: each purchase order's steps in order (its own three
    times and the events about it), then what they say."""
    db = duckdb.connect()
    for t in SUPPLY:
        db.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{q(out)}/{t}/*.parquet')")
    s, po, _, ev, eo = SUPPLY
    db.execute(f"""CREATE TABLE log AS SELECT po_id, 'Order placed' AS activity, placed_at AS t, 0 AS o FROM {po}
     UNION ALL SELECT po_id, 'Order approved', approved_at, 1 FROM {po} WHERE approved_at IS NOT NULL
     UNION ALL SELECT po_id, 'Order cancelled', cancelled_at, 2 FROM {po} WHERE cancelled_at IS NOT NULL
     UNION ALL SELECT b.po_id, e.activity, e.started_at, 3 FROM {ev} e JOIN {eo} b USING (event_id)""")
    db.execute("""CREATE TABLE trace AS SELECT po_id, list(activity ORDER BY t, o) AS steps,
      min(t) FILTER (activity = 'Order approved') AS approved, min(t) FILTER (activity = 'Order cancelled') AS cancelled,
      min(t) FILTER (activity = 'Goods received') AS received, min(t) FILTER (activity = 'Invoice paid') AS paid,
      count(*) FILTER (activity = 'Invoice received') AS invoices FROM log GROUP BY 1""")
    one = lambda sql: db.execute(sql).fetchone()  # noqa: E731
    rows = {t: one(f"SELECT count(*) FROM {t}")[0] for t in SUPPLY}
    stamps = one(f"SELECT count(placed_at) + count(approved_at) + count(cancelled_at) FROM {po}")[0]
    happy, rework, prepaid, unapproved, cancelled = one(f"""SELECT count(*) FILTER (steps = {HAPPY}), count(*) FILTER (invoices > 1),
      count(*) FILTER (paid < received), count(*) FILTER (approved IS NULL AND received IS NOT NULL),
      count(*) FILTER (cancelled < received) FROM trace""")
    lead = db.execute(f"""SELECT supplier_id, any_value(supplier_name), median(date_diff('minute', approved, received)) / 1440 AS days
     FROM trace JOIN {po} USING (po_id) JOIN {s} USING (supplier_id) WHERE received IS NOT NULL GROUP BY 1 ORDER BY days DESC, 1""").fetchall()
    several, most = one(f"""SELECT count(*) FILTER (n > 1), max(n) FROM (SELECT count(*) AS n FROM {eo} JOIN {ev} USING (event_id)
     WHERE activity = 'Goods received' GROUP BY event_id)""")
    return {"rows": rows, "events": stamps + rows["supply_events"],
            "slow": {"supplier": lead[0][0], "name": lead[0][1], "from": "Order approved", "to": "Goods received",
                     "median_days": round(lead[0][2], 2), "next_slowest_median_days": round(lead[1][2], 2)},
            "rework": {"activity": "Invoice received", "orders": rework, "share": round(rework / rows["purchase_orders"], 4)},
            "deviations": {"paid_before_goods": prepaid, "not_approved": unapproved, "delivered_after_cancel": cancelled},
            "happy_path": {"steps": HAPPY, "orders": happy, "share": round(happy / rows["purchase_orders"], 4)},
            "deliveries_with_several_orders": several, "most_orders_in_one_delivery": most}


def answers(out, dim: str) -> dict:
    """What the written files say, from a fresh DuckDB (never the generator's tables): rows, revenue by region per year
    to the cent, the latest complete month and, against the month before, the planted dimension's member that moved
    most with its share of the change."""
    db = duckdb.connect()
    o, c, p = (f"read_parquet('{q(out)}/{t}/*.parquet')" for t in ("orders", "cafes", "products"))
    rows = {t: db.execute(f"SELECT count(*) FROM read_parquet('{q(out)}/{t}/*.parquet')").fetchone()[0]
            for t in ("cafes", "customers", "products", "orders")}
    by = {}
    for region, year, revenue in db.execute(f"SELECT region, year(order_date), sum(revenue) FROM {o} JOIN {c} USING (cafe_id) "
                                            "GROUP BY ALL ORDER BY ALL").fetchall():
        by.setdefault(region, {})[str(year)] = float(revenue)
    month, prev = db.execute(f"""SELECT strftime(m, '%Y-%m'), strftime(m - INTERVAL 1 MONTH, '%Y-%m')
     FROM (SELECT date_trunc('month', max(order_date) + 1) - INTERVAL 1 MONTH AS m FROM {o})""").fetchone()
    ch = db.execute(f"""SELECT {dim}, sum(revenue) FILTER (strftime(order_date, '%Y-%m') = '{prev}') AS a,
       sum(revenue) FILTER (strftime(order_date, '%Y-%m') = '{month}') AS b,
       count(*) FILTER (strftime(order_date, '%Y-%m') = '{prev}'), count(*) FILTER (strftime(order_date, '%Y-%m') = '{month}')
     FROM {o} JOIN {c} USING (cafe_id) JOIN {p} USING (product_id) WHERE strftime(order_date, '%Y-%m') IN ('{prev}', '{month}')
     GROUP BY ALL ORDER BY abs(b - a) DESC, {dim}""").fetchall()
    a, b = sum(r[1] for r in ch), sum(r[2] for r in ch)
    member, ma, mb, na, nb = ch[0]
    return ({"supply": _supply_answers(out)} if (Path(out) / "supply_events").is_dir() else {}) | {
            "rows": rows, "revenue_by_region_year": by, "latest_complete_month": month,
            "why": {"measure": "revenue", "month": month, "previous_month": prev, "revenue": {prev: float(a), month: float(b)},
                    "change": float(b - a), "change_pct": float(round((b - a) / a * 100, 2)), "dimension": dim, "member": member,
                    "member_revenue": {prev: float(ma), month: float(mb)}, "member_change": float(mb - ma),
                    "share_of_change": float(round((mb - ma) / (b - a), 4)), "member_orders": {prev: na, month: nb}}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--scale", type=float, default=0.01, help="1 is about 7.4M orders (default 0.01, at least 0.005)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--csv", action="store_true", help="also write brewline.csv")
    ap.add_argument("--plant", choices=PLANTS, default="north", help="the why's planted change (default north)")
    ap.add_argument("--supply", action="store_true", help="also write the supply process's tables (purchase orders to paid invoices)")
    a = ap.parse_args()
    t0 = time.time()
    k = generate(a.out, a.scale, a.seed, a.csv, a.plant, a.supply)
    w = k["why"]
    print(f"{k['rows']['orders']:,} orders in {time.time() - t0:.1f} s; {w['month']} revenue {w['change_pct']:+}% on "
          f"{w['previous_month']}, {w['member']} {w['share_of_change']:.0%} of the change")
