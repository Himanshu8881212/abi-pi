"""Git remotes and environments. PRD PC-8 (push, pull, branches; pulled changes go through PC-3), PC-9 (environments,
promote by merge), PC-10 (diverged pulls merge per object, `merge.py`), DECISIONS D-1, D-2, D-34.

The remote is project.json `settings.remote {url, username, password}`: HTTPS with a literal host, the password only
as ${secret:NAME}. libgit2 gets the password only from `_Callbacks.credentials`, which checks the URL it is asked for
against the configured host and the secret's allowed_hosts (D-2). Off-site redirects are off (libgit2 asks for
credentials with the pre-redirect URL), proxies are off, and the host's git config (`insteadOf`, `http.*`) is never read.
The host's addresses pass API tools' policy before every fetch and push (D-125: loopback/link-local never, private only
for hosts in `ABI_API_PRIVATE_HOSTS`).
A project's environment is the branch in its own .git/config (`git.environment`); its environments/<branch>.json
replaces connection settings of sources/<id>.json (`ingest._source`).
"""

import json
import re
from contextlib import contextmanager
from typing import Literal
from urllib.parse import urlsplit

import pygit2
from pydantic import Field
from pygit2.callbacks import git_fetch_options, git_proxy_options, git_push_options
from pygit2.enums import CredentialType
from pygit2.ffi import C, ffi
from pygit2.utils import StrArray

from abi.actions import ActionFailed, action, require_confirmation
from abi.project import git, imports, merge
from abi.project.bundle import dumps
from abi.security import Role, User, credentials, role_in

URL = r"https://([A-Za-z0-9.-]+(?::[0-9]{1,5})?)/[A-Za-z0-9._~/-]*"  # schemas/v1/project.json settings.remote.url
# libgit2 waits forever by default, and remote calls hold the project's repo lock: bound connect and reads (ms).
pygit2.settings.server_connect_timeout = 10_000
pygit2.settings.server_timeout = 10_000


class _Callbacks(pygit2.RemoteCallbacks):
    def __init__(self, conn, netloc: str, username: str | None, used: dict):
        super().__init__()
        self.conn, self.netloc, self.username, self.used, self.rejected = conn, netloc, username, used, None

    def credentials(self, url, username_from_url, allowed_types):
        if not self.username or not allowed_types & CredentialType.USERPASS_PLAINTEXT:
            raise pygit2.Passthrough
        u = urlsplit(url)
        if u.scheme != "https" or u.netloc.lower() != self.netloc:  # D-2 (refusals are audited by actions.run)
            raise ActionFailed(403, f"The remote's password goes only to {self.netloc}; nothing was sent.")
        credentials.allow(self.conn, self.used, u.hostname, replay=False)
        return pygit2.UserPass(self.username, next(iter(self.used.values()))[0])

    def push_update_reference(self, refname, message):
        self.rejected = message


def _remote(conn, r: pygit2.Repository, project: str):
    """(anonymous remote, callbacks, used secrets) from project.json, re-checked here (the file is the trust boundary).
    `r` comes from `git.repo`, which opens it without the host's git config (insteadOf would rewrite the URL)."""
    s = git.settings(project).get("remote")
    if not isinstance(s, dict) or not (m := re.fullmatch(URL, str(s.get("url")))):
        raise ActionFailed(409, "This project has no remote: set settings.remote {url, username, password} in project.json,"
                                " with url https://<host>[:port]/<path> (no user or password in it).")
    from abi.tools import api  # here: abi.tools imports abi.project
    try:
        host, port = (u := urlsplit(s["url"])).hostname, u.port or 443
    except ValueError:
        raise ActionFailed(400, "settings.remote.url has an invalid port.") from None
    try:  # D-125: API tools' address policy; libgit2 resolves again itself (rebinding residual, TLS checks the name)
        api.checked(host, port)
    except OSError:
        raise ActionFailed(502, f"The Git remote's host '{host}' doesn't resolve.") from None
    except ValueError as e:
        raise ActionFailed(403, f"The Git remote's host '{host}' resolves to {e}. The server never connects to it.") from None
    used: dict = {}
    if s.get("username"):
        if not re.fullmatch(rf"^{credentials.REF}$", str(s.get("password"))):
            raise ActionFailed(400, "settings.remote.password must be exactly ${secret:NAME}.")
        credentials.resolve(conn, s["password"], used, project)
    return r.remotes.create_anonymous(s["url"]), _Callbacks(conn, m[1].lower(), s.get("username"), used), used


