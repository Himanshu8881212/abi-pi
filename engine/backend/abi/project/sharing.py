"""SH-1 per-object sharing of reports, datasets and custom agents (D-4, D-51; work/validation/E-03.md, E-03.2.md).

Sharing only widens access: project members keep seeing everything; an object's `sharing` ({"users": [user IDs],
"workspace": bool}; an agent's no-share value is "project") lets accounts with no role in the project read that one
object (an agent: run it), view-only.
`actions.run` grants it through `reaches`, checked on every read under the tenant rule (SC-9, D-50), so an imported
foreign ID or an account moved to another tenant reaches nothing.
"""

import re
from typing import Literal

from pydantic import Field

from abi import storage
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import ID, git, imports
from abi.project.bundle import bundle, loads
from abi.security import ALLOWED, MEMBERS, Role, User, role_in

DATASETS = "model/datasets.json"


def _datasets(project: str) -> list[dict]:
    p = git._root() / project / DATASETS
    return loads(p.read_text())["datasets"] if p.is_file() else []


def _object(project: str, kind: str, id) -> dict | None:
    """The stored report or dataset entry, or None (also for any malformed name: this runs before a role check)."""
    if not (isinstance(id, str) and re.fullmatch(ID, id) and isinstance(project, str) and re.fullmatch(storage.PROJECT, project)):
        return None
    if kind in ("report", "agent"):
        p = git._root() / project / f"{kind}s" / f"{id}.json"
        return loads(p.read_text()) if p.is_file() else None
    return next((d for d in _datasets(project) if d["id"] == id), None)


def _may(conn, user: User, project: str) -> tuple[bool, bool]:
    """(may be named, is in the workspace) for `project`: named recipients pass the SC-9 membership rule; workspace-wide
    means the project's own tenant (a platform project: platform accounts), never platform staff in a tenant project."""
    r = conn.execute(f"SELECT {ALLOWED} AS named, tenant IS NOT DISTINCT FROM (SELECT tenant FROM project_tenants"
                     " WHERE project = %s) AS ws FROM users WHERE id = %s", [project, project, user.id]).fetchone()
    return (bool(r["named"]), bool(r["ws"])) if r else (False, False)


def _of(obj: dict) -> dict:
    s = obj.get("sharing")
    return s if isinstance(s, dict) else {}  # absent, or an agent's "project"


def _shared(obj: dict, user: User, may: tuple[bool, bool]) -> bool:
    s = _of(obj)
    return may[0] and str(user.id) in s.get("users", []) or may[1] and s.get("workspace") is True


def reaches(conn, user: User, project, kind: str, id) -> bool:
    """`user` may read report/dataset (or run agent) `id` of `project` through its sharing."""
    obj = _object(project, kind, id)
    return obj is not None and _shared(obj, user, _may(conn, user, project))


def shared_in(conn, user: User, project: str) -> list[dict]:
    """The reports, datasets and agents of `project` shared with `user`: {project, kind, id, name, owner (name)}."""
    may = _may(conn, user, project)
    if not any(may):
        return []
    objs = [("report", o) for o in imports.objects(project, "reports")] + [("dataset", d) for d in _datasets(project)] + \
        [("agent", a) for a in imports.objects(project, "agents")]
    out = [(k, o) for k, o in objs if _shared(o, user, may)]
    # only names the project shows (SC-9 MEMBERS): an owner ID imported from another tenant stays unnamed
    names = {str(r["id"]): r["name"] for r in conn.execute(
        f"SELECT id, name FROM users WHERE {MEMBERS}", [project, project])} if out else {}
    return [{"project": project, "kind": k, "id": o["id"], "name": o["name"], "owner": names.get(o.get("owner"))}
            for k, o in out]


class ObjectRef(storage.ProjectRef):
    kind: Literal["report", "dataset", "agent"]
    id: str = Field(pattern=ID, description="the report, dataset or agent ID")


