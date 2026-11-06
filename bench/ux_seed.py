#!/usr/bin/env python3
"""The 1.2.5 UX bench's seed: fills an empty install so that no page of the app is empty (bench/UX.md). Run INSIDE an
ABI container as its owner (ABI_USER, ABI_PASSWORD from the container's environment, as scripts/creds.py signs in):

    docker exec -i abi-ux-NAME python3 bench/ux_seed.py < old-accounts.json > accounts.json

Makes project brewline (bench/brewline.py at scale 0.01, loaded with abi.load as bench/perf.py's `project`: tables,
model, starter measures, Overview), a `showcase` report with a visual of every type, a dataset, an ML forecast
(trained), a flow on a schedule (saved switched off), a Monitor check (its alert flow on, so the Inbox fills), a vault
document, glossary terms, a saved Explore analysis, the accounts editor1 (Editor), owner2 (a project Owner no rule lists) and viewer1 (Viewer, a row-level rule:
the South region only), one commit, a published copy (Publish) and Home widgets for the owner and viewer1.

Idempotent: what exists is kept (accounts, the trained model, the loaded data); the rest is saved again as it was.
`ux_seed.py --model` instead saves the agent's model provider from stdin (bench/ux_install.sh).
The accounts' passwords (the owner's too, for bench/ux_shot.py) go to stdout as one JSON object, nothing else does:
the caller writes it to a root-only file. The previous run's object on stdin keeps the same passwords. Progress on
stderr, never a password.
"""
import base64
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "bench")]
import abi  # noqa: E402
import brewline  # noqa: E402
import perf  # noqa: E402

P = "brewline"
URL = os.environ.get("ABI_UX_URL", "http://127.0.0.1:8790")
SCHEMA = "https://schema.abi.invalid/v1/"
VAULT_DOC = """# Brewline KPI definitions

## Revenue
Revenue is what the customer paid: quantity times the unit price, less the discount. Taxes and tips are not revenue.
Retail (beans and cups) counts as revenue like coffee.

## Orders
One order is one line of the till: one product in one quantity. A customer may place several orders in one visit.

## Regions
Cafés belong to one of four regions (North, East, South, West) by their city. Regional managers see their own region.
"""


def log(*a) -> None:
    print("ux_seed:", *a, file=sys.stderr, flush=True)


def ask(o: perf.App, path: str, body: dict) -> dict:
    """An app route as the owner's browser calls it; ABI's confirmation is answered as their click (the seed is theirs)."""
    r = o.post(path, body)
    if r.get("confirm"):
        r = o.post(f"/confirm/{r['confirm']['id']}", {})
    return perf.done(r)


