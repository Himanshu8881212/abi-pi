#!/usr/bin/env python3
"""The performance bench: the numbers ABI's speed and memory targets are judged by, measured on a running ABI (this
machine's engine and app, or inside a docker install). Each run prints one JSON line; --results FILE appends it there.

    .venv/bin/python bench/perf.py pages --sessions 50 [--people N] [--project P] [--url http://127.0.0.1:8790]
    .venv/bin/python bench/perf.py memory --chats 4 [--project P] [--url ...] [--app-pid PID]
    .venv/bin/python bench/perf.py load --rows 1000000
    .venv/bin/python bench/perf.py isolation --rows 2000000 [--queries 30]

  pages      N sessions signed in to the app as throwaway Viewer and Developer accounts open the page at the same
             moment: first, then right after a commit (a probe measure committed through the app; it is deleted after).
             The app keeps page data per person, so --people distinct accounts (default one per session).
             p50/p95/p99/max ms, errors and dropped connections of each round.
  memory     RSS of the app, the engine, the worker, Postgres and the chats' Pi processes (everything the app started):
             idle, after K chats started, and after each answered one short question (when a model answers).
  load       N generated Brewline orders (bench/brewline.py) into a new project through add_data from the engine's
             files folder: seconds, rows/s, the project's lake bytes, seconds until the model is Ready, and whether
             revenue by region equals the generator's known answers to the cent.
  isolation  uncached semantic_query latency in a small project, idle and while N orders load into another.

The workspace owner's name and password come from ABI_USER and ABI_PASSWORD (a docker install has both), the app from
--url or ABI_PERF_URL, the engine from ABI_URL (abi.py), its data folder from ABI_PI_DATA. The accounts, chats,
projects and files a run makes are removed at the end. pages needs a project without uncommitted work; it commits its
probe and then the probe's and the accounts' removal (ABI's roles file, security/roles.json, then lists none of them).
A docker install keeps the engine and the processes inside the container, so the bench runs there:

    docker cp bench abi-bench:/app/ && docker exec abi-bench python3 bench/perf.py pages --sessions 50
"""
import argparse
import concurrent.futures as cf
import contextlib
import http.client
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "bench")]
import abi  # noqa: E402
import brewline  # noqa: E402
import settings  # noqa: E402

D = Path(os.environ.get("ABI_PI_DATA", ROOT / "data"))  # scripts/engine.sh's data: pid files, the files folder, the lake
FILES, LAKE = (Path(os.environ.get(v) or D / d) for v, d in (("ABI_FILES_PATH", "files"), ("ABI_DATA_PATH", "lake")))
WAIT = 300  # s: a chat's answer, the app showing a change
TIMEOUT = 900  # s: one request (as long as the app waits for the engine; 1.1 serves 50 people's pages in turn)
ASK = "Reply with the single word: ready."


class App:
    """One browser signed in to the app as a person: its session cookie, and the page key once a page opened."""

    def __init__(self, url: str, name: str, password: str, project: str = ""):
        u = urllib.parse.urlsplit(url)
        self.host, self.port, self.project, self.cookie, self.key = u.hostname, u.port or 80, project, "", ""
        st, _, hd = self.http("POST", "/signin", urllib.parse.urlencode({"name": name, "password": password}),
                              {"Content-Type": "application/x-www-form-urlencoded"})
        self.cookie = (hd.get("Set-Cookie") or "").split(";")[0]
        if st != 303 or not self.cookie.startswith("abi_u="):
            raise SystemExit(f"{name} can't sign in to the app at {url}.")

    def http(self, method: str, path: str, body=None, headers: dict | None = None):
        c = http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT)
        try:
            c.request(method, path, body, {"Cookie": self.cookie, **(headers or {})})
            r = c.getresponse()
            return r.status, r.read(), r.headers
        finally:
            c.close()

    def page(self) -> bytes:
        """The page as this person opens it: its HTML carries all of the page's data (window.APP), computed as them."""
        st, b, _ = self.http("GET", f"/?p={self.project}")
        if st != 200 or b"window.APP=" not in b:
            raise SystemExit(f"The app gave no page ({st}).")
        self.key = self.key or re.search(rb"KEY='([^']+)'", b)[1].decode()
        return b

    def post(self, path: str, body: dict, cid: str = "perf") -> dict:
        _, b, _ = self.http("POST", path, json.dumps(body), {"Content-Type": "application/json", "X-Abi-App": self.key,
                                                            "X-Abi-Client": cid, "X-Abi-Project": self.project})
        return json.loads(b or b"{}")

    def out(self) -> None:
        self.http("POST", "/signout")


