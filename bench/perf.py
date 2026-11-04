#!/usr/bin/env python3
"""The performance bench: the numbers ABI's speed and memory targets are judged by, measured on a running ABI (this
machine's engine and app, or inside a docker install). Each run prints one JSON line; --results FILE appends it there.

    .venv/bin/python bench/perf.py project perf_brewline [--scale 0.01]
    .venv/bin/python bench/perf.py pages --project P --sessions 50 [--people N] [--rls TABLE.COLUMN] [--url http://127.0.0.1:8790]
    .venv/bin/python bench/perf.py memory --project P --chats 4 [--url ...] [--app-pid PID]
    .venv/bin/python bench/perf.py load --rows 1000000 [--door path|app] [--url ...]
    .venv/bin/python bench/perf.py isolation --rows 2000000 [--queries 30]

  project    a bench project for pages and memory, made as the baselines' was: Brewline (bench/brewline.py, the
             orders' months in one file) loaded with abi.load: tables, model, starter measures, its Overview report.
  pages      N sessions signed in to the app as throwaway Viewer and Developer accounts open the page at the same
             moment: first, then right after a commit (a probe measure committed through the app; it is deleted after).
             The app keeps page data per person, so --people distinct accounts (default one per session). "first" is
             each person's first page, not a cold one: unrestricted people share the engine's cached results, and the
             owner's page has filled them. --rls makes every account a Viewer who sees only the rows of their own value
             of TABLE.COLUMN (a row-level security rule, removed after), so each person's queries are their own.
             Each round: p50/p95/p99/max ms, errors, dropped connections, and the lake snapshot at its start and end
             (they differ when the worker wrote new query copies during it, which ends every cached result; the run
             first waits for the copies a new model calls for).
  memory     RSS of the app, the engine, the worker, Postgres and the chats' Pi processes (each `pi --mode rpc` the app
             started, with its children; "chats" counts them): idle, then K chats. A chat's Pi starts with its first
             message, so K chats are measured once each has its Pi and, when a model answers, its answer: "answered"
             then, else "started". per_chat_mb only when K Pi processes were measured.
  load       N generated Brewline orders (bench/brewline.py) into a new project: seconds, rows/s, the project's lake
             bytes, seconds until the model is Ready, and whether revenue by region equals the generator's known answers
             to the cent. --door path (default): the engine's add_data on a server path in its files folder, a door
             neither the app nor the agent has; --door app: each table one file through the app's upload, as a person's
             browser sends it (signed in as the owner, as pages is).
  isolation  uncached semantic_query latency in a small project, idle and while N orders load into another.

pages and memory sign in to the app (--url or ABI_PERF_URL) as the workspace owner, ABI_USER (default himanshu), with
ABI_PASSWORD (a docker install has both) or the password in data/accounts.txt (scripts/accounts.py): never on the
command line. They run on a bench project (bench_… or perf_…; --force for another): each commits to it and adds and
removes accounts, so it may have no uncommitted work, and the run commits only its own changes back (the probe's and
the accounts' removal: security/roles.json then lists none of them); anything else changed meanwhile stays
uncommitted and is named in the result. The engine is ABI_URL (abi.py), its data folder ABI_PI_DATA. The accounts,
chats, rules, projects and files a run makes are removed at the end. A docker install keeps the engine and the
processes inside the container, so the bench runs there:

    docker cp bench abi-bench:/app/ && docker exec abi-bench python3 bench/perf.py pages --project perf_brewline --sessions 50
"""
import argparse
import base64
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
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "bench")]
import abi  # noqa: E402
import brewline  # noqa: E402
import settings  # noqa: E402

D = Path(os.environ.get("ABI_PI_DATA", ROOT / "data"))  # scripts/engine.sh's data: pid files, the files folder, the lake
FILES, LAKE = (Path(os.environ.get(v) or D / d) for v, d in (("ABI_FILES_PATH", "files"), ("ABI_DATA_PATH", "lake")))
ACCOUNTS = Path(os.environ.get("ABI_ACCOUNTS", ROOT / "data/accounts.txt"))  # as scripts/creds.py reads it
WAIT = 300  # s: a chat's answer, the app showing a change
TIMEOUT = 900  # s: one request (as long as the app waits for the engine; 1.1 serves 50 people's pages in turn)
ASK = "Reply with the single word: ready."
BENCH = re.compile(r"(bench|perf)\w*")  # bench/bench.py's and this bench's own projects
OURS = {"project.json", "model/measures.json", "security/roles.json", "security/rls.json"}  # what pages and memory change


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


def password(user: str) -> str | None:
    """ABI_PASSWORD (a docker install's owner), else the user's line in data/accounts.txt, as scripts/creds.py signs in."""
    lines = ACCOUNTS.read_text().splitlines() if ACCOUNTS.exists() else []
    return os.environ.get("ABI_PASSWORD") or next((x.split()[-1] for x in lines if re.match(rf"\s*{re.escape(user)}\b", x)), None)