class Sharing(ObjectRef):
    users: list[str] = Field([], max_length=500, description="names of the accounts that may view it (no role needed)")
    workspace: bool = Field(False, description="every account of the project's tenant (a platform project: every platform account)")


@action(Role.editor, definition=True, confirms=True)
def set_sharing(conn, user: User, args: Sharing) -> dict:
    """Sets who may view a report or dataset, or run a custom agent (its tools then read only within its scope, as them), without a role in the project (SH-1, view-only, row-level security applies): named users and/or the whole workspace. Replaces the previous list; only the object's owner or a project Owner may change it, and widening asks for a confirmation naming the recipients."""
    obj = _object(args.project, args.kind, args.id)
    if obj is None:
        raise ActionFailed(404, f"No {args.kind} '{args.id}' in project '{args.project}'.")
    if obj.get("owner") != str(user.id) and role_in(conn, user, args.project) < Role.owner:
        raise ActionFailed(403, f"Only the {args.kind}'s owner or a project Owner may change who it is shared with.")
    ids = {r["name"]: str(r["id"]) for r in conn.execute(
        f"SELECT id, name FROM users WHERE name = ANY(%s) AND {ALLOWED}", [args.users, args.project])}
    for n in args.users:  # another tenant's account answers as an unknown name (no probing, SC-9)
        if n not in ids:
            raise ActionFailed(400, f"No user named '{n}'.")
    new = {"users": sorted(set(ids.values()), key=int), "workspace": args.workspace}
    old = _of(obj)
    added = sorted(n for n, i in ids.items() if i not in old.get("users", []))
    if added or args.workspace and not old.get("workspace"):
        t = conn.execute("SELECT tenant FROM project_tenants WHERE project = %s", [args.project]).fetchone()
        everyone = [f"every account of tenant '{t['tenant']}'" if t else "every platform account"] if args.workspace else []
        require_confirmation(f"This lets {', '.join(added + everyone)} view {args.kind} '{args.id}' of project"
                             f" '{args.project}' (view-only; row-level security applies).",
                             {"sharing": {"kind": args.kind, "id": args.id, "users": sorted(ids), "workspace": args.workspace}})
    obj = {k: v for k, v in obj.items() if k != "sharing"} | (
        {"sharing": new} if new["users"] or new["workspace"] else {"sharing": "project"} if args.kind == "agent" else {})
    files = bundle(git.path(args.project))
    if args.kind != "dataset":
        change = {f"{args.kind}s/{args.id}.json": obj}
    else:
        change = {DATASETS: files[DATASETS] | {"datasets": [obj if d["id"] == args.id else d for d in files[DATASETS]["datasets"]]}}
    imports.check(conn, args.project, files | change)
    imports.write(args.project, change)
    return {"kind": args.kind, "id": args.id, "users": sorted(ids), "workspace": args.workspace}


@action(Role.editor)
def get_sharing(conn, user: User, args: ObjectRef) -> dict:
    """Who a report, dataset or custom agent is shared with beyond the project's roles (SH-1): user names and workspace-wide, as set_sharing takes them."""
    obj = _object(args.project, args.kind, args.id)
    if obj is None:
        raise ActionFailed(404, f"No {args.kind} '{args.id}' in project '{args.project}'.")
    s = _of(obj)
    # only accounts the tenant rule lets it reach (SC-9): an imported foreign ID grants nothing, so it isn't named
    names = [r["name"] for r in conn.execute(f"SELECT name FROM users WHERE id::text = ANY(%s) AND {ALLOWED} ORDER BY name",
                                             [s.get("users", []), args.project])]
    return {"users": names, "workspace": s.get("workspace") is True}


@action(Role.viewer)
def shared_with_me(conn, user: User, args: git.NoInput) -> dict:
    """Reports, datasets and custom agents shared with you (SH-1) across projects: project, kind, ID, name and owner, to open them view-only."""
    # ponytail: reads every project's report files per call; index sharing in Postgres if projects number in the thousands
    return {"shared": [o for p in git.projects() for o in shared_in(conn, user, p)]}