@contextmanager
def _errors(used: dict):
    try:
        yield
    except pygit2.GitError as e:  # libgit2's text may quote the server; never a secret (SC-4)
        raise ActionFailed(502, f"The Git remote failed: {credentials.scrub(str(e), used)}") from None


def _fetch(r: pygit2.Repository, rem: tuple, branch: str = "*") -> dict[str, pygit2.Commit]:
    """Fetch the remote's branches (or one) into refs/remotes/origin/; returns {branch: tip}."""
    remote, cb, used = rem
    with _errors(used), git_fetch_options(cb) as p:
        # ponytail: pygit2 1.20 has no follow_redirects argument, so the fetch is called through its cffi layer
        # (private API); use the argument once pygit2 exposes it
        p.fetch_options.follow_redirects = C.GIT_REMOTE_REDIRECT_NONE
        with git_proxy_options(remote, p.fetch_options.proxy_opts, None), \
                StrArray([f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]) as specs:
            p.check_error(C.git_remote_fetch(remote._remote, specs.ptr, p.fetch_options, ffi.NULL))
    tips = {ref[len("refs/remotes/origin/"):]: r.references[ref].peel(pygit2.Commit)
            for ref in r.references if ref.startswith("refs/remotes/origin/")}
    return tips if branch == "*" else {b: c for b, c in tips.items() if b == branch}


class BranchRef(git.ProjectRef):  # branch = file name of environments/<branch>.json
    branch: str | None = Field(None, pattern=git.BRANCH, description="Default: the project's environment branch.")


class Environment(git.ProjectRef):
    branch: str = Field(pattern=git.BRANCH)
    production: bool | None = Field(None, description=(
        "D-56: true makes this copy a production environment (view-only; content changes only by a promote), false"
        " makes it editable again; omit to keep it. A change is confirmed."))


@action(Role.viewer)
def remote_branches(conn, user: User, args: git.ProjectRef) -> dict:
    """Fetches the project's Git remote and lists its branches, with how many commits the project is ahead of and behind each."""
    with git.locked(args.project):
        r = git.repo(args.project)
        for ref in [x for x in r.references if x.startswith("refs/remotes/origin/")]:
            r.references.delete(ref)  # list only what the remote has now
        tips, head = _fetch(r, _remote(conn, r, args.project)), r.head.target
        bs = [{"name": b, "commit": str(c.id), **dict(zip(("ahead", "behind"), r.ahead_behind(head, c.id)))}
              for b, c in sorted(tips.items())]
    # D-22 (GATE-9-fix-8.7): counts and tips as quotable rows; branch names are people's (D-114), in `branches` order
    return {"environment": git.environment(args.project), "branches": bs, "columns": ["commit", "ahead", "behind"],
            "rows": [[b["commit"], b["ahead"], b["behind"]] for b in bs]}


@action(Role.editor)
def push(conn, user: User, args: BranchRef) -> dict:
    """Pushes the project's committed history to a branch of its Git remote (uncommitted changes stay here). Never forces: if the remote branch has commits the project lacks, pull first."""
    branch = args.branch or git.environment(args.project)
    with git.locked(args.project):
        r = git.repo(args.project)
        head = r.head.target
        remote, cb, used = rem = _remote(conn, r, args.project)
        tip = _fetch(r, rem, branch).get(branch)
        if tip and tip.id != head and not r.descendant_of(head, tip.id):
            raise ActionFailed(409, f"Remote branch '{branch}' has commits this project doesn't have; pull first.")
        with _errors(used), git_push_options(cb) as p:
            p.push_options.follow_redirects = C.GIT_REMOTE_REDIRECT_NONE  # ponytail: private API, as in _fetch
            with git_proxy_options(remote, p.push_options.proxy_opts, None), \
                    StrArray([f"{r.head.name}:refs/heads/{branch}"]) as specs:  # no leading +: never forced
                p.check_error(C.git_remote_push(remote._remote, specs.ptr, p.push_options))
        if cb.rejected:
            raise ActionFailed(409, f"The remote refused the push to '{branch}': {credentials.scrub(cb.rejected, used)}. Pull first.")
        return {"branch": branch, "commit": str(head)}


