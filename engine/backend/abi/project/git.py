"""Built-in Git: each project is a pygit2 repository. PRD §5 #4, PC-5 (repo), PC-6 (uncommitted changes).

Definition actions (`@action(..., definition=True)`) edit files in the project's working tree;
`run()` wraps them in `change()`, so each lands as an uncommitted change. Data actions (refresh,
restore, write-back) never touch the repo; they are undone via snapshots and WB-8 (V-02 C-4).

Undo chain: `refs/abi/undo` points to a linear chain of commits, off every branch, one per
definition action. Its root holds the working tree before the first recorded action; each child
holds the tree after one action (message = action name, author = user or agent). Undo checks the
tree out of the parent and moves the ref back. The chain is the proposed commit message (PC-6);
committing deletes it, so undo covers only uncommitted changes and committed ones are reverted.

M-07: changes, commit, history, diff between any two versions, revert to a version (PC-5, PC-7, UI-4).
"""

import datetime
import fcntl
import functools
import json
import os
import re
import shutil
import time
import zoneinfo
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import pygit2
from pydantic import AfterValidator, BaseModel, Field, field_validator
from pygit2.enums import CheckoutStrategy, ConfigLevel, SortMode

from abi import actions, storage
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import SCHEMA, bundle
from abi.project.bundle import dumps
from abi.security import Role, User, role_in

UNDO_REF = "refs/abi/undo"
REV = r"^(HEAD|[0-9a-f]{4,40})$"  # a version: HEAD or a (short) commit id
ID = storage.PROJECT  # project IDs are directory names (no path traversal) and the lake's schema and folder name
BRANCH = r"^[a-z0-9][a-z0-9_-]{0,62}$"  # branch = environments/<branch>.json file name


def _root() -> Path:
    return Path(os.environ.get("ABI_PROJECTS_PATH", "/data/projects"))


def projects() -> list[str]:
    """The project repos on disk with a valid project ID, sorted."""
    return sorted(g.parent.name for g in _root().glob("*/.git") if re.fullmatch(storage.PROJECT, g.parent.name))


def bad_id(project: str) -> str | None:
    """Why `project` can't be a project ID, with a fix (X-04-fix-4); None when it can."""
    if re.fullmatch(ID, project):
        return None
    fix = re.sub(r"[^a-z0-9_]", "_", project.lower())
    fix = (fix if re.match("[a-z]", fix) else "p_" + fix)[:63]
    return (f"Project ID '{project}' isn't allowed: use a lowercase letter first, then lowercase letters, digits or _ "
            f"(at most 63, pattern {ID}; it names the project's data lake). Try '{fix}'.")


def path(project: str) -> Path:
    if why := bad_id(project):
        raise ActionFailed(422, why)
    return _root() / project


def settings(project: str) -> dict:
    """project.json `settings`, unchecked: callers that need the schema validate the file themselves."""
    return json.loads((path(project) / "project.json").read_text()).get("settings", {})


def repo(project: str) -> pygit2.Repository:
    p = path(project)
    if not (p / ".git").is_dir():
        raise ActionFailed(404, f"Project '{project}' does not exist.")
    for level in (ConfigLevel.SYSTEM, ConfigLevel.XDG, ConfigLevel.GLOBAL, ConfigLevel.PROGRAMDATA):
        pygit2.settings.search_path[level] = os.devnull  # D-34: a repo opens without the host's git config (insteadOf, proxy)
    return pygit2.Repository(p)