def owner(a) -> App:
    """The workspace owner in the app (Users & access, Commit), on --project or the app's first project."""
    if not os.environ.get("ABI_PASSWORD"):
        raise SystemExit("Set ABI_USER and ABI_PASSWORD to the workspace owner's: the bench signs in to the app as them.")
    o = App(a.url, os.environ.get("ABI_USER", "himanshu"), os.environ["ABI_PASSWORD"], a.project or "")
    shown = re.search(rb'window\.APP=\{"project": "(\w+)"', o.page())[1].decode()
    if a.project and shown != a.project:
        o.out()
        raise SystemExit(f"The app has no project {a.project} for {os.environ.get('ABI_USER', 'himanshu')}.")
    o.project = shown
    return o


def accounts(o: App, roles: list, made: list, stamp: str) -> list:
    """Throwaway accounts with these roles in the owner's project, added through the app's Users & access; `made`
    gets each name as it comes, so the end removes them whatever happened. Passwords stay in this process."""
    out = []
    for i, role in enumerate(roles):
        name, pw = f"perf_{stamp}_{i}", secrets.token_urlsafe(16)
        done(o.post("/admin/adduser", {"name": name, "password": pw}))
        made.append(name)
        done(o.post("/admin/role", {"user": name, "role": role}))
        out.append((name, pw))
    return out


def done(r: dict) -> dict:
    if "result" not in r:
        raise SystemExit(f"The app refused: {r.get('error') or r}")
    return r


def sure(action: str, **args) -> None:
    """An action on what this run made (its accounts and projects): ABI's confirmation is answered here."""
    try:
        abi.call(action, **args)
    except abi.ActionError as e:
        if e.status != 409 or not (e.body or {}).get("confirm_token"):
            raise
        abi.call(action, confirm=e.body["confirm_token"], **args)


def pct(xs: list, q: float) -> float | None:
    """Nearest-rank percentile."""
    xs = sorted(xs)
    return round(xs[max(0, math.ceil(q / 100 * len(xs)) - 1)], 1) if xs else None


def burst(sessions: list) -> dict:
    """Every session opens its page at the same moment: the latency of each whole page, errors and dropped connections."""
    gate = threading.Barrier(len(sessions))

    def one(s: App):
        gate.wait()
        t = time.perf_counter()
        try:
            st, b, _ = s.http("GET", f"/?p={s.project}")
            kind = "ok" if st == 200 and b"window.APP=" in b else "errors"
        except (OSError, http.client.HTTPException):  # reset, closed or timed out
            kind = "dropped"
        return kind, (time.perf_counter() - t) * 1000

    with cf.ThreadPoolExecutor(len(sessions)) as ex:
        got = list(ex.map(one, sessions))
    ms = [m for k, m in got if k == "ok"]
    return {**{f"p{q}": pct(ms, q) for q in (50, 95, 99)}, "max": pct(ms, 100), **{k: sum(x == k for x, _ in got) for k in ("errors", "dropped")}}


def shown(o: App, r: dict, marker: str) -> None:
    """A change made through the app drops every cached page: wait until the owner's page shows it, so the next round
    starts from pages computed anew."""
    done(r)
    end = time.time() + WAIT
    while marker.encode() not in o.page():
        if time.time() > end:
            raise SystemExit(f"The app didn't show '{marker}' within {WAIT} s.")
        time.sleep(0.2)