class Pull(BranchRef):
    resolutions: dict[str, Literal["ours", "theirs"]] | None = Field(None, description=(
        'After a 409 with conflicts: {"<file>#<path>": "ours" | "theirs"} for every conflict listed.'))


def _own(rel: str) -> bool:
    """What a promote keeps from the target project: its connections (D-34) and who may see it (D-86)."""
    return rel.startswith("environments/") or rel == imports.ROLES


def _merge(r: pygit2.Repository, base, head: pygit2.Commit, tip: pygit2.Commit, promote: bool, pick: dict | None) -> dict:
    """PC-10: the merged files of HEAD and the remote tip, or 409 listing the conflicts (400 for a wrong `pick`)."""
    b, o, t = (git.tree_files(c.tree, "Merging") for c in (r[base], head, tip))
    if promote:  # PC-9: a promote never brings another environment's connections (D-34) or access list (D-86)
        t = {k: v for k, v in t.items() if not _own(k)} | {k: v for k, v in o.items() if _own(k)}
    files, conflicts = merge.merge(b, o, t)
    keys = {f"{c['file']}#{c['path']}" for c in conflicts}
    if pick is None:
        if conflicts:
            raise ActionFailed(409, "This project and the remote both changed the same objects. Choose ours or theirs for"
                                    " each conflict and pull again with resolutions (or merge in a Git tool and push).",
                               conflicts=conflicts)
        return files
    if pick.keys() != keys:
        unknown, missing = sorted(pick.keys() - keys), sorted(keys - pick.keys())
        raise ActionFailed(400, "resolutions must name every conflict and nothing else"
                                + (f"; unknown: {', '.join(unknown)}" if unknown else "")
                                + (f"; unresolved: {', '.join(missing)}" if missing else "") + ".")
    return merge.merge(b, o, t, pick)[0]


def _promotes(project: str, branch: str, head: pygit2.Commit, files: dict) -> list[str]:
    """§5 #3: a promote changes another environment, so it is confirmed with what it changes (one token, as in _confirm)."""
    where = f"{'production ' if git.production(project) else ''}environment '{git.environment(project)}'"
    return [f"It promotes '{branch}' into {where}: "
            + (" ".join(imports.diff(git.tree_files(head.tree, "Merging"), files)) or "no file changes.")]


@action(Role.editor, definition=True)
def pull(conn, user: User, args: Pull) -> dict:
    """Pulls a branch of the project's Git remote: its files go through the import checks (schemas, references, measures, tool tests; role changes need an Owner) and become the project's committed version. Refused while there are uncommitted changes. If both sides have new commits they are merged per measure, visual and step; conflicts come back for an ours/theirs choice each. Pulling another branch than the project's environment promotes it by merge, keeping this project's environments/ connections and security/roles.json (who may see it)."""
    branch = args.branch or git.environment(args.project)
    promote = branch != git.environment(args.project)
    prod = git.production(args.project)
    if prod and ((have := role_in(conn, user, args.project)) is None or have < Role.owner):
        raise ActionFailed(403, f"Project '{args.project}' is a production environment: only an Owner may promote into it.")
    r = git.repo(args.project)  # git.change (run) holds the lock
    head = r.head.peel(pygit2.Commit)
    if git._worktree(r) != head.tree_id:
        raise ActionFailed(409, "The project has uncommitted changes; commit or undo them before pulling.")
    tip = _fetch(r, _remote(conn, r, args.project), branch).get(branch)
    if tip is None:
        raise ActionFailed(404, f"The remote has no branch '{branch}'.")
    if tip.id == head.id or r.descendant_of(head.id, tip.id):
        return {"commit": str(head.id), "changes": []}  # up to date
    base = r.merge_base(head.id, tip.id)
    if base is not None and (promote or base != head.id):  # diverged, or a promote (never a fast-forward)
        files = _merge(r, base, head, tip, promote, args.resolutions)
        why = _promotes(args.project, branch, head, files) if promote or prod else []
        out = git.apply_tree(conn, user, args.project, files, f"Merging '{branch}'", why)
        sig = git._sig(user)
        r.create_commit("HEAD", sig, sig, f"Merged '{branch}'", git._worktree(r), [head.id, tip.id])
        return {**out, "commit": str(r.head.target)}
    if args.resolutions:
        raise ActionFailed(400, "Nothing to resolve: this pull has no conflicts.")
    # base None: e.g. a new project set up as an environment of an existing repo
    why = [f"The project and remote branch '{branch}' share no history: the project's files and history become the remote's."
           ] if base is None else []
    files = git.tree_files(tip.tree, f"Pulling '{branch}'")
    if promote:  # D-86: adopting another branch keeps this project's roles.json (or its lack of one)
        mine = git.tree_files(head.tree, f"Pulling '{branch}'")
        files = {k: v for k, v in files.items() if k != imports.ROLES} | {k: v for k, v in mine.items() if k == imports.ROLES}
    why += _promotes(args.project, branch, head, files) if prod else []  # D-56: even a fast-forward shows its diff
    out = git.apply_tree(conn, user, args.project, files, f"Pulling '{branch}'", why)
    r.references[r.head.name].set_target(tip.id)  # last: HEAD moves only once the files are in
    # the import writes canonical files (PC-4) and schema-first model entries (PC-15, D-36); either leaves one more
    # commit to push, never an uncommitted change that refuses the next pull
    git._commit(r, user, f"Pulled '{branch}': canonical formatting (PC-4), schema-first model entries (PC-15)")
    return {**out, "commit": str(r.head.target)}


