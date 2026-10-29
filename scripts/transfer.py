#!/usr/bin/env python3
"""A whole project as one zip, and back: everything that defines it, nothing that is data or a secret.

    transfer.py export <project> [out.zip]            the committed project (ABI's own layout) + the app's parts
    transfer.py import <zip> <project> [--apply]      a dry run by default: ABI lists what would change

The zip:
    manifest.json   what is inside, ABI's commit, and what an import needs (secret names, a refresh for the data)
    abi/...         ABI's committed files, exactly as import_project takes them (no uncommitted work, no .gitkeep)
    app/...         the app's JSON (Monitor settings, shared analyses): workspaces/<ws>/_app/<project>/
    vault/...       the vault's documents: workspaces/<ws>/_vault/<project>/files/
    team/...        the agent team's plan, brief and journal: workspaces/<ws>/_team/<project>/
Not inside: the data (Parquet in ABI's lake; a refresh of the sources loads it again), secret values (only their
names, which the person creates in ABI), roles of people (set in ABI), chats (private) and ABI's history (the import
is one new commit). The import goes through import_project, so ABI checks everything and the import is undoable.
"""
import io
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import abi

FORMAT = "abi-pi-export/1"
SECRET = re.compile(r"\$\{secret:([^}]+)\}")


def places(project: str) -> dict[str, Path]:
    dest = abi.folder(project)
    if not dest:
        raise SystemExit(f"No folder for project '{project}'; create it with `abi.py project`.")
    ws = dest.parent
    return {"abi": dest, "app": ws / "_app" / project, "vault": ws / "_vault" / project / "files", "team": ws / "_team" / project}


def committed(dest: Path) -> dict:
    """ABI's committed project as a bundle {path: content}: what ABI has, not what this folder has."""
    abi.git(dest, "fetch", "-q")
    out = {}
    for rel in abi.git(dest, "ls-tree", "-r", "--name-only", "FETCH_HEAD").split():
        if rel.endswith(".gitkeep"):
            continue
        text = subprocess.run(["git", "-C", str(dest), "show", f"FETCH_HEAD:{rel}"], capture_output=True, text=True, check=True).stdout
        out[rel] = json.loads(text) if rel.endswith(".json") else text
    return out


def export(project: str) -> bytes:
    p, buf = places(project), io.BytesIO()
    bundle = committed(p["abi"])
    raw = json.dumps(bundle)
    meta = bundle.get("project.json") or {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, content in bundle.items():
            z.writestr(f"abi/{rel}", json.dumps(content, indent=1) + "\n" if rel.endswith(".json") else content)
        counts = {"abi": len(bundle)}
        for part in ("app", "vault", "team"):
            files = sorted(f for f in p[part].glob("*") if f.is_file()) if p[part].exists() else []
            for f in files:
                z.writestr(f"{part}/{f.name}", f.read_bytes())
            counts[part] = len(files)
        z.writestr("manifest.json", json.dumps({
            "format": FORMAT, "project": project, "name": meta.get("name"), "workspace": p["abi"].parent.name,
            "abi_commit": abi.git(p["abi"], "rev-parse", "FETCH_HEAD").strip(),
            "exported": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": counts,
            "needs": {"secrets": sorted(set(SECRET.findall(raw))),
                      "data": "not included: after the import, refresh the sources to load the data again",
                      "roles": "people and their roles are set in ABI"}}, indent=1) + "\n")
    return buf.getvalue()


def read_zip(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        man = json.loads(z.read("manifest.json"))
        if man.get("format") != FORMAT:
            raise ValueError(f"Not an {FORMAT} zip.")
        out = {"manifest": man, "abi": {}, "app": {}, "vault": {}, "team": {}}
        for n in z.namelist():
            part, _, rel = n.partition("/")
            if part == "abi" and rel:
                text = z.read(n).decode()
                out["abi"][rel] = json.loads(text) if rel.endswith(".json") else text
            elif part in ("app", "vault", "team") and rel and "/" not in rel and not rel.startswith("."):
                out[part][rel] = z.read(n)  # flat folders only: a name can't climb out of its place
        return out


def extras(project: str, z: dict) -> dict:
    """After ABI took the import: the app's parts in their places (the vault through its own add, so it is indexed)."""
    p = places(project)
    for part in ("app", "team"):
        p[part].mkdir(parents=True, exist_ok=True)
        for name, data in z[part].items():
            (p[part] / name).write_bytes(data)
    with tempfile.TemporaryDirectory() as tmp:
        import vault  # noqa: PLC0415 (sibling script)
        for name, data in z["vault"].items():
            (Path(tmp) / name).write_bytes(data)
            vault.add(project, Path(tmp) / name, name)
    return {part: len(z[part]) for part in ("app", "vault", "team")}


def import_zip(project: str, data: bytes, apply: bool = False, confirm: str | None = None) -> dict:
    z = read_zip(data)
    r = abi.call("import_project", project=project, bundle=z["abi"], dry_run=not apply, confirm=confirm)
    out = {"abi": r, "needs": z["manifest"]["needs"]}
    if apply:
        out["placed"] = extras(project, z)
    return out


def demo() -> None:
    """Round trip without ABI: a zip with every part reads back the same, and names can't leave their folder."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": FORMAT, "needs": {}}))
        z.writestr("abi/model/model.json", '{"tables": {}}')
        z.writestr("abi/docs/README.md", "# x")
        z.writestr("app/monitor.json", "{}")
        z.writestr("vault/../../evil.md", "x")
    r = read_zip(buf.getvalue())
    assert r["abi"] == {"model/model.json": {"tables": {}}, "docs/README.md": "# x"}
    assert r["app"] == {"monitor.json": b"{}"} and r["vault"] == {}, "a name with a path is refused"
    assert SECRET.findall('{"key": "${secret:s3_key}"}') == ["s3_key"]
    print("ok")


def main(argv: list[str]) -> None:
    if argv[:1] == ["export"] and len(argv) >= 2:
        out = Path(argv[2] if len(argv) > 2 else f"{argv[1]}.abi.zip")
        out.write_bytes(export(argv[1]))
        print(json.dumps({"zip": str(out), "manifest": json.loads(zipfile.ZipFile(out).read("manifest.json"))}, indent=1))
    elif argv[:1] == ["import"] and len(argv) >= 3:
        try:
            print(json.dumps(import_zip(argv[2], Path(argv[1]).read_bytes(), "--apply" in argv), indent=1, default=str))
        except abi.ActionError as e:  # a 409 is ABI's confirmation: the person confirms in the app, never this script
            print(json.dumps({"status": e.status, "abi": e.body}, indent=1, default=str))
    elif argv[:1] == ["demo"]:
        demo()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