def pages(a) -> dict:
    o, made, sessions, probe, stamp = owner(a), [], [], None, time.strftime("%m%d%H%M%S")
    p, people = o.project, a.people or a.sessions
    try:
        # a commit takes every uncommitted change: only the app's own one (opening a project turns auto-commit off) may wait
        if {f["path"] for f in o.post("/api/changes", {"project": p})["result"]["files"]} - {"project.json"}:
            raise SystemExit(f"{p} has uncommitted changes: commit or undo them first (the bench commits a probe, then its removal).")
        if not (ms := o.post("/api/semantic_model", {"project": p, "all": True})["result"]["measures"]):
            raise SystemExit(f"{p} has no measure yet: the probe copies one.")
        rec = {"project": p, "sessions": a.sessions, "people": people, "page_kb": len(o.page()) // 1024}
        creds = accounts(o, [("viewer", "editor")[i % 2] for i in range(people)], made, stamp)
        with cf.ThreadPoolExecutor(10) as ex:  # each sign-in checks a password hash
            sessions = list(ex.map(lambda i: App(a.url, *creds[i % people], p), range(a.sessions)))
        rec["first"] = burst(sessions)
        probe, msg = f"perf_{stamp}", f"perf.py {stamp}: page check"
        shown(o, o.post("/api/set_measure", {"project": p, "id": probe, "name": "perf.py probe", "expression": ms[0]["expression"]}), probe)
        shown(o, o.post("/api/commit", {"project": p, "message": msg}), msg)
        rec["after_commit"] = burst(sessions)
    finally:  # through ABI itself, so a stopped app leaves nothing behind either
        signed_out(sessions + [o])
        if probe and any(m["id"] == probe for m in abi.call("semantic_model", project=p, all=True)["measures"]):
            sure("delete_measure", project=p, id=probe)
        for n in made:
            sure("delete_user", name=n)
        if (made or probe) and abi.call("changes", project=p)["files"]:  # the probe gone, roles.json without the accounts
            abi.call("commit", project=p, message=f"perf.py {stamp}: probe and accounts removed")
    return rec


def signed_out(apps: list) -> None:
    for x in apps:
        with contextlib.suppress(OSError, http.client.HTTPException):  # the app may be gone
            x.out()


def ps() -> dict:
    """{pid: (parent, RSS in KB, command line)} of every live process (ps: macOS and Linux; an exited child the app
    hasn't reaped yet has no memory and doesn't count)."""
    out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "rss=", "-o", "args="], capture_output=True,
                         text=True, check=True).stdout
    rows = (line.split(None, 3) + [""] for line in out.splitlines())
    return {int(x[0]): (int(x[1]), int(x[2]), x[3]) for x in rows if int(x[2])}


def app_pid(url: str) -> int:
    port = str(urllib.parse.urlsplit(url).port or 80)
    apps = {pid: args.split() for pid, (_, _, args) in ps().items() if "scripts/app.py" in args}
    hit = [pid for pid, args in apps.items() if port in args] or list(apps) * (len(apps) == 1)
    if not hit:
        raise SystemExit(f"Can't tell which process serves {url}: pass --app-pid.")
    return hit[0]


def rss(app: int) -> dict:
    """MB and process count of each part, each with its children; the chats' Pi processes are the app's children."""
    procs, kids = ps(), defaultdict(list)
    for pid, (parent, _, _) in procs.items():
        kids[parent].append(pid)
    tree = lambda pid: [pid, *(x for k in kids[pid] for x in tree(k))]  # noqa: E731
    pid = lambda f: int((D / f).read_text().split()[0])  # noqa: E731 (engine.sh's pid files, Postgres's own)
    parts = {"app": tree(app), "engine": tree(pid("run/app.pid")), "worker": tree(pid("run/worker.pid")), "postgres": tree(pid("pg/postmaster.pid"))}
    parts["pi"], parts["app"] = parts["app"][1:], parts["app"][:1]
    return {k: {"mb": round(sum(procs[x][1] for x in v if x in procs) / 1024, 1), "n": sum(x in procs for x in v)} for k, v in parts.items()}


class Events(threading.Thread):
    """One browser window's event stream (/events): which model answered its chat ("" when none did: the app's note)."""

    def __init__(self, s: App, cid: str):
        super().__init__(daemon=True)
        self.c = http.client.HTTPConnection(s.host, s.port, timeout=WAIT)
        self.c.request("GET", f"/events?k={s.key}&c={cid}&p={s.project}", headers={"Cookie": s.cookie})
        self.r, self.answer = self.c.getresponse(), None
        self.start()

    def run(self) -> None:
        try:
            for line in self.r:
                if line.startswith(b"data: ") and (e := json.loads(line[6:])).get("type") == "answer":
                    self.answer = e.get("model") or ""
                    return
        except (OSError, ValueError, http.client.HTTPException):
            pass


def until(ok, wait: float = WAIT) -> bool:
    end = time.time() + wait
    while not ok() and time.time() < end:
        time.sleep(0.5)
    return ok()


def settled(app: int) -> dict:
    """rss() once the Pi processes have stopped growing for 3 s: a starting Pi (node, its extensions, ABI's tools)
    pauses while its helpers run."""
    seen = [rss(app)]
    while len(seen) < 60:
        time.sleep(1)
        seen.append(rss(app))
        if len(seen) >= 4 and max(w := [x["pi"]["mb"] for x in seen[-4:]]) - min(w) <= 0.02 * max(w):
            break
    return seen[-1]