def owner(a, project: str = "") -> App:
    """The workspace owner in the app (Users & access, Commit), on a bench project (--project's by default)."""
    user, project = os.environ.get("ABI_USER", "himanshu"), project or a.project
    if not BENCH.fullmatch(project) and not getattr(a, "force", False):
        raise SystemExit(f"{project} isn't a bench project (bench_… or perf_…): the run commits to it and adds and removes"
                         " accounts. Use a bench project, or --force.")
    if not (pw := password(user)):
        raise SystemExit(f"No password for {user}: set ABI_USER and ABI_PASSWORD (a docker install has both), or keep it in {ACCOUNTS}.")
    o = App(a.url, user, pw, project)
    if re.search(rb'window\.APP=\{"project": "(\w+)"', o.page())[1].decode() != project:
        o.out()
        raise SystemExit(f"The app has no project {project} for {user}.")
    return o


def pending(o: App) -> None:
    """A run commits, and a commit takes every uncommitted change: only the app's own one may wait (opening a project
    turns auto-commit off, in project.json)."""
    if {f["path"] for f in o.post("/api/changes", {"project": o.project})["result"]["files"]} - {"project.json"}:
        raise SystemExit(f"{o.project} has uncommitted changes: commit or undo them first (the bench commits its own changes).")


def tidy(p: str, message: str) -> list:
    """Commit what the run changed back, and nothing of anyone else's: with other files changed meanwhile nothing is
    committed, and those files are returned."""
    files = {f["path"] for f in abi.call("changes", project=p)["files"]}
    if files and not files - OURS:
        abi.call("commit", project=p, message=message)
    return sorted(files - OURS)


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
    """An action on what this run made (its accounts, probe, rule and projects): ABI's confirmation is answered here."""
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


def snapshot(p: str) -> int:
    return abi.call("list_snapshots", project=p, limit=1)["rows"][0][0]