def load() -> None:
    if abi.folder(P) and abi.call("semantic_model", project=P).get("measures"):
        return log("brewline is loaded already")
    d = Path(tempfile.mkdtemp(prefix="ux_"))
    try:
        brewline.generate(d, 0.01)
        duckdb.sql(f"COPY (SELECT * FROM read_parquet('{brewline.q(d)}/orders/*.parquet')) TO '{brewline.q(d)}/orders.parquet' (FORMAT parquet)")
        made = abi.load("main", P, [str(d / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(d / "orders.parquet")])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if made["failed"] or "report" not in made:
        raise SystemExit(f"brewline didn't load: {made['failed'] or made.get('report_error')}")
    log("loaded", [x["table"] for x in made["loaded"]], "ready:", made.get("readiness", {}).get("ready"))


def v(i, t, title, ms, ds, x, y, w, h, **q):
    return {"id": i, "type": t, "title": title, "query": {"measures": ms, "dimensions": ds, **q}, "layout": {"x": x, "y": y, "w": w, "h": h}}


def showcase(cal: dict) -> dict:
    """One visual of every type the app's renderer draws (scripts/render.py chart(), app.py's slicer and text), with a
    stacked bar (a second dimension) and a month-year line; page 2 the two-dimension types the page shows as tables."""
    month = ["calendar.year", "calendar.month"] if {"year", "month"} <= set(cal) else ["calendar.day"]
    rev, n = ["revenue"], ["revenue", "orders_rows"]
    top = {"order_by": [{"field": "revenue", "desc": True}]}
    page1 = [
        {"id": "intro", "type": "text", "layout": {"x": 0, "y": 0, "w": 12, "h": 2},
         "text": "Every visual type on one page: Brewline Coffee, 2023-2025. Click a bar or a slice to filter by it."},
        v("kpi-revenue", "kpi", "Revenue", rev, [], 0, 2, 3, 3), v("kpi-orders", "kpi", "Orders", ["orders_rows"], [], 3, 2, 3, 3),
        v("kpi-quantity", "kpi", "Items sold", ["quantity"], [], 6, 2, 3, 3),
        v("slicer-region", "slicer", "Region", rev, ["cafes.region"], 9, 2, 3, 6),
        v("bar-city", "bar", "Revenue by city", rev, ["cafes.city"], 0, 5, 9, 5, **top),
        v("stacked", "bar", "Revenue by region and category", rev, ["cafes.region", "products.category"], 0, 10, 6, 5),
        v("line-month", "line", "Revenue per month", rev, month, 6, 10, 6, 5),
        v("combo", "combo", "Revenue and orders by product", n, ["products.product_name"], 0, 15, 6, 5, **top),
        v("pie", "pie", "Revenue by category", rev, ["products.category"], 6, 15, 6, 5),
        v("scatter", "scatter", "Cafés: revenue against orders", n, ["cafes.cafe_name"], 0, 20, 6, 5),
        v("map", "map", "Revenue by customers' home country", rev, ["customers.home_country"], 6, 20, 6, 5),
        v("waterfall", "waterfall", "Revenue by format", rev, ["cafes.format"], 0, 25, 4, 5),
        v("funnel", "funnel", "Orders by payment", ["orders_rows"], ["orders.payment"], 4, 25, 4, 5, order_by=[{"field": "orders_rows", "desc": True}]),
        v("treemap", "treemap", "Revenue by category and product", rev, ["products.category", "products.product_name"], 8, 25, 4, 5),
        v("table", "table", "Products", ["revenue", "orders_rows", "quantity"], ["products.product_name", "products.category"], 0, 30, 6, 6, **top),
        v("matrix", "matrix", "Revenue by region and year", rev, ["cafes.region", "calendar.year"], 6, 30, 6, 6),
    ]
    page2 = [v("heatmap", "heatmap", "Orders by region and payment", ["orders_rows"], ["cafes.region", "orders.payment"], 0, 0, 6, 6),
             v("sankey", "sankey", "Revenue from region to category", rev, ["cafes.region", "products.category"], 6, 0, 6, 6)]
    return {"$schema": SCHEMA + "report.json", "id": "showcase", "name": "Showcase", "description": "One visual of every type.",
            "pages": [{"id": "all", "name": "Every visual", "visuals": page1}, {"id": "two", "name": "Two dimensions", "visuals": page2}]}


def build(o: perf.App) -> None:
    sm = abi.call("semantic_model", project=P)
    ask(o, "/api/save_report", {"project": P, "report": showcase((sm.get("columns") or {}).get("calendar") or {})})
    # the starter measure "Orders rows" reads as a count of rows; the business word is Orders (R5)
    ask(o, "/api/set_measure", {"project": P, "id": "orders_rows", "name": "Orders", "expression": "COUNT(orders.order_id)",
                                "description": "The number of order lines."})
    for rep in abi.call("list_reports", project=P).get("reports") or []:  # the starter report's card titles follow
        vs = [v for pg in rep.get("pages") or [] for v in pg.get("visuals") or [] if v.get("title") == "Orders rows"]
        if vs:
            for v in vs:
                v["title"] = "Orders"
            ask(o, "/api/save_report", {"project": P, "report": {k: v for k, v in rep.items() if k != "owner"}})
    for term, text, ms in (("Revenue", "What the customer paid: quantity x unit price, less the discount (vault: Brewline KPI definitions).", ["revenue"]),
                           ("Order", "One line of the till: one product in one quantity.", ["orders_rows"]),
                           ("Region", "North, East, South or West: the café's city decides it.", [])):
        ask(o, "/api/set_glossary_term", {"project": P, "term": term, "definition": text, "measures": ms,
                                          "columns": ["cafes.region"] if term == "Region" else []})
    ask(o, "/api/save_dataset", {"project": P, "id": "revenue_by_month", "name": "Revenue by month",
                                 "query": {"measures": ["revenue"], "dimensions": ["calendar.year", "calendar.month"]}})
    ask(o, "/api/save_ml_model", {"project": P, "ml": {"$schema": SCHEMA + "ml.json", "id": "revenue_forecast", "name": "Revenue forecast",
                                                       "description": "Next quarter's revenue per month.", "task": "forecast",
                                                       "dataset": "revenue_by_month", "target": "revenue", "time": "calendar.month", "horizon": 3}})
    ask(o, "/api/save_flow", {"project": P, "flow": {
        "$schema": SCHEMA + "flow.json", "id": "monday_refresh", "name": "Monday refresh and PDF", "enabled": False,
        "description": "Reloads the orders every Monday at 07:00 and renders the Overview as a PDF (switched off).",
        "trigger": {"type": "schedule", "cron": "0 7 * * 1", "timezone": "Europe/Berlin"},
        "steps": [{"id": "refresh", "type": "refresh", "table": "orders"},
                  {"id": "pdf", "type": "snapshot", "after": ["refresh"], "report": "overview", "pages": "all", "format": "pdf"}]}})
    # Monitor: a check that holds (its flow posts to the Inbox within a minute) and one that doesn't
    for c in ({"name": "Revenue is flowing", "kind": "measure", "measure": "revenue", "op": ">", "against": "value", "value": 0},
              {"name": "Monthly revenue falls 20%", "kind": "measure", "measure": "revenue", "op": "<", "against": "prev", "value": -20,
               "period": "calendar.month", "skip_latest": True}):
        ask(o, "/monitor", {"action": "save", "check": c})
    ask(o, "/analyses", {"analyses": [{"name": "Revenue by region", "q": {"measures": ["revenue"], "dimensions": ["cafes.region"], "filters": [],
                                                                         "order_by": [{"field": "revenue", "desc": True}]},
                                       "type": "bar", "at": time.strftime("%Y-%m-%d %H:%M")}]})
    ask(o, "/vault/add", {"name": "brewline-kpis.md", "data": base64.b64encode(VAULT_DOC.encode()).decode()})
    log("showcase, glossary, dataset, ML model, flow, monitor, analysis and vault saved")


def train() -> None:
    m = next(x for x in abi.call("ml_models", project=P)["models"] if x["id"] == "revenue_forecast")
    if (m.get("latest_run") or {}).get("status") == "ok":
        return log("the forecast is trained already")
    abi.call("train_model", project=P, model="revenue_forecast")
    end = time.time() + 900
    while time.time() < end:
        st = (next(x for x in abi.call("ml_models", project=P)["models"] if x["id"] == "revenue_forecast").get("latest_run") or {}).get("status")
        if st not in ("queued", "running"):
            return log("forecast training:", st)
        time.sleep(5)
    log("forecast training still running after 15 min")


def people(o: perf.App, old: dict) -> dict:
    have = {u["name"] for u in abi.call("list_users")["users"]}
    out = {"owner": {"name": os.environ.get("ABI_USER") or "admin", "password": os.environ["ABI_PASSWORD"]}}
    # owner2: an Owner of the project (not of the workspace) whom no row rule lists: Viewer mode and the phone must not
    # leave them with empty reports (1.2.5: the published copy gave everyone Viewer, and its rules then hid every row)
    for role, name in (("editor", "editor1"), ("viewer", "viewer1"), ("owner2", "owner2")):
        pw = (old.get(role) or {}).get("password")
        if name not in have:
            pw = secrets.token_urlsafe(16)
            ask(o, "/admin/adduser", {"name": name, "password": pw})
        elif not pw:  # an account from before whose password is lost: a new one
            pw = secrets.token_urlsafe(16)
            ask(o, "/admin/setpass", {"user": name, "password": pw})
        ask(o, "/admin/role", {"user": name, "role": role.rstrip("2")})
        out[role] = {"name": name, "password": pw}
    ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=P)["accounts"]}
    perf.sure("set_rls_rule", project=P, id="viewer_region", table="cafes", column="region", users={str(ids["viewer1"]): ["South"]})
    log("editor1 (Editor), viewer1 (Viewer, South only) and owner2 (Owner, in no rule)")
    return out


