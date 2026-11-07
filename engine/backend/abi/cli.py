"""CLI for local use and CI. PRD PC-11, PC-12 (watch), PC-14, PC-2, PC-3.

Every command except bundle/unbundle calls a running app's actions over REST through the SDK (ABI_URL; login
from ABI_TOKEN, or ABI_USER + ABI_PASSWORD (+ ABI_MFA_CODE with two-step sign-in), never arguments, never printed), so roles, confirmations and the
audit log hold as for every other caller (principle 1, §9, D-33). validate and diff are dry runs. watch sends
each settled change of a local clone through `import_project` (D-34: the app never watches its own folder).
Exit codes: 0 ok, 1 rejected, failed test or run, or unreadable input, 2 usage error, 3 refused, server error or not done. CI templates (PC-13): abi/project/ci/.
Run from backend/: `uv run python -m abi.cli --help`.
"""

import argparse
import contextlib
import os
import sys
import time
import urllib.error
from pathlib import Path

from abi import sdk
from abi.project import bundle as b

REFUSED = (401, 403, 404)
SETTLE = 0.5  # watch: seconds between polls; a change is sent once the folder is unchanged for this long


def _fail(code: int, message: str):
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _load(src: Path, project: str | None) -> tuple[dict, str]:
    try:
        if src.is_dir():
            return b.bundle(src), project or src.resolve().name
        files = b.loads(src.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:  # bad JSON, duplicate keys, symlink, non-UTF-8
        _fail(1, f"{src}: {e}")
    if not project:
        _fail(2, "--project is required with a bundle file.")
    return files, project


def _client(url: str) -> sdk.Client:
    if token := os.environ.get("ABI_TOKEN"):
        return sdk.Client(url, token)
    name, password = os.environ.get("ABI_USER"), os.environ.get("ABI_PASSWORD")
    if not (name and password):
        _fail(3, "Set ABI_TOKEN to a session token, or ABI_USER and ABI_PASSWORD (a service account).")
    with _guard(url, login=True):
        return sdk.Client.login(url, name, password, os.environ.get("ABI_MFA_CODE"))


def _error(e: sdk.ActionError) -> tuple[int, str]:
    """Exit code and message: 3 for a refusal (session, role, unknown project) or a server error, else 1."""
    label = "Server error" if e.status >= 500 else "Refused" if e.status in REFUSED else "Failed"
    return 1 if label == "Failed" else 3, f"{label} ({e.status}): {e.body.get('message')}"


@contextlib.contextmanager
def _guard(url: str, login: bool = False):
    """An action error, or an unreachable app, ends the command (never a traceback). A sign-in error never
    prints the body: a 422 echoes the password back (SC-4)."""
    try:
        yield
    except sdk.ActionError as e:
        _fail(3, f"Refused ({e.status}): sign-in as ABI_USER with ABI_PASSWORD failed.") if login else _fail(*_error(e))
    except urllib.error.URLError as e:
        _fail(3, f"Can't reach {url}: {e.reason}")


def _call(c: sdk.Client, action: str, /, **args) -> dict:
    with _guard(c.url):
        return c.call(action, **args)


def _post(c: sdk.Client, body: dict, confirm: str | None = None) -> tuple[int, dict]:
    with _guard(c.url):
        try:
            return 200, c.call("import_project", confirm, **body)
        except sdk.ActionError as e:
            return e.status, e.body


def _import(src: Path, project: str | None, c: sdk.Client, dry_run: bool) -> dict:
    files, project = _load(src, project)
    body = {"project": project, "bundle": files, "dry_run": dry_run}
    status, out = _post(c, body)
    if status == 409 and "confirm_token" in out:  # MA-3 / D-1: a person confirms, never a flag
        try:
            ok = input(f"{out['message']} [y/N]: ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):  # no terminal (CI)
            ok = False
        if not ok:
            _fail(3, "Not confirmed; nothing changed.")
        status, out = _post(c, body, out["confirm_token"])
    if status != 200:
        lines = [f"{'Rejected' if status == 422 else 'Server error' if status >= 500 else 'Refused'} ({status}): {out.get('message')}"]
        for e in out.get("errors") or []:
            where = e.get("file") or ".".join(map(str, e.get("loc", [])))
            lines.append(f"  {where} {e.get('path', '')}: {e.get('error') or e.get('msg')}")
            if e.get("fix"):
                lines.append(f"    fix: {e['fix']}")
        _fail(1 if status == 422 else 3, "\n".join(lines))
    return out


def _show(out: dict) -> None:
    for c in out["changes"] or ["No changes."]:
        print(f"- {c}")
    for k, label in (("refresh", "Tables to refresh"), ("removed", "Tables removed"), ("reports", "Reports affected")):
        if out["impact"][k]:
            print(f"{label}: {', '.join(out['impact'][k])}")


def validate(src: Path, project: str | None, url: str):
    """Check the project against the app's import pipeline without changing anything."""
    out = _import(src, project, _client(url), dry_run=True)
    print(f"Valid: {len(out['changes'])} change(s).")


def diff(src: Path, project: str | None, url: str):
    """Show what an apply would change, in plain language, and the tables it affects."""
    _show(_import(src, project, _client(url), dry_run=True))


def apply(src: Path, project: str | None, url: str):
    """Import the project into the app as one undoable change."""
    _show(_import(src, project, _client(url), dry_run=False))
    print("Applied as one uncommitted change; undo or commit it in the app.")


def make_bundle(folder: Path, output: Path):
    """Write the project folder as one project.bundle.json (PC-2)."""
    try:
        output.write_text(b.dumps(b.bundle(folder)), encoding="utf-8")
    except (ValueError, OSError) as e:
        _fail(1, f"{folder}: {e}")
    print(f"Wrote {output}")


def unbundle(src: Path, folder: Path):
    """Write a project.bundle.json out as a project folder (PC-2). An invalid bundle writes nothing."""
    try:
        b.unbundle(b.loads(src.read_text(encoding="utf-8")), folder)
    except (ValueError, OSError) as e:
        _fail(1, f"{src}: {e}")
    print(f"Wrote {folder}")


def run_tests(project: str, url: str):
    """Run the measure tests, every tool's tests and every custom agent's evaluation set; exit 1 if any fails."""
    c = _client(url)
    if project not in {p["project"] for p in _call(c, "list_projects")["projects"]}:  # an owner would get empty lists
        _fail(3, f"No project '{project}', or you have no role in it.")
    gaps = [g for g in _call(c, "readiness", project=project)["missing"] if g.startswith("Measure '")]
    rows = [("measure", m["id"], [g for g in gaps if g.startswith(f"Measure '{m['id']}' ")])
            for m in _call(c, "semantic_model", project=project)["measures"]]
    for kind, action, key in (("tool", "test_tool", "tests"), ("agent", "test_agent", "cases")):
        for t in _call(c, f"list_{kind}s", project=project)[f"{kind}s"]:
            with _guard(url):
                try:
                    out = c.call(action, project=project, **{kind: t["id"]})
                except sdk.ActionError as e:  # e.g. 409: custom agents need a Ready model; a refusal or 5xx ends the run
                    if (err := _error(e))[0] == 3:
                        _fail(*err)
                    rows.append((kind, t["id"], [e.body.get("message")]))
                    continue
            errors = [r.get("error") or f"{r['question']!r} misses {', '.join(r['missing'])}." for r in out[key] if not r["passed"]]
            rows.append((kind, t["id"], errors or ([] if out["passed"] else [f"No {key}."])))
    if not rows:  # CI must not pass on a project with nothing in it
        _fail(1, f"Project '{project}' has no measures, tools or agents to test.")
    for kind, id, errors in rows:
        print(f"{'FAIL' if errors else 'PASS'}  {kind:<7}  {id}  {' '.join(errors)}".rstrip())
    failed = sum(bool(e) for *_, e in rows)
    print(f"{len(rows) - failed} passed, {failed} failed.")
    if failed:
        raise SystemExit(1)


def run_flow(project: str, flow: str, key: str | None, wait: bool, timeout: float, url: str):
    """Start a flow run now (FL-7)."""
    c = _client(url)
    try:
        with _guard(url):
            r = c.run_flow(project, flow, key, wait, timeout)
    except TimeoutError as e:
        _fail(1, str(e))
    if not wait:
        print(f"Run {r['run']} {'started' if r['started'] else 'was already started with this key'}.")
        return
    print(f"Run {r['id']}: {r['status']}" + (f": {r['error']}" if r.get("error") else ""))
    if r["status"] != "ok":
        raise SystemExit(1)


def docs(project: str, output: Path, url: str):
    """Generate the project's documentation (DC-2) and write its Markdown files into a local folder."""
    files = _call(_client(url), "generate_docs", project=project)["files"]
    try:
        b.unbundle(files, output)  # the same path checks as unbundle: nothing lands outside the folder
    except (ValueError, OSError) as e:
        _fail(1, f"{output}: {e}")
    print(f"Wrote {len(files)} file(s) to {output}")


def _state(folder: Path) -> dict:
    out = {}
    for p in folder.rglob("*"):
        if ".git" not in p.relative_to(folder).parts:
            with contextlib.suppress(OSError):  # deleted while scanning: the next poll sees it gone
                s = p.lstat()
                out[p] = (s.st_mtime_ns, s.st_size)
    return out


def watch(folder: Path, project: str | None, url: str):
    """Hot reload (PC-12): send every saved change of the folder to the app. A valid change applies at once; an
    invalid one prints the file, JSON path and fix and leaves the running project untouched. Ctrl+C stops."""
    c = _client(url)
    print(f"Watching {folder} (Ctrl+C to stop).")
    seen = _state(folder)
    while True:
        # ponytail: polls and stats the whole folder every SETTLE s; an OS file watcher if folders get huge
        time.sleep(SETTLE)
        if (now := _state(folder)) == seen:
            continue
        while True:  # settle: wait until an editor's burst of writes is over
            time.sleep(SETTLE)
            if (later := _state(folder)) == now:
                break
            now = later
        seen = now
        try:
            out = _import(folder, project, c, dry_run=False)
        except SystemExit:  # the error is printed; the project is unchanged
            continue
        print("Applied:")
        _show(out)


def _path(exists: bool = True, file_okay: bool = True, dir_okay: bool = True):
    """A path argument checked as click checks it (usage error, exit 2)."""
    def check(s: str) -> Path:
        p = Path(s)
        if exists and not p.exists():
            raise argparse.ArgumentTypeError(f"Path '{s}' does not exist.")
        if not file_okay and p.is_file():
            raise argparse.ArgumentTypeError(f"File '{s}' is a file.")
        if not dir_okay and p.is_dir():
            raise argparse.ArgumentTypeError(f"Directory '{s}' is a directory.")
        return p
    return check


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m abi.cli")
    sub = parser.add_subparsers(metavar="COMMAND")

    def command(f, name: str | None = None, *args: tuple) -> argparse.ArgumentParser:
        p = sub.add_parser(name or f.__name__.replace("_", "-"), help=" ".join(f.__doc__.split()), description=f.__doc__)
        p.set_defaults(run=f)
        for flags, kw in args:
            p.add_argument(*flags, **kw)
        return p

    src = (("src",), {"type": _path(), "help": "Project folder or project.bundle.json"})
    project = (("--project",), {"help": "Project ID (default: the folder name)"})
    project_id = (("project",), {"help": "Project ID"})
    url = (("--url",), {"default": os.environ.get("ABI_URL", "http://localhost:8000"), "help": "URL of the running app [env var: ABI_URL]"})
    for f in (validate, diff, apply):
        command(f, None, src, project, url)
    command(make_bundle, "bundle", (("folder",), {"type": _path(file_okay=False), "help": "Project folder"}),
            (("--output", "-o"), {"type": Path, "default": Path("project.bundle.json")}))
    command(unbundle, None, (("src",), {"type": _path(dir_okay=False), "help": "project.bundle.json"}),
            (("folder",), {"type": _path(exists=False, file_okay=False), "help": "Folder to write the project files into"}))
    command(run_tests, "test", project_id, url)
    command(run_flow, None, project_id, (("flow",), {"help": "Flow ID"}),
            (("--key",), {"help": "Idempotency key: a retried call starts no second run"}),
            (("--wait",), {"action": argparse.BooleanOptionalAction, "default": False, "help": "Wait until the run ends; exit 1 unless it ends ok"}),
            (("--timeout",), {"type": float, "default": 600, "help": "Seconds to wait"}), url)
    command(docs, None, project_id, (("--output", "-o"), {"type": _path(exists=False, file_okay=False), "default": Path("docs"),
                                                           "help": "Folder for the Markdown files"}), url)
    command(watch, None, (("folder",), {"type": _path(file_okay=False), "help": "Project folder (your clone)"}), project, url)
    args = vars(parser.parse_args(argv))
    if not (run := args.pop("run", None)):  # no command: the help, as a usage error
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    try:
        run(**args)
    except KeyboardInterrupt:  # Ctrl+C (watch): as click ended it
        print("\nAborted!", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