def pages(a) -> dict:
    o, made, sessions, probe, rule, stamp = owner(a), [], [], None, None, time.strftime("%m%d%H%M%S")
    p, people, left = o.project, a.people or a.sessions, []
    try:
        pending(o)
        if not (ms := o.post("/api/semantic_model", {"project": p, "all": True})["result"]["measures"]):
            raise SystemExit(f"{p} has no measure yet: the probe copies one.")
        rec = {"project": p, "sessions": a.sessions, "people": people, "rls": a.rls, "page_kb": len(o.page()) // 1024, "copies": copies(p)}
        creds = accounts(o, ["viewer"] * people if a.rls else [("viewer", "editor")[i % 2] for i in range(people)], made, stamp)
        if a.rls:  # Viewer i sees the rows of the i-th value only (ABI's rule restricts roles below Editor)
            vals = [r[0] for r in abi.call("semantic_query", project=p, measures=[ms[0]["id"]], dimensions=[a.rls])["rows"]]
            ids = {u["name"]: u["id"] for u in abi.call("rls_rules", project=p)["accounts"]}
            rule = f"perf_{stamp}"
            sure("set_rls_rule", project=p, id=rule, table=a.rls.split(".")[0], column=a.rls.split(".")[1],
                 users={ids[n]: [vals[i % len(vals)]] for i, (n, _) in enumerate(creds)})
        with cf.ThreadPoolExecutor(10) as ex:  # each sign-in checks a password hash
            sessions = list(ex.map(lambda i: App(a.url, *creds[i % people], p), range(a.sessions)))
        rec["first"] = {"snapshots": [snapshot(p)], **burst(sessions)}
        rec["first"]["snapshots"].append(snapshot(p))
        probe, msg = f"perf_{stamp}", f"perf.py {stamp}: page check"
        shown(o, o.post("/api/set_measure", {"project": p, "id": probe, "name": "perf.py probe", "expression": ms[0]["expression"]}), probe)
        shown(o, o.post("/api/commit", {"project": p, "message": msg}), msg)
        rec["after_commit"] = {"snapshots": [snapshot(p)], **burst(sessions)}
        rec["after_commit"]["snapshots"].append(snapshot(p))
    finally:  # through ABI itself, so a stopped app leaves nothing behind either
        signed_out(sessions + [o])
        if rule:
            sure("delete_rls_rule", project=p, id=rule)
        if probe and any(m["id"] == probe for m in abi.call("semantic_model", project=p, all=True)["measures"]):
            sure("delete_measure", project=p, id=probe)
        for n in made:
            sure("delete_user", name=n)
        if made or probe:  # the probe and the rule gone, roles.json without the accounts
            left = tidy(p, f"perf.py {stamp}: probe and accounts removed")
    return rec | ({"uncommitted": left} if left else {})


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


def pis(app: int, procs: dict | None = None) -> list:
    """The chats' Pi processes: the app's children running `pi --mode rpc`."""
    return [pid for pid, (parent, _, args) in (procs or ps()).items() if parent == app and "--mode rpc" in args]


def rss(app: int) -> dict:
    """MB and process count of each part, each with its children; the chats' Pi processes are the app's children that
    run `pi --mode rpc`, with their own children ("chats" counts the Pi processes, nothing else the app runs counts)."""
    procs, kids = ps(), defaultdict(list)
    for pid, (parent, _, _) in procs.items():
        kids[parent].append(pid)
    tree = lambda pid: [pid, *(x for k in kids[pid] for x in tree(k))]  # noqa: E731
    pid = lambda f: int((D / f).read_text().split()[0])  # noqa: E731 (engine.sh's pid files, Postgres's own)
    chats = pis(app, procs)
    parts = {"app": [app], "engine": tree(pid("run/app.pid")), "worker": tree(pid("run/worker.pid")),
             "postgres": tree(pid("pg/postmaster.pid")), "pi": [x for c in chats for x in tree(c)]}
    out = {k: {"mb": round(sum(procs[x][1] for x in v if x in procs) / 1024, 1), "n": sum(x in procs for x in v)} for k, v in parts.items()}
    out["pi"]["chats"] = len(chats)
    return out


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
    o, made, chats, stamp, left = owner(a), [], [], time.strftime("%m%d%H%M%S"), []
    s = None
    try:
        pending(o)
        # the model a chat starts with, read as app.py's Hub.chat does: Settings, else the deployment's
        rec["model"] = settings.load()["model"] or os.environ.get("ABI_PI_LLM_MODEL") or None
        s = App(a.url, *accounts(o, ["editor"], made, stamp)[0], o.project)
        s.key, before = o.key, len(pis(app))
        for i in range(a.chats):  # one chat per browser window: each runs its own Pi, started by its first message
            ev = Events(s, f"perf{i}")
            chats.append((f"perf{i}", s.post("/chat/new", {}, f"perf{i}")["id"], ev))
            s.post("/chat", {"message": ASK, "page": "home"}, f"perf{i}")
        answered = lambda: all(ev.answer is not None for _, _, ev in chats)  # noqa: E731
        until(lambda: len(pis(app)) >= before + a.chats or answered())  # a Pi with no model to ask may end at once
        if rec["model"]:
            until(answered)
        rec["answered_by"] = [ev.answer for _, _, ev in chats]  # "": the app's note that no model answered
        rec["answered" if all(rec["answered_by"]) else "started"] = settled(app)  # no model, quota, key: Pi up only
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
        if made:  # roles.json without the account
            left = tidy(o.project, f"perf.py {stamp}: memory account removed")
    # what a chat adds: its Pi and the app's side of it (the engine and Postgres serve everyone, and move with them)
    mine = lambda g: g["app"]["mb"] + g["pi"]["mb"]  # noqa: E731
    rec["per_chat_mb"] = {k: round((mine(rec[k]) - mine(rec["idle"])) / a.chats, 1) for k in ("started", "answered")
                          if k in rec and rec[k]["pi"]["chats"] - rec["idle"]["pi"]["chats"] == a.chats}
    return rec | ({"uncommitted": left} if left else {})


def add(p: str, table: str, location: str) -> dict:
    """The engine's own add_data on a server path in its files folder (abi.call's door takes s3:// Parquet only)."""
    return abi._open("POST", "/api/actions/add_data", {"project": p, "table": table, "location": location}, timeout=6 * 3600)[1]


def upload(o: App, table: str, f: Path) -> dict:
    """A file into a table through the app's Sources form, as a browser sends it: 24 MB pieces to /upload; the app
    hands the whole file to the engine (abi.upload), whose last piece runs add_data."""
    uid, size, n = secrets.token_hex(16), f.stat().st_size, 24 << 20
    with f.open("rb") as fh:
        for off in range(0, max(size, 1), n):
            r = o.post("/upload", {"table": table, "name": f.name, "data": base64.b64encode(fh.read(n)).decode(), "id": uid,
                                   "last": off + n >= size})
    return done(r)


def build(p: str, d: Path, put=None) -> dict:
    """Generated Brewline tables into the new project p, each by `put(table)` (default: a new project and the engine's
    add_data on a server path), then a model as a first load makes it (keys, relationships, calendar, descriptions, a
    revenue measure): seconds per table, seconds until Ready, the project's lake bytes."""
    if not put:
        abi.call("create_project", project=p, name=f"perf {p}")
        put = lambda t: add(p, t, f"{d.relative_to(FILES)}/{t}/*.parquet")  # noqa: E731
    t0, took = time.perf_counter(), {}
    for t in ("cafes", "customers", "products", "orders"):
        t1 = time.perf_counter()
        put(t)
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


def project(a) -> dict:
    if not BENCH.fullmatch(a.name):
        raise SystemExit("Name it bench_… or perf_…: pages and memory measure only bench projects without --force.")
    d = Path(tempfile.mkdtemp(prefix="perf_"))  # outside the engine's data folder, the only place abi.load uploads from
    try:
        known = brewline.generate(d, a.scale)
        duckdb.sql(f"COPY (SELECT * FROM read_parquet('{brewline.q(d)}/orders/*.parquet')) TO '{brewline.q(d)}/orders.parquet' (FORMAT parquet)")
        made = abi.load("demo", a.name, [str(d / f"{t}/{t}.parquet") for t in ("cafes", "customers", "products")] + [str(d / "orders.parquet")])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"project": a.name, "orders": known["rows"]["orders"], "ready": made.get("readiness", {}).get("ready"), "report": made.get("report")}


