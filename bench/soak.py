#!/usr/bin/env python3
"""The soak bench: what the engine and its workers hold in memory over a long run of ordinary work, on a running ABI
(this machine's engine, or inside a docker install). The one-off probes of the 1.2.6 release run, as one script.

    .venv/bin/python bench/soak.py [--hours 24] [--idle 300] [--scale 0.05] [--project perf_soak] [--out FILE.jsonl]

One project (Brewline, object types, a yes/no model on customers, a plan over cafés) stays for the whole run, so the
number of projects is fixed. Each cycle: the orders are loaded again (a new snapshot), 40 queries nobody asked before,
a training run, a scoring run, two plan runs, a small project made and deleted, then --idle seconds of nothing (the
heavy worker's idle time is 180 s; a project nobody reads for ABI_IDLE_CLOSE_MIN minutes lets its DuckDB instance go).
Every minute one line goes to --out: seconds since the start, the cycle and what it was doing, the resident memory
(MB) of the engine, the default worker and the heavy worker, each with its children (null: not running), and
Postgres's connections. A cycle that fails is counted and named, and the run goes on.

At the end (and on Ctrl-C) one JSON line: the cycles, the failures, and for the engine and the default worker the
memory at the end of the idle part of the last cycle against the idle part nearest to 12 hours before it (the whole
run when it was shorter): `grew_mb`. The target (1.2.7 M0): at most 10 MB each over the last 12 hours of 24.

It signs in as perf.py does (ABI_USER/ABI_PASSWORD, or data/accounts.txt) and reads the engine's pid files in
ABI_PI_DATA; the project is removed at the end unless --keep.
"""
import argparse
import contextlib
import json
import shutil
import sys
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "bench")]
import abi  # noqa: E402
import brewline  # noqa: E402
from perf import D, ps, sure  # noqa: E402

ONTO = {"objects": [{"id": "cafe", "table": "cafes", "name": "Café", "plural": "Cafés", "title": "cafe_name"},
                    {"id": "customer", "table": "customers", "name": "Customer", "plural": "Customers", "title": "customer_id"}]}
ROWS = {"measures": ["revenue", "orders_rows"], "dimensions": ["customers.customer_id", "customers.segment", "customers.loyalty_member"]}
MODEL = {"id": "loyal", "name": "Loyalty chance", "task": "classification", "dataset": "customer_features", "object": "customer",
         "target": "customers.loyalty_member", "features": ["revenue", "orders_rows", "customers.segment"]}
PLAN = {"id": "seats", "name": "Seats per café", "over": {"type": "cafe"}, "decide": {"name": "Seats", "write": "seats", "max": 200, "integer": True},
        "objective": {"goal": "maximize", "name": "Revenue", "per_unit": {"measure": "revenue"}}, "limits": [{"name": "Seats to hand out", "max": 3000}]}
WAIT = 1800  # s: one training, scoring or plan run


def exists(p: str) -> bool:
    return p in {x["project"] for x in abi.call("list_projects")["projects"]}


def parts() -> dict:
    """MB of the engine, the default worker and the heavy worker, each with its children; None: not running."""
    procs, kids = ps(), defaultdict(list)
    for pid, (parent, _, _) in procs.items():
        kids[parent].append(pid)
    tree = lambda pid: [pid, *(x for k in kids[pid] for x in tree(k))]  # noqa: E731

    def mb(f: str) -> float | None:
        with contextlib.suppress(OSError, ValueError):
            pid = int((D / "run" / f).read_text().split()[0])
            if pid in procs:
                return round(sum(procs[x][1] for x in tree(pid) if x in procs) / 1024, 1)
        return None

    return {"engine": mb("app.pid"), "worker": mb("worker.pid"), "heavy": mb("worker_heavy.pid")}


def connections() -> int | None:
    with contextlib.suppress(Exception):
        import psycopg
        with psycopg.connect() as c:  # the engine's own environment (scripts/engine.sh env)
            return c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()").fetchone()[0]
    return None


def ended(read, what: str) -> dict:
    """`read()` until its run's status is neither queued nor running."""
    end = time.monotonic() + WAIT
    while (r := read()) is None or r.get("status") in ("queued", "running"):
        if time.monotonic() > end:
            raise RuntimeError(f"{what} didn't end within {WAIT} s")
        time.sleep(2)
    if r["status"] not in ("ok",):
        raise RuntimeError(f"{what} ended {r['status']}: {r.get('error')}")
    return r


def model_run(p: str, kind: str, run: str):
    r = next(m for m in abi.call("ml_models", project=p)["models"] if m["id"] == MODEL["id"])[kind]
    return r if r and r["run"] == run else None