def model() -> None:
    """--model: the agent's model provider from stdin (its JSON line, then its key line; ux_install.sh copies the Mac's),
    the default model proxy/claude-sonnet-5-5 with Opus as the fallback; the key is stored encrypted, never shown."""
    import settings  # noqa: PLC0415
    p, key = json.loads(sys.stdin.readline()), sys.stdin.readline().strip()
    settings.save_provider(p, key or None)
    settings.set_default("proxy/claude-sonnet-5-5", "medium", ["proxy/claude-opus-5-5"])
    try:
        log(settings.engine_sync())  # ABI's custom agents use the same model
    except Exception as e:  # noqa: BLE001 (the app's own agent has it either way)
        log("engine model not set:", type(e).__name__)
    log("model provider", p["id"], "saved; default proxy/claude-sonnet-5-5")


PINS = [{"kind": "visual", "ref": {"report": "showcase", "page": "all", "visual": v}, "title": t} | ({"wide": True} if w else {})
        for v, t, w in (("kpi-revenue", "Revenue", 0), ("kpi-orders", "Orders", 0), ("line-month", "Revenue per month", 1),
                        ("stacked", "Revenue by region and category", 1))]


def pins(o: perf.App) -> None:
    """A Home with widgets (each person's own, the app's pins.json): the same four for the owner and the viewer."""
    if not o.post("/pins", {})["result"]:
        perf.done(o.post("/pins", {"pins": PINS}))


def main() -> None:
    if sys.argv[1:] == ["--model"]:
        return model()
    out = os.fdopen(os.dup(1), "w")  # the accounts only: everything else (ours, git's, a child's) goes to stderr
    os.dup2(2, 1)
    old = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        old = json.loads(raw) if raw else {}
    load()
    o = perf.App(URL, os.environ.get("ABI_USER") or "admin", os.environ["ABI_PASSWORD"], P)
    try:
        o.page()
        build(o)
        train()
        acc = people(o, old)
        pins(o)
        if [f for f in o.post("/api/changes", {"project": P})["result"]["files"] if f["path"] != "project.json"]:
            ask(o, "/api/commit", {"project": P, "message": "UX bench seed: showcase, glossary, forecast, flow, monitor, people"})
        ask(o, "/publish", {})
        log("committed and published")
    finally:
        o.out()
    vw = perf.App(URL, acc["viewer"]["name"], acc["viewer"]["password"], P)
    try:
        vw.page()
        pins(vw)
    finally:
        vw.out()
    log("Home widgets pinned for the owner and viewer1")
    abi.sync(P)
    print(json.dumps(acc), file=out, flush=True)


if __name__ == "__main__":
    main()