def memory(a) -> dict:
    app = a.app_pid or app_pid(a.url)
    rec = {"chats": a.chats, "idle": rss(app)}
    if not a.chats:
        return rec
    o, made, chats, stamp = owner(a), [], [], time.strftime("%m%d%H%M%S")
    s, pis = None, lambda: sum(parent == app and "--mode rpc" in args for parent, _, args in ps().values())  # noqa: E731
    try:
        s = App(a.url, *accounts(o, ["editor"], made, stamp)[0], o.project)
        s.key, before = o.key, pis()
        for i in range(a.chats):  # one chat per browser window: each runs its own Pi
            ev = Events(s, f"perf{i}")
            chats.append((f"perf{i}", s.post("/chat/new", {}, f"perf{i}")["id"], ev))
            s.post("/chat", {"message": ASK, "page": "home"}, f"perf{i}")
        answered = lambda: all(ev.answer is not None for _, _, ev in chats)  # noqa: E731
        until(lambda: pis() >= before + a.chats or answered())  # a Pi with no model to ask may end at once
        rec["started"] = settled(app)
        # the model a chat starts with, read as app.py's Hub.chat does: Settings, else the deployment's; none: after
        # start only (never through /admin/settings: any call there stops every chat agent that isn't busy)
        rec["model"] = settings.load()["model"] or os.environ.get("ABI_PI_LLM_MODEL") or None
        if rec["model"]:
            until(answered)
            rec["answered_by"] = [ev.answer for _, _, ev in chats]
            rec["answered"] = rss(app) if all(rec["answered_by"]) else None  # it didn't answer (quota, key): after start only
    finally:
        with contextlib.suppress(OSError, http.client.HTTPException, ValueError):  # the app may be gone (its Pis with it)
            for cid, chat, ev in chats:
                s.post("/chat/delete", {"id": chat}, cid)  # its Pi stops, its files go
                ev.c.close()
        signed_out([s, o] if s else [o])
        for n in made:
            sure("delete_user", name=n)
            with contextlib.suppress(OSError):  # its chats' folder, empty now
                (abi.folder(o.project).parent / "_views" / o.project / "chats" / n).rmdir()
    # what a chat adds: its Pi and the app's side of it (the engine and Postgres serve everyone, and move with them)
    mine = lambda g: g["app"]["mb"] + g["pi"]["mb"]  # noqa: E731
    rec["per_chat_mb"] = {k: round((mine(rec[k]) - mine(rec["idle"])) / a.chats, 1) for k in ("started", "answered") if rec.get(k)}
    return rec


def add(p: str, table: str, location: str) -> dict:
    """The engine's own add_data on a server path in its files folder (abi.call's door takes s3:// Parquet only)."""
    return abi._open("POST", "/api/actions/add_data", {"project": p, "table": table, "location": location}, timeout=6 * 3600)[1]


def build(p: str, d: Path) -> dict:
    """Generated Brewline tables into a new project p, then a model as a first load makes it (keys, relationships,
    calendar, descriptions, a revenue measure): seconds per table, seconds until Ready, the project's lake bytes."""
    abi.call("create_project", project=p, name=f"perf {p}")
    t0, took = time.perf_counter(), {}
    for t in ("cafes", "customers", "products", "orders"):
        t1 = time.perf_counter()
        add(p, t, f"{d.relative_to(FILES)}/{t}/*.parquet")
        took[t] = round(time.perf_counter() - t1, 2)
    abi.call("build_model", project=p)
    for t, info in abi.call("semantic_model", project=p, all=True)["tables"].items():
        if not info.get("description"):
            abi.call("set_table", project=p, table=t, description=f"Brewline {t}, generated by bench/brewline.py.")
    abi.call("set_measure", project=p, id="revenue", name="Revenue", expression="SUM(orders.revenue)")
    ready = abi.call("readiness", project=p)
    return {"seconds": took["orders"], "tables_s": took, "ready_s": round(time.perf_counter() - t0, 2), "ready": ready["ready"],
            **({} if ready["ready"] else {"missing": ready["missing"]}),
            "lake_bytes": sum(f.stat().st_size for f in (LAKE / p).rglob("*") if f.is_file())}


def brew(rows: int, d: Path) -> dict:
    return brewline.generate(d, rows / brewline.ORDERS)