def setup(p: str, scale: float, d: Path) -> None:
    brewline.generate(d, scale)
    duckdb.sql(f"COPY (SELECT * FROM read_parquet('{brewline.q(d)}/orders/*.parquet')) TO '{brewline.q(d)}/orders.parquet' (FORMAT parquet)")
    abi.load("demo", p, [str(d / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(d / "orders.parquet")])
    sure("save_ontology", project=p, ontology=ONTO)
    abi.call("save_dataset", project=p, id=MODEL["dataset"], name="Customer features", query=ROWS)
    abi.call("save_ml_model", project=p, ml=MODEL)
    abi.call("save_optimizer", project=p, optimizer=PLAN)
    small = d / "small"
    brewline.generate(small, 0.005)
    duckdb.sql(f"COPY (SELECT * FROM read_parquet('{brewline.q(small)}/orders/*.parquet')) TO '{brewline.q(small)}/orders.parquet' (FORMAT parquet)")


def cycle(p: str, n: int, d: Path, say) -> None:
    say("load")
    abi.call("refresh", project=p, table="orders")
    say("queries")
    for i in range(40):  # a filter nobody used before: no cached result
        abi.call("semantic_query", project=p, measures=["revenue", "orders_rows"], dimensions=[("cafes.city", "products.category", "customers.segment")[i % 3]],
                 filters=[{"column": "orders.quantity", "op": "<=", "value": 1000 + n * 40 + i}])
    say("train")
    ended(lambda r=sure_run("train_model", p): model_run(p, "latest_run", r), "training")
    say("score")
    ended(lambda r=sure_run("score_model", p): model_run(p, "latest_score", r), "scoring")
    for _ in range(2):
        say("plan")
        abi.call("run_optimizer", project=p, optimizer=PLAN["id"])
        ended(lambda: abi.call("optimizers", project=p, optimizer=PLAN["id"]), "the plan")
    say("project")
    tmp = f"{p}_tmp"
    abi.load("demo", tmp, [str(d / "small" / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(d / "small" / "orders.parquet")])
    abi.call("semantic_query", project=tmp, measures=["revenue"], dimensions=["cafes.region"])
    sure("delete_project", project=tmp)


def sure_run(action: str, p: str) -> str:
    """train_model or score_model, its confirmation answered here; the run's ID."""
    try:
        return abi.call(action, project=p, model=MODEL["id"])["run"]
    except abi.ActionError as e:
        if e.status != 409 or not (e.body or {}).get("confirm_token"):
            raise
        return abi.call(action, confirm=e.body["confirm_token"], project=p, model=MODEL["id"])["run"]


def summary(samples: list, cycles: int, failed: list, hours: float) -> dict:
    idle = [s for s in samples if s["doing"] == "idle"]
    ends = [g[-1] for g in ([s for s in idle if s["cycle"] == c] for c in sorted({s["cycle"] for s in idle})) if g]  # each cycle's last idle sample
    out = {"bench": "soak", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "hours": round(hours, 2), "cycles": cycles, "failed": failed[:20], "samples": len(samples)}
    if len(ends) >= 2:
        last = ends[-1]
        first = min(ends, key=lambda s: abs(s["t"] - (last["t"] - 12 * 3600)))
        out |= {"compared_hours": round((last["t"] - first["t"]) / 3600, 2),
                **{k: {"then_mb": first[k], "now_mb": last[k], "grew_mb": None if None in (first[k], last[k]) else round(last[k] - first[k], 1),
                       "highest_mb": max((s[k] for s in samples if s[k] is not None), default=None)} for k in ("engine", "worker")},
                "heavy_stopped_in_idle": sum(s["heavy"] is None for s in ends), "idle_parts": len(ends),
                "postgres_connections": [first["pg"], last["pg"]]}
    return out


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--idle", type=float, default=300, help="seconds of nothing at the end of each cycle")
    ap.add_argument("--scale", type=float, default=0.05, help="bench/brewline.py's (0.05: about 370,000 orders)")
    ap.add_argument("--project", default="perf_soak")
    ap.add_argument("--out", default=str(D / "soak.jsonl"), help="one line a minute")
    ap.add_argument("--keep", action="store_true", help="leave the project when the run ends")
    a = ap.parse_args(argv)
    p, d = a.project, Path(tempfile.mkdtemp(prefix="soak_"))
    start, state, samples, failed, stop = time.monotonic(), {"cycle": 0, "doing": "setup"}, [], [], threading.Event()

    def sample() -> None:
        with open(a.out, "a") as f:
            while not stop.wait(60):
                s = {"t": round(time.monotonic() - start), **state, **parts(), "pg": connections()}
                samples.append(s)
                f.write(json.dumps(s) + "\n")
                f.flush()

    say = lambda doing: state.update(doing=doing)  # noqa: E731
    try:
        if exists(p):
            sure("delete_project", project=p)
        setup(p, a.scale, d)
        threading.Thread(target=sample, daemon=True).start()
        while time.monotonic() - start < a.hours * 3600:
            state["cycle"] += 1
            try:
                cycle(p, state["cycle"], d, say)
            except Exception as e:  # noqa: BLE001 - named in the result; the run goes on
                failed.append(f"cycle {state['cycle']} ({state['doing']}): {str(e)[:200]}")
            say("idle")
            time.sleep(a.idle)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        out = summary(samples, state["cycle"], failed, (time.monotonic() - start) / 3600)
        print(json.dumps(out))
        shutil.rmtree(d, ignore_errors=True)
        if not a.keep:
            for x in (f"{p}_tmp", p):
                with contextlib.suppress(Exception):
                    if exists(x):
                        sure("delete_project", project=x)
    return out


if __name__ == "__main__":
    main()
