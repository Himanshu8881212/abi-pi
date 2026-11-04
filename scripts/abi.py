#!/usr/bin/env python3
"""ABI-on-Pi harness helpers: deterministic steps the agent (or a person) runs. Everything goes through ABI's REST
action API as the signed-in person; nothing here reads data files (but the bytes of an upload, and a CSV's first rows
for its quoting) or ABI's folders except `git clone/pull` of the project repository (read-only).

  abi.py health                                  ABI up, signed in, MCP reachable, model proxy reachable
  abi.py workspace <ws>                          new workspace: an empty folder
  abi.py project <ws> <project> [name]           create_project + auto-commit + clone + full empty skeleton
  abi.py load <ws> <project> <folder|file>...    upload every data file, build the model, starter measures + report
  abi.py sync <project>                          git pull ABI's commits into the project folder
  abi.py pack make|list <project> ...            a board pack (scripts/pack.py)
  abi.py call <action> '<json>'                  one action (a 409 is printed as needs-confirmation, never confirmed)
  abi.py cli validate|diff|apply|watch <project> ABI's own CLI on the folder (bundle without .gitkeep)

Token: $ABI_TOKEN (the Pi extension passes its in-memory one), else scripts/creds.py signs in. Output: JSON on stdout.
"""
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACES = Path(os.environ.get("ABI_PI_WORKSPACES", ROOT / "workspaces"))
URL = os.environ.get("ABI_URL", "http://127.0.0.1:8780")
ABI_PROJECTS = Path(os.environ.get("ABI_PROJECTS_PATH", HERE.parent / "data/projects")).expanduser()
ABI_CLI = HERE.parent / "engine/backend"
SCHEMAS = HERE.parent / "engine/schemas/v1"
PROJECT_ID = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SKELETON = ["sources", "tables", "model", "reports", "ml", "flows", "agents", "tools", "security", "environments", "docs"]
DATA = {".csv", ".tsv", ".parquet", ".json", ".jsonl", ".ndjson", ".xlsx", ".avro", ".arrow"}  # what ABI's engine reads
CSV_SAFE = {"quote": '"', "escape": '"', "strict_mode": False}  # demo lesson: quoted fields with "" and line breaks
CHUNK = 64 * 1024**2


class ActionError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(f"{status}: {body.get('message')}")
        self.status, self.body = status, body


_ME = threading.local()  # a signed-in person's ABI session for this request's thread (the app); memory only


@contextlib.contextmanager
def as_user(tok: str | None):
    """Every call in this block is made as that person (their roles, row-level security); None: this process's."""
    prev, _ME.token = getattr(_ME, "token", None), tok
    try:
        yield
    finally:
        _ME.token = prev