def load(a) -> dict:
    p = f"perf_{time.strftime('%m%d%H%M%S')}_load"
    d = FILES / p
    try:
        known = brew(a.rows, d)
        rec = {"rows": known["rows"]["orders"], **build(p, d)}
        got = abi.call("semantic_query", project=p, measures=["revenue"], dimensions=["cafes.region"])["rows"]
        rec["exact"] = {k: round(v, 2) for k, v in got} == {r: round(sum(y.values()), 2) for r, y in known["revenue_by_region_year"].items()}
        rec["rows_per_s"] = round(rec["rows"] / rec["seconds"])
    finally:
        if any(x["project"] == p for x in abi.call("list_projects")["projects"]):
            sure("delete_project", project=p)
        shutil.rmtree(d, ignore_errors=True)
    return rec


def copies(p: str) -> bool:
    """The worker writes a model's query copies within a minute of a change, and queries plan differently before."""
    return until(lambda: any(r[3] == "build query copies" for r in abi.call("list_snapshots", project=p)["rows"]), 90)


def asked(p: str, i: int) -> float:
    t = time.perf_counter()  # a new limit is new SQL: never ABI's query cache
    abi.call("semantic_query", project=p, measures=["revenue"], dimensions=["cafes.region"], limit=1000 + i)
    return (time.perf_counter() - t) * 1000


def isolation(a) -> dict:
    stamp = time.strftime("%m%d%H%M%S")
    (pa, da), (pb, db) = ((f"perf_{stamp}_{x}", FILES / f"perf_{stamp}_{x}") for x in "ab")
    try:
        known = brew(a.rows, da)
        brew(37_000, db)  # the smallest Brewline: a project people are reading
        build(pb, db)
        rec = {"rows": known["rows"]["orders"], "copies": copies(pb)}
        abi.call("create_project", project=pa, name=f"perf {pa}")
        asked(pb, 0)  # the lake attached
        idle, during, box = [asked(pb, i) for i in range(1, a.queries + 1)], [], {}

        def big():
            t = time.perf_counter()
            try:
                add(pa, "orders", f"{da.relative_to(FILES)}/orders/*.parquet")
            except abi.ActionError as e:
                box["error"] = (e.body or {}).get("message")
            box["s"] = round(time.perf_counter() - t, 2)
        th = threading.Thread(target=big)
        th.start()
        while not during or th.is_alive():  # at least one query sent while the load runs
            during.append(asked(pb, a.queries + 1 + len(during)))
        th.join()
        rec |= {"load_s": box["s"], **({"load_error": box["error"]} if "error" in box else {}),
                **{k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95)} for k, v in (("idle", idle), ("during_load", during))}}
        rec |= {f"x_{q}": round(rec["during_load"][q] / rec["idle"][q], 2) for q in ("p50", "p95")}
    finally:
        listed = {x["project"] for x in abi.call("list_projects")["projects"]}
        for p in (pa, pb):
            if p in listed:
                sure("delete_project", project=p)
        for d in (da, db):
            shutil.rmtree(d, ignore_errors=True)
    return rec


def main(argv=None) -> dict:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--results", help="also append the JSON line to this file")
    web = argparse.ArgumentParser(add_help=False)
    web.add_argument("--url", default=os.environ.get("ABI_PERF_URL", "http://127.0.0.1:8790"), help="the app (ABI_PERF_URL)")
    web.add_argument("--project", help="default: the app's first project")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="bench", required=True)
    x = sub.add_parser("pages", parents=[common, web])
    x.add_argument("--sessions", type=int, default=50)
    x.add_argument("--people", type=int, help="distinct throwaway accounts (default: one per session)")
    x = sub.add_parser("memory", parents=[common, web])
    x.add_argument("--chats", type=int, default=4)
    x.add_argument("--app-pid", type=int, help="the app's process (default: the scripts/app.py serving --url)")
    sub.add_parser("load", parents=[common]).add_argument("--rows", type=int, default=1_000_000)
    x = sub.add_parser("isolation", parents=[common])
    x.add_argument("--rows", type=int, default=2_000_000, help="orders loaded into the other project")
    x.add_argument("--queries", type=int, default=30, help="idle queries")
    a = ap.parse_args(argv)
    rec = {"bench": a.bench, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           **{"pages": pages, "memory": memory, "load": load, "isolation": isolation}[a.bench](a)}
    line = json.dumps(rec)
    print(line, flush=True)
    if a.results:
        with open(a.results, "a") as f:
            f.write(line + "\n")
    return rec


if __name__ == "__main__":
    main()