@contextmanager
def locked(project: str, p: Path | None = None):
    """Hold the project repo for a write: threads by its project's lock (M3.1: other projects' actions go on),
    processes (app, worker, scripts) by a flock on a file in its git dir (DEMO-1.11-fix-1: libgit2 fails "the index is
    locked" instead of waiting). Not reentrant."""
    p = p or path(project)
    if not (p / ".git").is_dir():
        raise ActionFailed(404, f"Project '{project}' does not exist.")
    with storage.project_lock("git", project), open(p / ".git" / "abi.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # released when the file closes, also when the process dies
        yield


def _sig(user: User) -> pygit2.Signature:
    """UI-4 author (user or agent): changes made through the Main Agent are signed as the agent."""
    if actions.by_agent():
        who = "MCP agent" if actions.VIA.get() == "mcp" else "Main Agent"
        return pygit2.Signature(f"{who} for {user.name}", f"agent-{user.id}@abi.local")
    return pygit2.Signature(user.name, f"user-{user.id}@abi.local")


def _worktree(r: pygit2.Repository, write: bool = True) -> pygit2.Oid:
    """Stage the whole working tree (adds, edits, deletions) and return its tree id. Without `write` the index file
    stays as it is (a read: no repo lock needed; tree objects go to the object store, whose writes are atomic)."""
    idx = r.index
    idx.clear()
    idx.add_all()
    if write:
        idx.write()
    return idx.write_tree()


def _checkout(r: pygit2.Repository, tree: pygit2.Oid) -> None:
    r.checkout_tree(r[tree], strategy=CheckoutStrategy.FORCE | CheckoutStrategy.REMOVE_UNTRACKED)


def _top(r: pygit2.Repository) -> pygit2.Commit | None:
    ref = r.references.get(UNDO_REF)
    return ref.peel(pygit2.Commit) if ref else None


@contextmanager
def change(project: str, user: User, name: str):
    """Record the action's file edits as one undoable step; roll them back if the action fails."""
    with locked(project):
        r = repo(project)
        before, head = _worktree(r), r.head.target
        try:
            yield
        except BaseException:
            _checkout(r, before)
            raise
        after = _worktree(r)
        if r.head.target != head:  # the action committed (PC-8 pull): committed changes are reverted, not undone
            if ref := r.references.get(UNDO_REF):
                ref.delete()
        elif after == before:
            return
        else:
            top = _top(r)
            if top is None or top.tree_id != before:  # new chain: first action, or files changed since
                top = r[r.create_commit(None, _sig(user), _sig(user), "before", before, [])]
            r.references.create(UNDO_REF, r.create_commit(None, _sig(user), _sig(user), name, after, [top.id]), force=True)
            if _auto_commit(r) or production(project):  # D-60: production keeps its access changes committed
                _commit(r, user, _proposal(r))
    from abi import docs  # not at top: docs imports this module

    docs.notify_outdated(project)  # §5 proactive help: descriptions whose definition just changed


def _auto_commit(r: pygit2.Repository) -> bool:
    try:
        return json.loads((Path(r.workdir) / "project.json").read_text())["settings"]["auto_commit"] is True
    except (OSError, ValueError, KeyError, TypeError):  # no or unreadable project.json: off
        return False


RLS = "security/rls.json"
HIDDEN = "Row-level security rules are hidden from Viewers."


GLOSSARY = "model/glossary.json"
GLOSSARY_HIDDEN = "Glossary changes are hidden under row-level security."


def _diff(r: pygit2.Repository, a: pygit2.Oid, b: pygit2.Oid, hide: dict | None = None) -> list[dict]:
    """Readable diff from tree `a` to tree `b`: per file its path, status (A/M/D) and unified patch. `hide`: {path: text
    shown instead of its patch} (`_hidden`)."""
    hide = hide or {}
    return [{"path": p.delta.new_file.path, "status": p.delta.status_char(),
             "patch": next((hide[x] for x in (p.delta.old_file.path, p.delta.new_file.path) if x in hide), p.text)}
            for p in r[a].diff_to_tree(r[b])]


def _hidden(conn, user: User, project: str) -> dict:
    """SE-6/D-42: a Viewer sees that the row-level security rules changed, never their values (other users' allowed
    rows); H-01.4-fix-5 (D-47): a restricted Viewer gets no glossary patch, whose agent-written text may quote rows."""
    from abi.semantic import rls  # semantic imports this module

    out = {RLS: HIDDEN} if (role_in(conn, user, project) or Role.viewer) < Role.editor else {}
    return out | ({GLOSSARY: GLOSSARY_HIDDEN} if rls.restricted(project, user.id) else {})


def _proposal(r: pygit2.Repository) -> str:
    """PC-6 proposed commit message: one line per action in the undo chain, oldest first."""
    lines, c = [], _top(r)
    while c is not None and c.parents:
        lines.append(f"{c.message}: {', '.join(d['path'] for d in _diff(r, c.parents[0].tree_id, c.tree_id))}")
        c = c.parents[0]
    return "\n".join(reversed(lines))


def _commit(r: pygit2.Repository, user: User, message: str) -> str | None:
    """Commit the working tree to the branch; None when it has no changes."""
    head = r.head.peel(pygit2.Commit)
    tree = _worktree(r)
    if tree == head.tree_id:
        return None
    oid = r.create_commit("HEAD", _sig(user), _sig(user), message, tree, [head.id])
    if ref := r.references.get(UNDO_REF):
        ref.delete()  # committed changes are reverted, not undone
    return str(oid)


def _version(r: pygit2.Repository, rev: str) -> pygit2.Commit:
    """A commit on the project's history (never an undo-chain commit)."""
    head = r.head.target
    try:
        c = r.revparse_single(rev).peel(pygit2.Commit)
    except (KeyError, ValueError, pygit2.GitError):
        c = None
    if c is None or (c.id != head and not r.descendant_of(head, c.id)):
        raise ActionFailed(404, f"No version '{rev}' in this project's history.")
    return c


class NewProject(BaseModel):
    project: str = Field(pattern=ID)
    name: str = Field(min_length=1)
    tenant: str | None = None  # the tenant the project belongs to (SC-9); None: a platform project

    @field_validator("project", mode="before")
    @classmethod
    def _hint(cls, v):  # runs before the pattern check, so the 422 carries a fix
        if isinstance(v, str) and (why := bad_id(v)):
            raise ValueError(why)
        return v


class ProjectRef(BaseModel):
    project: str


@action(Role.owner)  # no role in a project that doesn't exist yet: only workspace owners pass
def create_project(conn, user: User, args: NewProject) -> dict:
    """Create a project: a Git repository whose first commit holds project.json (§6.6)."""
    p = path(args.project)
    if p.exists():
        raise ActionFailed(409, f"Project '{args.project}' already exists.")
    if args.tenant and not conn.execute("INSERT INTO project_tenants SELECT %s, id FROM tenants WHERE id = %s",
                                        [args.project, args.tenant]).rowcount:
        raise ActionFailed(400, f"No tenant '{args.tenant}'.")
    r = pygit2.init_repository(p, initial_head="main")
    (p / "project.json").write_bytes(dumps({"$schema": SCHEMA + "project.json", "name": args.name, "schema_version": 1}).encode())
    r.create_commit("HEAD", _sig(user), _sig(user), "Create project", _worktree(r), [])
    return {"project": args.project}


class NoInput(BaseModel):
    pass


@action(Role.viewer)  # workspace scope: every account; the list itself is filtered by project role (SC-2)
def list_projects(conn, user: User, args: NoInput) -> dict:
    """Lists the projects you have a role in, with their loaded tables and input tables. Use it to find which project to work on."""
    from abi import transform  # not at top: it imports this module

    names = _folders()  # projects() hides invalid IDs; listed here to rename
    names |= {r["project"] for r in conn.execute("SELECT project FROM project_roles UNION SELECT project FROM sources")}
    tables = conn.execute("SELECT project, table_name FROM sources").fetchall()
    return {"projects": [
        {"project": p, "rename_needed": why} if (why := bad_id(p)) else
        {"project": p, "tables": sorted({t["table_name"] for t in tables if t["project"] == p} | transform.inputs(p)),
         "production": production(p)}
        for p in sorted(names) if role_in(conn, user, p) is not None
    ]}


def _folders() -> set[str]:
    """Every project folder, also one with an invalid ID (X-04-fix-4)."""
    return {g.parent.name for g in _root().glob("*/.git")}


def _existing(project: str) -> Path:
    if project not in _folders():  # also keeps the name inside the projects root (no traversal)
        raise ActionFailed(404, f"Project '{project}' does not exist.")
    return _root() / project


def _keyed(conn) -> list[str]:
    """Every Postgres table holding rows per project, but the SC-6 audit log and the SC-11 usage ledger, never rewritten."""
    return [r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.columns WHERE table_schema = current_schema()"
        " AND column_name = 'project' AND table_name NOT IN ('audit_log', 'usage', 'usage_storage')")]


