#!/usr/bin/env python3
"""The agent bench: fixed tasks on generated Brewline Coffee data (bench/brewline.py), each run through Pi as a person
at the terminal would (tests/pi_driver.py) and checked against the generator's known answers.

    .venv/bin/python bench/bench.py [--runs 2] [--tasks ask,why,report,load] [--model provider/model] [--fallback p/m,...]

  ask     revenue by region in 2025: every region's figure stated, nothing masked
  why     why revenue changed in the latest complete month: no [not verified] and the planted region named
  report  a small managers' report: a KPI, a bar by region and a line; the KPI and the bar right to the cent
  load    a raw Parquet export to a Ready model in a new project, revenue by region right (the wall time is the load's)

ask, why and report share one throwaway project loaded through ABI's actions (abi.load, as the tests do). Every
project and file the bench made is deleted at the end. Each run (wall seconds, model turns, tool calls and errors,
masked figures, tokens, pass) is printed and kept with its answer in logs/bench-<stamp>.jsonl; the medians per task
go to bench/RESULTS.md. --model and --fallback default to ABI_PI_LIVE_MODEL and ABI_PI_LIVE_FALLBACK (pi_driver)."""
import argparse
import json
import os
import re
import shutil
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]
import abi  # noqa: E402
import brewline  # noqa: E402 (beside this file)
from conftest import drop  # noqa: E402
from pi_driver import ask  # noqa: E402

RESULTS, RUNS = ROOT / "bench" / "RESULTS.md", ROOT / "logs"
SCALE = 0.01  # about 74k orders: a project loads in seconds and North's December drop stands out (brewline.py)
MASK = "[not verified]"
UNITS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6}


def says(text: str, value: float) -> bool:
    """The answer states `value` as the rows give it, or rounded for display to 3 significant figures or better."""
    return any(abs(float(n.replace(",", "")) * UNITS.get(u.lower(), 1) - value) <= 0.005 * abs(value) + 0.01
               for n, u in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|mn|million|m)?\b", text, re.I))


def cents(x):
    return round(x, 2) if isinstance(x, (int, float)) else x


def totals(known: dict) -> dict:
    """Revenue per region over every year, to the cent."""
    return {r: round(sum(y.values()), 2) for r, y in known["revenue_by_region_year"].items()}


def t_ask(c: dict, i: int):
    r = ask(f"In project {c['project']}: what was total revenue by region in 2025? Give the figures.")
    want = [y["2025"] for y in c["known"]["revenue_by_region_year"].values()]
    return r, bool(r.called("semantic_query")) and MASK not in r.answer and all(says(r.answer, v) for v in want)


def t_why(c: dict, i: int):
    r = ask(f"In project {c['project']}: why did revenue change in the latest complete month of data compared with the month before?")
    return r, bool(r.called("semantic_query")) and MASK not in r.answer and bool(re.search(rf"\b{re.escape(c['known']['why']['member'])}\b", r.answer))


def t_report(c: dict, i: int):
    p, rid = c["project"], f"managers_{i}"
    r = ask(f"In project {p} build a small report for our café managers with id {rid}: a KPI of total revenue, a bar chart"
            " of revenue by region and a line chart of revenue by month.")
    return r, MASK not in r.answer and report_ok(p, rid, totals(c["known"]))


def report_ok(p: str, rid: str, want: dict) -> bool:
    """A KPI, a bar and a line as ABI draws them, every visual read without error, the KPI the total revenue and the
    bar by region each region's revenue, to the cent."""
    try:
        rep = abi.call("list_reports", project=p, report=rid)["reports"][0]
        data = [abi.call("visual_data", project=p, report=rid, page=pg["id"], visual=v["id"]) for pg in rep["pages"] for v in pg["visuals"]]
    except abi.ActionError:
        return False
    data = [d for d in data if d.get("rows")]
    kpi = any(d["type"] == "kpi" and round(sum(want.values()), 2) in map(cents, d["rows"][0]) for d in data)
    bar = any(d["type"] == "bar" and d["columns"][0].endswith(".region") and {r[0]: cents(r[-1]) for r in d["rows"]} == want for d in data)
    return kpi and bar and any(d["type"] == "line" for d in data)


def t_load(c: dict, i: int):
    p, want = f"{c['project']}_load{i}", totals(c["known"])
    r = ask(f"Load the file {c['flat']} into a new ABI project {p} in workspace demo, make sure the model is Ready, and tell"
            " me total revenue (the sum of the revenue column) by region with the figures.")
    return r, MASK not in r.answer and all(says(r.answer, v) for v in want.values()) and loaded(p, want)