def login(name: str, password: str, code: str | None = None) -> tuple[int, dict]:
    """ABI's own sign-in (password, then an MFA code when the account has one): (status, body with the token).
    The password goes to ABI only; nothing here keeps or logs it."""
    body = json.dumps({"name": name, "password": password, **({"code": code} if code else {})}).encode()
    req = urllib.request.Request(URL + "/api/login", body, {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read()).get("detail")
        except ValueError:
            d = None
        if e.code in (401, 403, 429):  # wrong name or password, an MFA step, throttled: ABI's own words
            return e.code, {k: v for k, v in d.items() if k in ("message", "mfa")} if isinstance(d, dict) else {"message": str(d or "Sign-in failed.")}
        return e.code, {"message": "Sign-in failed."}  # never ABI's other bodies: a 422 repeats the password


class _Here(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a redirect goes back to the browser as it is
        return None


def raw(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, bytes, dict]:
    """One of ABI's own web routes (password reset, invite links, SSO): (status, body, headers), redirects not followed.
    The caller passes on only what the browser should see."""
    req = urllib.request.Request(URL + path, None if body is None else json.dumps(body).encode(),
                                 {"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.build_opener(_Here).open(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def logout(tok: str) -> None:
    """Ends that ABI session, so its token stops working (a failure is fine: it may be gone already)."""
    with contextlib.suppress(OSError, ValueError, ActionError), as_user(tok):
        _open("POST", "/api/logout", {})


def token() -> str:
    if t := getattr(_ME, "token", None):
        return t
    if t := os.environ.get("ABI_TOKEN"):
        return t
    t = subprocess.run([sys.executable, str(HERE / "creds.py"), "abi-token"], capture_output=True, text=True, check=True).stdout.strip()
    os.environ["ABI_TOKEN"] = t  # this process only
    return t


def _open(method: str, path: str, body=None, headers: dict | None = None, raw: bytes | None = None, timeout=900):
    h = {"Authorization": f"Bearer {token()}", **(headers or {})}
    data = raw
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(URL + path, data, h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text else {}), dict(r.headers)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            detail = json.loads(text).get("detail", text)
        except ValueError:
            detail = text
        raise ActionError(e.code, detail if isinstance(detail, dict) else {"message": str(detail)}) from None


def artifact(snapshot: str) -> bytes:
    """The bytes of a file ABI rendered (report_snapshot's `artifact:<project>/<file>`), as the person."""
    req = urllib.request.Request(f"{URL}/api/artifacts/{snapshot.removeprefix('artifact:')}", headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return r.read()


def call(action: str, confirm: str | None = None, **args) -> dict:
    """One action as the person (SDK semantics). A 409 with a confirm token raises ActionError: callers show it."""
    return _open("POST", f"/api/actions/{action}", args, {"X-Abi-Confirm": confirm} if confirm else None)[1]


def finished(project: str, out: dict, every: float = 2) -> dict:
    """1.2 M3.2: a big add_data, set_steps or refresh answers at once with {run, status: queued} and loads in ABI's
    worker; this waits for it with a short load_runs call every `every` s (never one call as long as the load): the
    snapshot as the inline answer gives it, or ActionError with the run's error. Any other answer as it is."""
    while isinstance(out, dict) and out.get("run") and out.get("status") in ("queued", "running"):
        time.sleep(every)
        r = call("load_runs", project=project, run=out["run"])["runs"][0]
        if r["status"] == "failed":
            raise ActionError(400, {"message": r["error"], "run": r["run"]})
        out = {**r, "columns": ["snapshot", "rows"], "rows": [[r["snapshot"], r["rows"]]]} if r["status"] == "done" else r
    return out


def upload(project: str, table: str, path: Path, csv: dict | None = None, xlsx: dict | None = None, wait: bool = True) -> dict:
    """IN-7 resumable upload; the last PATCH runs add_data (with these CSV or Excel options) and returns its result,
    a big file's once its run is in (`finished`; wait=False: the queued run, for a caller that shows its progress).
    Bytes go to ABI, never to the LLM."""
    size = path.stat().st_size
    if not size:  # ABI's answer to an empty upload is its validation dump
        raise ActionError(400, {"message": f"{path.name} is empty."})
    name = re.sub(r"[^\w .-]", "_", path.name)
    _, started, _ = _open("POST", f"/api/uploads/{project}/{table}/{urllib.parse.quote(name)}", headers={"Upload-Length": str(size)})
    q = urllib.parse.urlencode({k: json.dumps(v) for k, v in (("csv", csv), ("xlsx", xlsx)) if v})
    off, out = 0, {}
    with path.open("rb") as f:
        while off < size:
            chunk = f.read(CHUNK)
            _, out, hdr = _open("PATCH", f"/api/uploads/{started['id']}" + (f"?{q}" if q else ""), raw=chunk,
                                headers={"Upload-Offset": str(off), "Content-Type": "application/offset+octet-stream"})
            off += len(chunk)
    return finished(project, out) if wait else out


def csv_options(path: Path) -> dict | None:
    """The options a local CSV loads with: CSV_SAFE, standard quoting, so a quoted line break or "" far down the file
    stays one row (DuckDB guesses from the first rows only), unless those rows use another quote or escape (a
    single-quoted or backslash-escaped CSV, which CSV_SAFE misreads without an error): then None, DuckDB's own guess.
    Its sniffer runs in the engine's Python, the DuckDB that loads the file; it reads only those first rows."""
    code = "import duckdb, json, sys; print(json.dumps(duckdb.connect().execute('SELECT Quote, Escape FROM sniff_csv(?)', [sys.argv[1]]).fetchone()))"
    try:
        r = subprocess.run([ABI_CLI / ".venv/bin/python", "-I", "-c", code, path], capture_output=True, text=True, timeout=120, check=True)
        sniffed = set(json.loads(r.stdout))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):  # no sniff: the safe defaults (the load says if they don't fit)
        return CSV_SAFE
    return CSV_SAFE if sniffed <= {'"', "(empty)"} else None


def table_id(path: Path) -> str:
    t = re.sub(r"[^a-z0-9_]", "_", path.stem.lower()).strip("_") or "data"
    return t if t[0].isalpha() else f"t_{t}"


# ---------- workspaces and projects on disk ----------

def folder(project: str) -> Path | None:
    return next((p for p in WORKSPACES.glob(f"*/{project}") if (p / ".git").exists()), None)


def new_workspace(ws: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", ws):
        raise SystemExit(f"Workspace name '{ws}': lowercase letters, digits, - and _, starting with a letter.")
    (WORKSPACES / ws).mkdir(parents=True, exist_ok=True)
    return {"workspace": ws, "folder": str(WORKSPACES / ws)}


def skeleton(dest: Path) -> list[str]:
    """ABI's full layout at once (create_project writes only project.json: an ABI gap). Empty folders get .gitkeep."""
    made = []
    for d in SKELETON:
        (dest / d).mkdir(exist_ok=True)
        if not any(p.name != ".gitkeep" for p in (dest / d).iterdir()) and not (dest / d / ".gitkeep").exists():
            (dest / d / ".gitkeep").write_text("")
            made.append(f"{d}/.gitkeep")
    return made


def git(dest: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(dest), *args], capture_output=True, text=True, check=True).stdout


def new_project(ws: str, project: str, name: str | None = None) -> dict:
    if not PROJECT_ID.fullmatch(project):
        raise SystemExit(f"Project ID '{project}': lowercase letters, digits and _, starting with a letter (no hyphens).")
    new_workspace(ws)
    try:
        call("create_project", project=project, name=name or project.replace("_", " ").title())
        created = True
    except ActionError as e:
        if e.status != 409:
            raise
        created = False  # exists: adopt it
    call("set_auto_commit", project=project, on=True)
    dest = WORKSPACES / ws / project
    if not (dest / ".git").exists():
        subprocess.run(["git", "clone", "-q", str(ABI_PROJECTS / project), str(dest)], check=True)
        git(dest, "config", "pull.rebase", "true")
    made = skeleton(dest)
    if made:
        git(dest, "add", "-A")
        git(dest, "-c", "user.name=abi-pi", "-c", "user.email=abi-pi@localhost", "commit", "-qm", "Harness skeleton (.gitkeep)")
    sync(project)
    return {"project": project, "created": created, "folder": str(dest), "skeleton": made}


def sync(project: str) -> dict:
    """Pull ABI's commits (read-only clone) and re-render the views. Committed files only (auto-commit is on)."""
    dest = folder(project)
    if not dest:
        raise SystemExit(f"No folder for project '{project}' under {WORKSPACES}; create it with `abi.py project`.")
    with copy_lock(dest):
        return _sync(project, dest)


@contextlib.contextmanager
def copy_lock(dest: Path):
    """One reader or writer of a project's copy at a time, across processes (the app's sync after a commit, Publish,
    Export, the agent's abi_sync): two fetches at once fail with "cannot lock ref", and one rewrites FETCH_HEAD
    under the other's reads."""
    with open(dest / ".git" / "abi-sync.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _sync(project: str, dest: Path) -> dict:
    git(dest, "fetch", "-q")
    edited = git(dest, "ls-files", "--others", "--modified", "--exclude-standard").split()
    for rel in dict.fromkeys(edited):  # hand edits ABI has now committed (cli apply): drop ours, take ABI's
        theirs = subprocess.run(["git", "-C", str(dest), "show", f"FETCH_HEAD:{rel}"], capture_output=True, text=True)
        ours = (dest / rel).read_text() if (dest / rel).exists() else None
        same = lambda a, b: json.loads(a) == json.loads(b) if rel.endswith(".json") else a == b  # noqa: E731
        if theirs.returncode == 0 and ours is not None and same(theirs.stdout, ours):
            if git(dest, "ls-files", rel).strip():
                git(dest, "checkout", "--", rel)
            else:
                (dest / rel).unlink()
    git(dest, "pull", "-q", "--rebase")
    skeleton(dest)
    # ABI's last commit (M2 review: HEAD is the local skeleton commit, rebased on ABI's, so it told reports "uncommitted")
    return {"project": project, "folder": str(dest), "head": git(dest, "log", "-1", "--format=%h %s", "@{u}").strip(),
            **({} if os.environ.get("ABI_APP_URL") else {"live": f"python scripts/app.py {project}"})}  # in the app: it is open


# ---------- one-shot build from a folder of files ----------

DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d", "%y.%d.%m", "%y.%m.%d", "%d-%m-%Y",
                "%b %d, %Y", "%d %b %Y"]


def add_date(project: str, table: str) -> dict | None:
    """Readiness needs a fact with a many-to-one to a dimension; a typed date gives build_model a calendar. When the
    table has no DATE/TIMESTAMP column, try each text column whose name says date/day/time against common formats
    through preview_steps (ABI reads the data, 1,000 rows) and add `day` from the format that parses every value."""
    src = [{"type": "source", "source": table}]
    base = call("preview_steps", project=project, steps=src)
    types = {c["name"]: c["type"] for c in base["columns"]}
    if any(t.startswith(("DATE", "TIMESTAMP")) for t in types.values()) or "day" in types:
        return None
    cands = sorted((c for c, t in types.items() if t == "VARCHAR" and re.search(r"date|day|time|_at$|_on$|period|month", c, re.I)),
                   key=lambda c: (not re.search(r"date|day", c, re.I), c))
    if not cands:
        return None
    tries = [(c, f) for c in cands for f in DATE_FORMATS]
    exprs = [f"CAST(try_strptime(\"{c}\", '{f}') AS DATE)" for c, f in tries] + [f"TRY_CAST(\"{c}\" AS DATE)" for c in cands]
    tries += [(c, None) for c in cands]
    r = call("preview_steps", project=project, steps=src + [{"type": "add_column", "name": f"p{i}", "expression": e}
                                                             for i, e in enumerate(exprs)])
    names = [c["name"] for c in r["columns"]]
    rows = r["rows"]
    for i, (c, f) in enumerate(tries):
        vals = [row[names.index(c)] for row in rows if row[names.index(c)] not in (None, "")]
        got = [row[names.index(f"p{i}")] for row in rows if row[names.index(c)] not in (None, "")]
        # %Y also takes "02" (year 2): yy.dd.mm data parsed as dd.mm.yyyy when every sampled day was <= 12
        if vals and sum(g is not None and 1900 <= int(str(g)[:4]) <= 2100 for g in got) >= 0.99 * len(vals):
            steps = src + [{"type": "add_column", "name": "day", "expression": exprs[i]}]
            finished(project, call("set_steps", project=project, table=table, steps=steps))
            return {"column": c, "format": f or "ISO", "expression": exprs[i]}
    return None


def _changes(m: dict, cal: dict) -> list[dict]:
    """M2.3: a SUM's previous period, change and % change as governed measures, so a "why" quotes ABI's figures, never
    its own arithmetic (AGENTS.md rule 3). PREVIOUS_PERIOD steps back one period of the query's finest date grain. The
    % change is a fraction of the period before's size, as reports show percents (-0.25 is -25%). Each stands alone (no
    [<id>_prev]), so a starter SUM that already took one of these IDs leaves the others meaning what they say."""
    i, n, prev = m["id"], m["name"], f"PREVIOUS_PERIOD([{m['id']}])"
    by = f" Group by {cal['table']}.year and {cal['table']}.month (or .quarter), or by {cal['table']}.{cal['column']}."
    # M2 review: ordering by the change put a café that closed last (its change is empty), and a month still filling
    # always showed a fall
    caveat = (" A period still filling is compared, in part, with the whole period before: filter complete periods."
              " A member with no rows in one of the two periods (closed or new) has an empty change: rank members by their"
              " change with explain_change.")
    return [{"id": f"{i}_prev", "name": f"{n}, previous period", "expression": prev,
             "description": f"{n} in the period before each row's: the month before for a month, the year before for a year." + by},
            {"id": f"{i}_change", "name": f"{n} change", "expression": f"[{i}] - {prev}",
             "description": f"{n} minus the period before's; negative is a fall." + by + caveat},
            {"id": f"{i}_change_pct", "name": f"{n} % change", "expression": f"DIVIDE([{i}] - {prev}, ABS({prev}))",
             "description": f"{n}'s change as a fraction of the period before (-0.25 is a 25% fall); empty when that is 0." + by + caveat}]


def _measures(project: str, sm: dict | None = None) -> list[dict]:
    """Starter measures from detection: a row count per fact table and a SUM per numeric non-key fact column. Given the
    semantic model `sm`, each SUM of a fact related straight to the date table also gets `_changes`, after all the
    others: the starter report's first measures become KPIs, and a KPI has no period to step back from."""
    det = call("detect_model", project=project)
    cal = (sm or {}).get("date_table") or {}
    dated = {r["from"]["table"] for r in (sm or {}).get("relationships", []) if r["to"]["table"] == cal.get("table")}
    out, timed = [], []
    for t, info in (det.get("tables") or {}).items():
        if info.get("kind") != "fact":
            continue
        cols = info.get("columns") or {}
        key = info.get("key") or next(iter(cols), None)
        if key:
            out.append({"id": f"{t}_rows", "name": f"{t.replace('_', ' ').title()} rows", "expression": f"COUNT({t}.{key})",
                        "description": f"Rows of {t}."})
        for c, typ in cols.items():
            dt = typ if isinstance(typ, str) else (typ or {}).get("type", "")
            if re.match(r"(BIG|SMALL|TINY|HUGE)?INT|DOUBLE|FLOAT|DECIMAL|REAL|NUMERIC|U?INTEGER", str(dt).upper()) \
                    and not re.search(r"(^|_)(id|key|code|zip|year|month|day)$", c.lower()) and c != key:
                out.append({"id": f"{c}" if not any(m["id"] == c for m in out) else f"{t}_{c}",
                            "name": c.replace("_", " ").title(), "expression": f"SUM({t}.{c})",
                            "description": f"Sum of {t}.{c}."})
                if t in dated:
                    timed += _changes(out[-1], cal)
    return out + timed


def load(ws: str, project: str, paths: list[str]) -> dict:
    """Drop-in build: project (+skeleton), every data file loaded, model built, starter measures and report, views."""
    files = []
    for p in map(lambda s: Path(s).expanduser().resolve(), paths):
        files += sorted(f for f in (p.rglob("*") if p.is_dir() else [p]) if f.is_file() and f.suffix.lower() in DATA)
    if not files:
        raise SystemExit("No data files (csv, tsv, parquet, json, ndjson, xlsx, avro, arrow) found there.")
    for f in files:
        guard(f)
    log = {"project": new_project(ws, project), "loaded": [], "failed": []}
    for f in files:
        t = table_id(f)
        t0 = time.time()
        try:
            csv = csv_options(f) if f.suffix.lower() in (".csv", ".tsv") else None
            try:  # demo lesson: DuckDB's guess splits quoted multi-line fields, so CSVs load with explicit quoting first
                r = upload(project, t, f, csv)
            except ActionError:
                if not csv:
                    raise
                r = upload(project, t, f)
            log["loaded"].append({"table": t, "file": f.name, "snapshot": r.get("snapshot"), "seconds": round(time.time() - t0, 1)})
        except ActionError as e:
            log["failed"].append({"table": t, "file": f.name, "error": e.body.get("message")})
    if not log["loaded"]:
        return log
    for x in log["loaded"]:
        try:
            if d := add_date(project, x["table"]):
                x["date"] = d
        except ActionError as e:
            x["date_error"] = e.body.get("message")
    call("build_model", project=project)
    told = {x["table"]: f"Loaded from the file {x['file']}." for x in log["loaded"]}
    for t, info in (call("semantic_model", project=project).get("tables") or {}).items():
        if not info.get("description"):  # readiness needs one per table; the Documenter can improve them later
            call("set_table", project=project, table=t, description=told.get(t) or (
                "Calendar of every day, generated by build_model for time intelligence." if t == "calendar" else f"Table {t}."))
    sm = call("semantic_model", project=project, all=True)
    have = {m.get("id") for m in sm.get("measures", []) if isinstance(m, dict)}
    log["measures"] = []
    for m in _measures(project, None if have else sm):  # change measures in a project's first build: others stay as they are
        if m["id"] in have:
            continue
        have.add(m["id"])  # a SUM of a column named revenue_prev keeps its ID; revenue's previous period gives way
        try:
            call("set_measure", project=project, **m)
            log["measures"].append(m["id"])
        except ActionError as e:
            log.setdefault("measure_errors", []).append({m["id"]: e.body.get("message")})
    log["readiness"] = call("readiness", project=project)
    try:
        log["report"] = starter_report(project)
    except ActionError as e:
        log["report_error"] = e.body.get("message")
    log["sync"] = sync(project)
    return log


def starter_report(project: str) -> str:
    """One overview page: a KPI per measure (up to 4), a bar by the first text dimension, a line by day if dated."""
    sm = call("semantic_model", project=project)
    names = {m["id"]: m.get("name") or m["id"] for m in sm.get("measures", []) if isinstance(m, dict)
             and not re.search(r"\b(YTD|QTD|MTD|PREVIOUS_YEAR|PREVIOUS_PERIOD|ROLLING)\s*\(", m.get("expression") or "", re.I)}
    measures = list(names)[:4]  # a KPI has no date to step back from
    if not measures:
        raise ActionError(400, {"message": "No measures to report."})
    cols, kinds = sm.get("columns") or {}, {t: i.get("kind") for t, i in (sm.get("tables") or {}).items()}
    day = "calendar.day" if "day" in cols.get("calendar", {}) else None
    good = r"categor|country|region|segment|type|status|group|channel|brand|product|store|city|state"
    dims = sorted((f"{t}.{c}" for t in cols if t != "calendar" for c, typ in cols[t].items()
                   if (typ == "VARCHAR" and not re.search(r"(^|_)(id|key|link|url|description|tags|title|time|date)$", c.lower()))
                   or (re.search(good, c, re.I) and re.match(r"(BIG|SMALL|TINY)?INT", typ))),
                  key=lambda d: (kinds.get(d.split(".")[0]) != "dimension", not re.search(good, d, re.I), d))
    visuals = [{"id": f"kpi-{m}", "type": "kpi", "title": names[m],
                "query": {"measures": [m], "dimensions": []}, "layout": {"x": 3 * i, "y": 0, "w": 3, "h": 3}}
               for i, m in enumerate(measures)]
    main = next((m for m in measures if not m.endswith("_rows")), measures[0])  # a sum reads better than a count
    if dims:
        visuals.append({"id": "by-dim", "type": "bar", "title": f"{names[main]} by {dims[0].split('.')[1].replace('_', ' ')}",
                        "query": {"measures": [main], "dimensions": [dims[0]], "limit": 20,
                                  "order_by": [{"field": main, "desc": True}]},
                        "layout": {"x": 0, "y": 3, "w": 6, "h": 5}})
    if day:
        visuals.append({"id": "by-day", "type": "line", "title": f"{names[main]} per day",
                        "query": {"measures": [main], "dimensions": [day]}, "layout": {"x": 6, "y": 3, "w": 6, "h": 5}})
    report = {"id": "overview", "name": "Overview", "pages": [{"id": "overview", "name": "Overview", "visuals": visuals}]}
    try:
        call("save_report", project=project, report=report)
    except ActionError:  # an order_by/limit shape this ABI refuses: drop them, keep the visual
        for v in visuals:
            v["query"].pop("order_by", None)
            v["query"].pop("limit", None)
        call("save_report", project=project, report=report)
    return "overview"


# ---------- guards and checks ----------

DENY = [HERE.parent / "data", HERE.parent / "engine", Path("~/abi-preview").expanduser(), Path("~/.pi").expanduser(), Path("~/.ssh").expanduser()]


def guard(path: Path) -> None:
    """Uploads stream a person's file to ABI; never ABI's own lake/files/projects, credentials or dotfiles."""
    p = path.resolve()
    if any(p == d or d in p.parents for d in DENY) or re.search(r"credential|accounts\.txt|secret|\.env$|\.key$|\.pw$", p.name, re.I):
        raise SystemExit(f"Refused: {p} is not a data file the harness may upload.")


def health() -> dict:
    out = {}
    try:
        with urllib.request.urlopen(URL + "/api/health", timeout=5) as r:
            out["abi"] = json.load(r).get("status")
    except Exception as e:  # noqa: BLE001
        out["abi"] = f"down: {type(e).__name__}"
        return out
    out["signed_in"] = bool(call("list_projects"))
    init = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        _, res, _ = _open("POST", "/api/mcp", init, {"Accept": "application/json, text/event-stream",
                                                     "MCP-Protocol-Version": "2025-06-18"})
        out["mcp_tools"] = len(res.get("result", {}).get("tools", []))
    except ActionError as e:
        out["mcp_tools"] = f"error {e.status}"
    try:
        base = subprocess.run([sys.executable, str(HERE / "creds.py"), "llm-base"], capture_output=True, text=True).stdout.strip()
        key = subprocess.run([sys.executable, str(HERE / "creds.py"), "llm-key"], capture_output=True, text=True).stdout.strip()
        with urllib.request.urlopen(urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {key}"}), timeout=10) as r:
            out["model_gpt_6_1_sol"] = any(m["id"] == "gpt-6.1-sol" for m in json.load(r)["data"])
    except Exception as e:  # noqa: BLE001
        out["model_gpt_6_1_sol"] = f"unreachable: {type(e).__name__}"
    return out


def validate_files(dest: Path) -> list[str]:
    """Every JSON file in a project folder against ABI's published schema (schemas/v1/<type>.json)."""
    import jsonschema  # noqa: PLC0415 (.venv only)

    kinds = {r"project\.json": "project", r"sources/[^/]+\.json": "source", r"tables/[^/]+\.json": "table",
             r"model/(model|glossary|measures|datasets)\.json": None, r"security/(roles|rls)\.json": None,
             r"reports/[^/]+\.json": "report", r"tools/[^/]+\.json": "tool", r"docs/descriptions\.json": "descriptions",
             r"flows/[^/]+\.json": "flow", r"agents/[^/]+\.eval\.json": "eval", r"agents/[^/]+\.json": "agent",
             r"ml/[^/]+\.json": "ml", r"environments/[^/]+\.json": "environment"}
    errs = []
    for f in sorted(dest.rglob("*.json")):
        rel = f.relative_to(dest).as_posix()
        if rel.startswith(".git/"):
            continue
        kind = next(((k or Path(rel).stem) for p, k in kinds.items() if re.fullmatch(p, rel)), None)
        if not kind:
            errs.append(f"{rel}: no schema for this path")
            continue
        schema = json.loads((SCHEMAS / f"{kind}.json").read_text())
        for e in jsonschema.Draft202012Validator(schema).iter_errors(json.loads(f.read_text())):
            errs.append(f"{rel} {e.json_path}: {e.message[:200]}")
    return errs


def cli(cmd: str, project: str) -> int:
    """ABI's CLI (`abi validate|diff|apply|watch`) on the folder, sent as a bundle without the .gitkeep files."""
    dest = folder(project)
    if not dest:
        raise SystemExit(f"No folder for project '{project}'.")
    env = {**os.environ, "ABI_URL": URL, "ABI_TOKEN": token(), "PYTHONDONTWRITEBYTECODE": "1"}
    py = [str(ABI_CLI / ".venv/bin/python"), "-m", "abi.cli"]

    def bundle_file() -> Path:
        files = {}
        for p in sorted(dest.rglob("*")):
            rel = p.relative_to(dest).as_posix()
            if p.is_file() and ".git" not in p.relative_to(dest).parts and p.name not in (".gitkeep", "app.html"):
                files[rel] = json.loads(p.read_text()) if rel.endswith(".json") else p.read_text()
        out = ROOT / "scratch" / f"{project}.bundle.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(files))
        return out

    def run(c: str) -> int:
        return subprocess.run([*py, c, str(bundle_file()), "--project", project], cwd=ABI_CLI, env=env).returncode

    if cmd != "watch":
        return run(cmd)
    # ponytail: polls every second like ABI's own watch; ABI's watch can't take the folder itself (.gitkeep is refused)
    seen = None
    print(f"Watching {dest} (Ctrl+C to stop).", flush=True)
    while True:
        now = sorted((str(p), p.stat().st_mtime_ns) for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts)
        if seen is not None and now != seen:
            run("apply")
        seen = now
        time.sleep(1)


def main(argv: list[str]) -> None:
    sys.path.insert(0, str(HERE))
    if not argv:
        raise SystemExit(__doc__)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "health":
            out = health()
        elif cmd == "workspace":
            out = new_workspace(args[0])
        elif cmd == "project":
            out = new_project(args[0], args[1], " ".join(args[2:]) or None)
        elif cmd == "load":
            out = load(args[0], args[1], args[2:])
        elif cmd == "sync":
            out = sync(args[0])
        elif cmd == "validate-files":
            out = {"errors": validate_files(folder(args[0]) or Path(args[0]))}
        elif cmd == "pack":
            import pack  # noqa: PLC0415 (sibling script)
            return pack.main(argv[1:])
        elif cmd == "call":
            out = call(args[0], **json.loads(args[1] if len(args) > 1 else "{}"))
        elif cmd == "cli":
            raise SystemExit(cli(args[0], args[1]))
        else:
            raise SystemExit(__doc__)
    except ActionError as e:
        body = dict(e.body)
        if body.pop("confirm_token", None):  # never printed: only a person's yes in the harness uses it
            body["needs_confirmation"] = True
        out = {"error": e.status, **body}
    print(json.dumps(out, default=str, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