def _gate(conn, project: str, p: Path, why: str, extra: dict) -> None:
    """Refuse while runs whose jobs carry the project ID are queued or running, then the §5 #3 confirmation with the
    project's contents signed in its preview (D-04.1)."""
    busy = conn.execute("SELECT (SELECT count(*) FROM flow_runs WHERE project = %(p)s AND status IN ('queued', 'running'))"
                        " + (SELECT count(*) FROM ml_runs WHERE project = %(p)s AND status IN ('queued', 'running'))"
                        " + (SELECT count(*) FROM load_runs WHERE project = %(p)s AND status IN ('queued', 'running'))"
                        " + (SELECT count(*) FROM optimizer_runs WHERE project = %(p)s AND status IN ('queued', 'running')) AS n",
                        {"p": project}).fetchone()["n"]
    if busy:
        raise ActionFailed(409, f"Project '{project}' has {busy} flow, ML or load run(s) queued or running (a plan being worked out counts"
                                " too); try again once they finish.")
    stems = lambda d: sorted(f.stem for f in (p / d).glob("*.json"))
    tables = {r["table_name"] for r in conn.execute("SELECT table_name FROM sources WHERE project = %s", [project])}
    roles = conn.execute("SELECT u.name, r.role FROM project_roles r JOIN users u ON u.id = r.user_id"
                         " WHERE r.project = %s ORDER BY u.name", [project]).fetchall()
    require_confirmation(why, {**extra, "tables": sorted(tables | set(stems("tables"))), "reports": stems("reports"),
                               "flows": stems("flows"), "roles": [f"{r['name']}: {r['role']}" for r in roles]})