def brew(rows: int, d: Path) -> dict:
    return brewline.generate(d, rows / brewline.ORDERS)


def load(a) -> dict:
    p = f"perf_{time.strftime('%m%d%H%M%S')}_load"
    d, o, put = FILES / p, None, None
    try:
        known = brew(a.rows, d)
        if a.door == "app":  # a person's door: the app opens the project (its copy, as abi.load makes one), one file a table
            duckdb.sql(f"COPY (SELECT * FROM read_parquet('{brewline.q(d)}/orders/*.parquet')) TO '{brewline.q(d)}/orders.parquet' (FORMAT parquet)")
            abi.new_project("demo", p)
            o = owner(a, p)
            put = lambda t: upload(o, t, d / ("orders.parquet" if t == "orders" else f"{t}/{t}.parquet"))  # noqa: E731
        rec = {"rows": known["rows"]["orders"], "door": a.door, **build(p, d, put)}
        got = abi.call("semantic_query", project=p, measures=["revenue"], dimensions=["cafes.region"])["rows"]
        rec["exact"] = {k: round(v, 2) for k, v in got} == {r: round(sum(y.values()), 2) for r, y in known["revenue_by_region_year"].items()}
        rec["rows_per_s"] = round(rec["rows"] / rec["seconds"])
    finally:
        signed_out([o] if o else [])
        if any(x["project"] == p for x in abi.call("list_projects")["projects"]):
            sure("delete_project", project=p)
        for f in [*abi.WORKSPACES.glob(f"*/{p}"), *abi.WORKSPACES.glob(f"*/_*/{p}")]:  # the copy and the app's folders
            shutil.rmtree(f, ignore_errors=True)
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
    web.add_argument("--project", required=True, help="a bench project (bench_… or perf_…): the run commits to it")
    web.add_argument("--force", action="store_true", help="measure a project that isn't a bench project")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="bench", required=True)
    x = sub.add_parser("project", parents=[common])
    x.add_argument("name", help="bench_… or perf_…")
    x.add_argument("--scale", type=float, default=0.01, help="bench/brewline.py's (0.01: about 74,000 orders, the baselines')")
    x = sub.add_parser("pages", parents=[common, web])
    x.add_argument("--sessions", type=int, default=50)
    x.add_argument("--people", type=int, help="distinct throwaway accounts (default: one per session)")
    x.add_argument("--rls", metavar="TABLE.COLUMN", help="every account a Viewer who sees only its own value's rows")
    x = sub.add_parser("memory", parents=[common, web])
    x.add_argument("--chats", type=int, default=4)
    x.add_argument("--app-pid", type=int, help="the app's process (default: the scripts/app.py serving --url)")
    x = sub.add_parser("load", parents=[common])
    x.add_argument("--rows", type=int, default=1_000_000)
    x.add_argument("--door", choices=("path", "app"), default="path", help="the engine's add_data on a server path, or the app's upload")
    x.add_argument("--url", default=os.environ.get("ABI_PERF_URL", "http://127.0.0.1:8790"), help="the app, for --door app")
    x = sub.add_parser("isolation", parents=[common])
    x.add_argument("--rows", type=int, default=2_000_000, help="orders loaded into the other project")
    x.add_argument("--queries", type=int, default=30, help="idle queries")
    a = ap.parse_args(argv)
    rec = {"bench": a.bench, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           **{"project": project, "pages": pages, "memory": memory, "load": load, "isolation": isolation}[a.bench](a)}
    line = json.dumps(rec)
    print(line, flush=True)
    if a.results:
        with open(a.results, "a") as f:
            f.write(line + "\n")
    return rec


if __name__ == "__main__":
    main()