def loaded(p: str, want: dict) -> bool:
    """The project is Ready, and ABI's revenue by region over the loaded file equals the known answers to the cent."""
    try:
        sm = abi.call("semantic_model", project=p)
        m = next(x["id"] for x in sm["measures"] if re.fullmatch(r"SUM\(\w+\.revenue\)", x["expression"].replace(" ", "")))
        t = next(t for t, cols in sm["columns"].items() if "region" in cols)
        rows = abi.call("semantic_query", project=p, measures=[m], dimensions=[f"{t}.region"])["rows"]
        return abi.call("readiness", project=p)["ready"] and {k: cents(v) for k, v in rows} == want
    except (abi.ActionError, StopIteration):  # no project, no revenue measure or no region column
        return False


TASKS = {"ask": t_ask, "why": t_why, "report": t_report, "load": t_load}


def data(out: Path) -> dict:
    """Brewline at SCALE: one Parquet file per table for the shared project (the orders' months in one), and every
    order with its café and product in one file, the raw export the load task starts from."""
    known = brewline.generate(out, SCALE)
    orders, cafes, products = (f"read_parquet('{brewline.q(out)}/{t}/*.parquet')" for t in ("orders", "cafes", "products"))
    duckdb.sql(f"COPY (SELECT * FROM {orders}) TO '{brewline.q(out)}/orders.parquet' (FORMAT parquet)")
    duckdb.sql(f"COPY (SELECT o.order_id, o.order_date, o.cafe_id, city, region, o.customer_id, o.product_id, product_name, category,"
               f" quantity, unit_price, discount, revenue FROM {orders} o JOIN {cafes} USING (cafe_id) JOIN {products} USING (product_id)"
               f" ORDER BY order_id) TO '{brewline.q(out)}/brewline.parquet' (FORMAT parquet)")
    return {"known": known, "files": [str(out / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(out / "orders.parquet")],
            "flat": out / "brewline.parquet"}


def num(x: float) -> str:
    return f"{x:.1f}".removesuffix(".0")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--model", default=os.environ.get("ABI_PI_LIVE_MODEL"), help="provider/model (default ABI_PI_LIVE_MODEL, else Pi's)")
    ap.add_argument("--fallback", default=os.environ.get("ABI_PI_LIVE_FALLBACK"), help="provider/model,... asked in order when the model fails")
    a = ap.parse_args(argv)
    tasks = a.tasks.split(",")
    if bad := set(tasks) - set(TASKS):
        ap.error(f"unknown task {', '.join(sorted(bad))}; one of {', '.join(TASKS)}")
    os.environ.update({k: v for k, v in (("ABI_PI_LIVE_MODEL", a.model), ("ABI_PI_LIVE_FALLBACK", a.fallback)) if v})
    stamp = time.strftime("%m%d%H%M%S")
    c, out, runs = {"project": f"bench_{stamp}"}, ROOT / "scratch" / f"bench_{stamp}", []
    RUNS.mkdir(exist_ok=True)
    try:
        c |= data(out)
        if set(tasks) - {"load"} and not (made := abi.load("demo", c["project"], c["files"])).get("readiness", {}).get("ready"):
            raise SystemExit(f"The bench project isn't Ready, so no run would be fair: {made.get('failed') or made.get('readiness')}")
        for task in tasks:
            for i in range(a.runs):
                r, ok = TASKS[task](c, i)
                rec = {"task": task, "run": i, "ok": ok, "seconds": r.seconds, "turns": r.usage["turns"], "tools": len(r.tools),
                       "tool_errors": sum(1 for t in r.tools if t[2]), "masked": r.answer.count(MASK), "input": r.usage["input"],
                       "cached": r.usage["cacheRead"], "output": r.usage["output"], "model": r.model, "tool_names": [t[0] for t in r.tools], "answer": r.answer}
                runs.append(rec)
                with open(RUNS / f"bench-{stamp}.jsonl", "a") as f:
                    f.write(json.dumps(rec) + "\n")
                print(json.dumps({k: v for k, v in rec.items() if k not in ("tool_names", "answer")}), flush=True)
    finally:
        drop(x["project"] for x in abi.call("list_projects")["projects"] if x["project"].startswith(c["project"]))
        shutil.rmtree(out, ignore_errors=True)
    by = ", ".join(f"{m} in {n} of {len(runs)}" for m, n in Counter(x["model"] for x in runs).most_common())
    lines = [f"\n## {time.strftime('%Y-%m-%d %H:%M')}: {a.model or 'Pi default model'}"
             f"{f', fallback {a.fallback}' if a.fallback else ''} (runs={a.runs}; answered by {by})\n",
             "| task | pass | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for task in tasks:
        rs = [x for x in runs if x["task"] == task]
        lines.append(f"| {task} | {sum(x['ok'] for x in rs)}/{len(rs)} | " + " | ".join(
            num(statistics.median(x[k] for x in rs)) for k in ("seconds", "turns", "tools", "tool_errors", "masked", "input", "cached", "output")) + " |")
    with open(RESULTS, "a") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