def _move(conn, moves: list[tuple]) -> None:
    """Rename folders (all or none), then commit `conn`'s matching row changes."""
    done = []
    try:
        for a, b in moves:
            os.rename(a, b)
            done.append((a, b))
    except OSError:
        for a, b in reversed(done):
            os.rename(b, a)
        raise
    conn.commit()


@action(Role.owner, confirms=True)
def delete_project(conn, user: User, args: ProjectRef) -> dict:
    """Deletes the project for good: its files and Git history, roles, runs and other records (the audit log stays); its data and uploaded files are archived for 30 days. Asks for a confirmation listing its tables, reports, flows and roles."""
    from abi import ingest  # not at top: it imports this module

    p = _existing(args.project)
    with locked(args.project, p):
        _gate(conn, args.project, p, f"Deleting project '{args.project}' removes its files, Git history, roles and runs"
              " for good; its data and uploaded files are archived for 30 days.", {"delete": f"project:{args.project}"})
        for t in _keyed(conn):
            conn.execute(f'DELETE FROM "{t}" WHERE project = %s', [args.project])
        at = int(time.time())
        while conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [gone := f"{storage.DELETED}{at}_{args.project}"[:63]]).fetchone():
            at += 1  # deleted, made again and deleted within the same second: the archive's name is taken
        ingest.retain(p, gone)  # M3.5: before its history goes
        _move(conn, storage.move_lake(conn, args.project, gone))
        shutil.rmtree(p)
    return {"deleted": args.project}


class Rename(BaseModel):
    project: str
    new: str