@action(Role.owner)
def set_environment(conn, user: User, args: Environment) -> dict:
    """Binds the project to an environment branch: its connections come from environments/<branch>.json, and push and pull default to that branch. `production` makes it a view-only production environment (D-56) or editable again, after a confirmation. Stored with this copy of the project only, never merged."""
    git.repo(args.project)  # 404 for a missing project
    if args.production is not None and args.production != git.production(args.project):
        require_confirmation(f"Project '{args.project}' becomes " + (
            "a production environment: nobody can change its design any more, only promote from dev (write-back,"
            " sharing and roles stay)." if args.production else "editable again: it stops being a production environment."))
    with git.locked(args.project):
        cfg = pygit2.Config(str(git.path(args.project) / ".git" / "config"))
        cfg["abi.branch"] = args.branch
        if args.production is not None:
            cfg["abi.production"] = args.production
    return {"environment": args.branch, "production": git.production(args.project)}


@action(Role.viewer)
def remote_settings(conn, user: User, args: git.ProjectRef) -> dict:
    """The project's Git remote (URL, user, and the ${secret:NAME} its password comes from, never a value) and its environment branch. No network call."""
    git.repo(args.project)  # 404 for a missing project
    s = git.settings(args.project)
    return {"environment": git.environment(args.project), "production": git.production(args.project),
            "remote": s.get("remote")}


class RemoteSettings(git.ProjectRef):
    url: str | None = Field(None, pattern=f"^{URL}$", description="https://<host>[:port]/<path>; none removes the remote")
    username: str | None = Field(None, pattern=r"^[A-Za-z0-9._@+-]{1,100}$")
    secret: str | None = Field(None, pattern=r"^[A-Za-z0-9_]+$", description="name of the secret holding the password or token")


@action(Role.editor, definition=True)
def set_remote(conn, user: User, args: RemoteSettings) -> dict:
    """Sets the project's Git remote (project.json settings.remote): an https URL, and a user with the name of the secret holding the password or token (the secret's allowed_hosts must hold the remote's host). Without url, removes the remote."""
    if bool(args.username) != bool(args.secret):
        raise ActionFailed(400, "Give both a username and a secret, or neither.")
    if args.secret:  # SC-4: an unknown name may be a typed password; refuse it before it reaches the repo
        credentials.resolve(conn, f"${{secret:{args.secret}}}", {}, args.project)
    remote = args.url and {"url": args.url, **({"username": args.username, "password": f"${{secret:{args.secret}}}"} if args.username else {})}
    f = git.path(args.project) / "project.json"
    doc = json.loads(f.read_text())
    doc["settings"] = {k: v for k, v in {**doc.get("settings", {}), "remote": remote}.items() if v is not None}
    f.write_bytes(dumps(doc).encode())
    return {"remote": remote}
