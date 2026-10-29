"""CLI for local use and CI. PRD PC-11, PC-12 (watch), PC-14, PC-2, PC-3.

Every command except bundle/unbundle calls a running app's actions over REST through the SDK (ABI_URL; login
from ABI_TOKEN, or ABI_USER + ABI_PASSWORD (+ ABI_MFA_CODE with two-step sign-in), never arguments, never printed), so roles, confirmations and the
audit log hold as for every other caller (principle 1, §9, D-33). validate and diff are dry runs. watch sends
each settled change of a local clone through `import_project` (D-34: the app never watches its own folder).
Exit codes: 0 ok, 1 rejected, failed test or run, or unreadable input, 2 usage error, 3 refused, server error or not done. CI templates (PC-13): abi/project/ci/.
Run from backend/: `uv run python -m abi.cli --help`.
"""

import contextlib
import os
import time
import urllib.error
from pathlib import Path
from typing import Annotated

import typer

from abi import sdk
from abi.project import bundle as b

cli = typer.Typer(no_args_is_help=True, add_completion=False)
Src = Annotated[Path, typer.Argument(exists=True, help="Project folder or project.bundle.json")]
Project = Annotated[str | None, typer.Option(help="Project ID (default: the folder name)")]
ProjectId = Annotated[str, typer.Argument(help="Project ID")]
Url = Annotated[str, typer.Option(envvar="ABI_URL", help="URL of the running app")]
REFUSED = (401, 403, 404)
SETTLE = 0.5  # watch: seconds between polls; a change is sent once the folder is unchanged for this long


def _fail(code: int, message: str):
    typer.echo(message, err=True)
    raise typer.Exit(code)


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
            ok = typer.confirm(out["message"], default=False)
        except typer.Abort:  # no terminal (CI)
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
        typer.echo(f"- {c}")
    for k, label in (("refresh", "Tables to refresh"), ("removed", "Tables removed"), ("reports", "Reports affected")):
        if out["impact"][k]:
            typer.echo(f"{label}: {', '.join(out['impact'][k])}")


@cli.command()
def validate(src: Src, project: Project = None, url: Url = "http://localhost:8000"):
    """Check the project against the app's import pipeline without changing anything."""
    out = _import(src, project, _client(url), dry_run=True)
    typer.echo(f"Valid: {len(out['changes'])} change(s).")


@cli.command()
def diff(src: Src, project: Project = None, url: Url = "http://localhost:8000"):
    """Show what an apply would change, in plain language, and the tables it affects."""
    _show(_import(src, project, _client(url), dry_run=True))


@cli.command()
def apply(src: Src, project: Project = None, url: Url = "http://localhost:8000"):
    """Import the project into the app as one undoable change."""
    _show(_import(src, project, _client(url), dry_run=False))
    typer.echo("Applied as one uncommitted change; undo or commit it in the app.")


@cli.command("bundle")
def make_bundle(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Project folder")],
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("project.bundle.json"),
):
    """Write the project folder as one project.bundle.json (PC-2)."""
    try:
        output.write_text(b.dumps(b.bundle(folder)), encoding="utf-8")
    except (ValueError, OSError) as e:
        _fail(1, f"{folder}: {e}")
    typer.echo(f"Wrote {output}")


@cli.command()
def unbundle(
    src: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="project.bundle.json")],
    folder: Annotated[Path, typer.Argument(file_okay=False, help="Folder to write the project files into")],
):
    """Write a project.bundle.json out as a project folder (PC-2). An invalid bundle writes nothing."""
    try:
        b.unbundle(b.loads(src.read_text(encoding="utf-8")), folder)
    except (ValueError, OSError) as e:
        _fail(1, f"{src}: {e}")
    typer.echo(f"Wrote {folder}")


@cli.command("test")
def run_tests(project: ProjectId, url: Url = "http://localhost:8000"):
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
        typer.echo(f"{'FAIL' if errors else 'PASS'}  {kind:<7}  {id}  {' '.join(errors)}".rstrip())
    failed = sum(bool(e) for *_, e in rows)
    typer.echo(f"{len(rows) - failed} passed, {failed} failed.")
    if failed:
        raise typer.Exit(1)


@cli.command()
def run_flow(
    project: ProjectId,
    flow: Annotated[str, typer.Argument(help="Flow ID")],
    key: Annotated[str | None, typer.Option(help="Idempotency key: a retried call starts no second run")] = None,
    wait: Annotated[bool, typer.Option(help="Wait until the run ends; exit 1 unless it ends ok")] = False,
    timeout: Annotated[float, typer.Option(help="Seconds to wait")] = 600,
    url: Url = "http://localhost:8000",
):
    """Start a flow run now (FL-7)."""
    c = _client(url)
    try:
        with _guard(url):
            r = c.run_flow(project, flow, key, wait, timeout)
    except TimeoutError as e:
        _fail(1, str(e))
    if not wait:
        typer.echo(f"Run {r['run']} {'started' if r['started'] else 'was already started with this key'}.")
        return
    typer.echo(f"Run {r['id']}: {r['status']}" + (f": {r['error']}" if r.get("error") else ""))
    if r["status"] != "ok":
        raise typer.Exit(1)


@cli.command()
def docs(
    project: ProjectId,
    output: Annotated[Path, typer.Option("--output", "-o", file_okay=False, help="Folder for the Markdown files")] = Path("docs"),
    url: Url = "http://localhost:8000",
):
    """Generate the project's documentation (DC-2) and write its Markdown files into a local folder."""
    files = _call(_client(url), "generate_docs", project=project)["files"]
    try:
        b.unbundle(files, output)  # the same path checks as unbundle: nothing lands outside the folder
    except (ValueError, OSError) as e:
        _fail(1, f"{output}: {e}")
    typer.echo(f"Wrote {len(files)} file(s) to {output}")


def _state(folder: Path) -> dict:
    out = {}
    for p in folder.rglob("*"):
        if ".git" not in p.relative_to(folder).parts:
            with contextlib.suppress(OSError):  # deleted while scanning: the next poll sees it gone
                s = p.lstat()
                out[p] = (s.st_mtime_ns, s.st_size)
    return out


@cli.command()
def watch(
    folder: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Project folder (your clone)")],
    project: Project = None,
    url: Url = "http://localhost:8000",
):
    """Hot reload (PC-12): send every saved change of the folder to the app. A valid change applies at once; an
    invalid one prints the file, JSON path and fix and leaves the running project untouched. Ctrl+C stops."""
    c = _client(url)
    typer.echo(f"Watching {folder} (Ctrl+C to stop).")
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
        except typer.Exit:  # the error is printed; the project is unchanged
            continue
        typer.echo("Applied:")
        _show(out)


if __name__ == "__main__":
    cli()