@action(Role.owner, confirms=True)
def rename_project(conn, user: User, args: Rename) -> dict:
    """Changes the project's ID to `new`, moving its files, records and data so everything reads as before under the new ID; also fixes a project listed with rename_needed. Asks for a confirmation."""
    from abi.tools import builtin  # not at top: it imports this module

    old, new = args.project, args.new
    if why := bad_id(new):
        raise ActionFailed(422, why)
    p = _existing(old)
    with locked(old, p):
        if new in _folders() or storage.lake_exists(conn, new) or any(
                conn.execute(f'SELECT 1 FROM "{t}" WHERE project = %s LIMIT 1', [new]).fetchone() for t in _keyed(conn)):
            raise ActionFailed(409, f"Project ID '{new}' is taken (a project, its records or its data); pick another.")
        _gate(conn, old, p, f"Renaming project '{old}' to '{new}' moves its files, records and data; links and webhook"
              f" URLs naming '{old}' stop working.", {"rename": f"project:{old}", "to": new})
        conn.execute("CREATE TEMP TABLE runs ON COMMIT DROP AS SELECT * FROM flow_runs WHERE project = %s", [old])
        conn.execute("UPDATE runs SET id = %s || substr(id, %s)", [new, len(old) + 1])  # FL-4 IDs: <project>/<flow>/<key>
        conn.execute("INSERT INTO flow_runs SELECT * FROM runs")
        for t in ("flow_steps", "flow_sends"):
            conn.execute(f"UPDATE {t} SET run_id = %s || substr(run_id, %s) WHERE starts_with(run_id, %s)", [new, len(old) + 1, old + "/"])
        conn.execute("DELETE FROM flow_runs WHERE starts_with(id, %s)", [old + "/"])
        for t in _keyed(conn):
            conn.execute(f'UPDATE "{t}" SET project = %s WHERE project = %s', [new, old])
        conn.execute("UPDATE inbox SET snapshot = %s || substr(snapshot, %s) WHERE starts_with(snapshot, %s)",
                     [f"artifact:{new}", len(f"artifact:{old}") + 1, f"artifact:{old}/"])
        art = builtin.artifacts_root()
        _move(conn, [*storage.move_lake(conn, old, new), *([(art / old, art / new)] if (art / old).exists() else []),
                     (p, _root() / new)])
    return {"project": new}


@action(Role.editor)
def undo(conn, user: User, args: ProjectRef) -> dict:
    """Undo the latest uncommitted definition change (§5 #4). Undoing a role change needs an Owner and a confirmation."""
    from abi.project import imports  # not at top: it imports this module

    with locked(args.project):
        r = repo(args.project)
        top = _top(r)
        if top is None or not top.parents:
            raise ActionFailed(409, "Nothing to undo.")
        if _worktree(r) != top.tree_id:
            raise ActionFailed(409, "Project files changed outside the action API since the last action; nothing undone.")
        was, now = top.parents[0].tree, top.tree
        old, files = _files(now), _files(was)
        imports.keys_kept(args.project, old, files)  # 1.2.6 M11: on either way back (the import below, or the files alone)
        gated = imports.ROLES in was and (imports.ROLES not in now or was[imports.ROLES].id != now[imports.ROLES].id) \
            or imports.api_published(old, files) or imports._rules(old) != imports._rules(files) \
            or imports.follow_links(old) != imports.follow_links(files) \
            or imports.granted(old, files) \
            or imports.shared_wider(old, files) or imports.sends(conn, args.project, old, files) \
            or imports.source_hosts(conn, args.project, old, files)
        if gated:  # its Owner checks first: an Editor's 403 comes before a confirmation naming who sees what
            imports.apply(conn, user, args.project, files, dry_run=True)
        # M-16: undoing a first add_data or set_steps can remove a table; archived as a revert does (PC-7)
        removed = _confirm(conn, args.project, old, files, imports.removed(old, files), f"Undoing {top.message}")
        from abi import flows  # not at top: flows imports this module

        try:
            if gated:
                # D-1/M-12: Postgres follows roles.json, so the files go back through the import (Owner + confirmation);
                # D-35: so does re-publishing an API tool, D-42: so does a row-level security change (1.2.6: a follow link), SH-1: widened sharing,
                # FL-6: a flow's first send (apply stores the confirmed targets), H-01.5-fix-8: a new source host
                imports.apply(conn, user, args.project, files)
            else:  # no roles.json before: D-11 leaves roles as they are, so the files alone go back
                _checkout(r, was.id)
                imports.schema_first(conn, args.project, old, files, user.name)  # D-36: undoing a mapping
            flows.drop_stale_tokens(conn, args.project)  # SC-8: undoing a flow's creation drops its token (undo isn't a definition action)
            _archive(conn, user, args.project, removed)  # last: the lake archive can't roll back
        except BaseException:  # atomic like git.change: the rows roll back with the action's transaction, the files here
            _checkout(r, now.id)
            raise
        r.references.create(UNDO_REF, top.parents[0].id, force=True)
        return {"undone": top.message}


class Commit(BaseModel):
    project: str
    message: str = Field(min_length=1, max_length=10000)


class Diff(BaseModel):
    project: str
    base: str = Field("HEAD", pattern=REV)
    to: str | None = Field(None, pattern=REV, description="Omit for the uncommitted working tree.")


class Revert(BaseModel):
    project: str
    to: str = Field(pattern=REV)


class Changes(ProjectRef):
    paths: list[str] = Field([], max_length=100, description="files whose patch you need (the app shows every patch)")


class AutoCommit(BaseModel):
    project: str
    on: bool


@action(Role.viewer)
def changes(conn, user: User, args: Changes) -> dict:
    """Shows the project's uncommitted changes as a diff, a proposed commit message, and whether undo is possible."""
    r = repo(args.project)  # no repo lock (M3 review): a big load holds it for its whole run, and pages read this
    top = _top(r)
    return {"files": _diff(r, r.head.peel(pygit2.Commit).tree_id, _worktree(r, False), _hidden(conn, user, args.project)),
            "proposed_message": _proposal(r), "can_undo": bool(top and top.parents)}


@action(Role.editor)
def commit(conn, user: User, args: Commit) -> dict:
    """Commits all uncommitted changes of the project with this message."""
    with locked(args.project):
        oid = _commit(repo(args.project), user, args.message)
    if oid is None:
        raise ActionFailed(409, "Nothing to commit.")
    return {"commit": oid}


@action(Role.viewer)
def history(conn, user: User, args: ProjectRef) -> dict:
    """Lists the project's commits, newest first, with author (user or Main Agent) and time."""
    r = repo(args.project)  # no repo lock, as changes
    # ponytail: the whole history in one response; page it when projects reach thousands of commits
    cs = [{"id": str(c.id), "message": c.message, "author": c.author.name,
           "by": "agent" if c.author.email.startswith("agent-") else "user", "time": c.author.time}
          for c in r.walk(r.head.target, SortMode.TOPOLOGICAL | SortMode.TIME)]
    # D-22 (GATE-9-fix-8.7): each commit's ID and UTC time as quotable rows; messages are author-written (D-114)
    return {"commits": cs, "columns": ["commit", "time"],
            "rows": [[c["id"], str(datetime.datetime.fromtimestamp(c["time"], datetime.UTC))] for c in cs]}


@action(Role.viewer)
def diff(conn, user: User, args: Diff) -> dict:
    """Diff between two versions of the project (commit ids or HEAD); without `to`, against the uncommitted files."""
    r = repo(args.project)  # no repo lock, as changes
    to = _version(r, args.to).tree_id if args.to else _worktree(r, False)
    return {"files": _diff(r, _version(r, args.base).tree_id, to, _hidden(conn, user, args.project))}


@action(Role.editor, definition=True)
def set_auto_commit(conn, user: User, args: AutoCommit) -> dict:
    """Turns auto-commit on or off: when on, every change is committed right away with the proposed message (PC-6)."""
    f = path(args.project) / "project.json"
    doc = json.loads(f.read_text())
    doc["settings"] = {**doc.get("settings", {}), "auto_commit": args.on}
    f.write_bytes(dumps(doc).encode())
    return {"auto_commit": args.on}


@functools.cache
def zones() -> frozenset[str]:
    """IANA zone names (stdlib zoneinfo: a subset of DuckDB ICU's, so DuckDB takes each one, E-11.md)."""
    return frozenset(zoneinfo.available_timezones())


def _zone(v: str) -> str:
    if v and v not in zones():
        raise ValueError("not an IANA time zone (e.g. Europe/Berlin)")
    return v


TimeZone = Annotated[str, Field(max_length=64, description="IANA time zone, e.g. Europe/Berlin; empty removes it"),
                     AfterValidator(_zone)]
Currency = Annotated[str, Field(pattern="^([A-Z]{3})?$", description="ISO 4217 code, e.g. EUR; empty removes it")]


class Locale(ProjectRef):
    time_zone: TimeZone | None = None
    currency: Currency | None = None


@action(Role.editor, definition=True)
def set_project_locale(conn, user: User, args: Locale) -> dict:
    """Sets the project's time zone (day boundaries and time intelligence, SE-13) and display currency in project.json
    settings, over its tenant's; an omitted field stays as it is, an empty one removes the project's own value."""
    from abi.semantic import model  # not at top: semantic imports git

    f = path(args.project) / "project.json"
    doc = json.loads(f.read_text())
    s = doc.get("settings", {}) | {k: v for k, v in (("time_zone", args.time_zone), ("currency", args.currency)) if v is not None}
    doc["settings"] = {k: v for k, v in s.items() if v != ""}
    f.write_bytes(dumps(doc).encode())
    model.extend_calendars(args.project, user.name)
    return {k: doc["settings"].get(k) for k in ("time_zone", "currency")}


@action(Role.viewer)
def project_locale(conn, user: User, args: ProjectRef) -> dict:
    """The project's own time zone and display currency (project.json settings, null = its tenant's), the effective
    ones its numbers use (SE-13; null while project.json holds an invalid zone) and the model's rates table."""
    from abi import semantic  # not at top: semantic imports git
    from abi.semantic import model

    s = json.loads((_existing(args.project) / "project.json").read_text()).get("settings", {})
    try:
        effective = semantic.locale(args.project)
    except ActionFailed:
        effective = None
    return {"time_zone": s.get("time_zone"), "currency": s.get("currency"), "effective": effective,
            "rates_table": model.load(args.project)["rates_table"]}


def _files(tree: pygit2.Tree, prefix: str = "") -> dict:
    """A tree as a bundle ({path: content}, M-05)."""
    out = {}
    for e in tree:
        rel = prefix + e.name
        if isinstance(e, pygit2.Tree):
            out |= _files(e, rel + "/")
        else:
            text = e.data.decode()
            out[rel] = bundle.loads(text) if rel.endswith(".json") else text
    return out


def _sizes(tree: pygit2.Tree, prefix: str = "") -> dict:
    """{path: blob size} of a tree, without decoding any file."""
    out = {}
    for e in tree:
        out |= _sizes(e, f"{prefix}{e.name}/") if isinstance(e, pygit2.Tree) else {prefix + e.name: e.size}
    return out


def texts(p: Path):
    """M3.5: every file of the project folder `p` as text, as it is now and in each version of its history (every ref:
    the undo chain and a remote's too, which undo, revert or pull bring back), each distinct one once."""
    for f in p.rglob("*"):
        if f.is_file() and ".git" not in f.relative_to(p).parts:
            yield f.read_text(errors="replace")
    r = pygit2.Repository(p)
    trees = list({c.tree_id: c.tree for ref in r.references for c in r.walk(r.references[ref].peel(pygit2.Commit).id)}.values())
    seen = set()
    while trees:
        for e in trees.pop():
            if e.id not in seen:
                seen.add(e.id)
                if isinstance(e, pygit2.Tree):
                    trees.append(e)
                else:
                    yield e.data.decode(errors="replace")


def _confirm(conn, project: str, old: dict, files: dict, gone, what: str, why: list[str] = (),
             shown: dict | None = None) -> set[str]:
    """PC-7/D-1: the tables going from `old` to `files` removes (`gone`, less a table whose steps read its own kept
    source: it stays a plain loaded table), after one confirmation naming them, any role change and the caller's `why`.
    `shown` (E-14 delete impact) is signed with the gate's preview; such a caller must not change roles, RLS or API
    tools, whose apply-side confirmation signs the gate's preview alone."""
    from abi.project import imports  # not at top: it imports this module

    def reads_itself(t: str) -> bool:
        try:
            return old[f"tables/{t}.json"]["steps"][0]["source"] == t and f"sources/{t}.json" in files
        except (KeyError, TypeError, IndexError):
            return False

    removed = {t for t in gone if not reads_itself(t)}
    why = [*why, *([f"It removes table(s) {', '.join(sorted(removed))}; their data is archived for 30 days."] if removed else [])]
    why += imports._publishing(project, old, files)[1]
    gate_why, preview = imports.gate(conn, project, old, files, bool(why))  # signs the exact role diff (D-1) and API tools (D-35)
    why += gate_why
    if why:  # the same token also confirms apply's own confirmation, so it names all of apply's reasons and signs its preview
        require_confirmation(f"{what}: " + " ".join(why), {**(shown or {}), **(preview or {})} or None)
    return removed


def _archive(conn, user: User, project: str, removed: set[str]) -> None:
    """PC-7: removed tables leave IN-6 refresh and the Data tab; their data goes to the 30-day archive."""
    from abi import storage  # not at top: it imports this module

    if removed:
        conn.execute("DELETE FROM sources WHERE project = %s AND table_name = ANY(%s)", [project, sorted(removed)])
        storage.archive(storage.lake(project), removed, author=user.name)


@action(Role.editor, definition=True)
def revert(conn, user: User, args: Revert) -> dict:
    """Reverts the project's definitions to that version as one uncommitted change that undo takes back, through the import checks. Changed tables reload; tables it removes are archived for 30 days; data and the edit log are not reverted. Removing tables or changing roles asks for a confirmation."""
    r = repo(args.project)  # git.change (run) holds the lock
    what = f"Reverting to {args.to[:7]}"
    return apply_tree(conn, user, args.project, tree_files(_version(r, args.to).tree, what), what)


def tree_files(tree: pygit2.Tree, what: str) -> dict:
    """A commit's tree as a bundle; 422 when a file isn't readable as project content or the files total more than an
    import may (D-111), checked on blob sizes before any is decoded, so pull, revert and promote share the import's cap."""
    from abi.app import BODY_MAX  # not at top: the app imports this module

    sizes = _sizes(tree)
    if sum(sizes.values()) > BODY_MAX:
        big = max(sizes, key=sizes.get)
        raise ActionFailed(422, f"{what}: the files total more than {BODY_MAX >> 20} MiB, the import limit; "
                                f"the largest is {big} ({sizes[big] >> 10} KiB).")
    try:
        return _files(tree)
    except ValueError as e:
        raise ActionFailed(422, f"{what}: the files can't be read as project files: {e}")


def apply_tree(conn, user: User, project: str, files: dict, what: str, why: list[str] = (), shown: dict | None = None) -> dict:
    """Make a bundle (a commit's tree, or a PC-10 merge result) the project's files through the import checks (revert,
    PC-8 pull): changed tables reload, tables it removes are archived after a confirmation, as is a role change (D-1).
    Call inside a definition action."""
    from abi import ingest  # not at top: it imports this module
    from abi.project import imports

    old = bundle.bundle(path(project))
    gone = imports.apply(conn, user, project, files, dry_run=True)["impact"]["removed"]  # PC-3 checks, D-1 Owner check
    removed = _confirm(conn, project, old, files, gone, what, why, shown)
    out = imports.apply(conn, user, project, files)  # marks the changed tables for refresh (PC-7)
    _archive(conn, user, project, removed)
    conn.commit()  # as in add_data: loads run on their own connections
    ingest.refresh(conn, project, None, user.name, reload=set(gone) - removed)  # TR-4 order, now rather than on the next worker tick
    return {**out, "archived": sorted(removed)}


def environment(project: str) -> str:
    """PC-9: the branch whose environments/<branch>.json applies, from the project's own .git/config (never merged,
    never the host's git config); `main` until set_environment."""
    cfg = pygit2.Config(str(path(project) / ".git" / "config"))
    return cfg["abi.branch"] if "abi.branch" in cfg else "main"  # noqa: SIM401  pygit2.Config has no get()


def production(project: str) -> bool:
    """D-56: the project is a production environment (view-only but for promotes), from `abi.production` in its own
    .git/config like `abi.branch`: never merged or promoted. False for a missing project."""
    if not isinstance(project, str) or bad_id(project):
        return False
    f = _root() / project / ".git" / "config"
    return f.is_file() and "abi.production" in (cfg := pygit2.Config(str(f))) and cfg.get_bool("abi.production")
