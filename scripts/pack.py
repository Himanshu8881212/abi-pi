#!/usr/bin/env python3
"""A board pack: the documents, data extracts and explainer video a meeting needs, made from ABI and checked before
anything leaves. Kept in workspaces/<ws>/_app/<project>/packs/<id>/ (manifest.json, notes.md and the files), so it is
in the project's export zip. Nothing is sent from here: sending is a flow or an email the person approves.

    pack.py make <project> <spec.json|'{…}'>  spec: {id, title, audience, as_of, notes, items: [...]}
    pack.py list <project>

Items:
    {"kind": "pdf" | "png" | "pptx", "report": r, "page": p | "pages": [..] | "all", "visual"?: v, "filters"?: [...]}
    {"kind": "csv" | "xlsx", "report": r, "page": p, "visual": v, "filters"?: [...]}
    {"kind": "video", "report": r, "pages"?: [..], "views"?: [..], "theme"?: "paper" | "ink", "captions"?: {view: text}}
The notes (the commentary, Markdown) may only use numbers that appear in the rows of the pack's report views; else
the pack is refused. Whatever ABI can't render is listed with its reason, never replaced by something made here.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import abi
import video

ID = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
DOC, DATA = {"pdf", "png", "pptx"}, {"csv", "xlsx"}


def home(project: str) -> Path:
    return abi.folder(project).parent / "_app" / project / "packs"


def rows_of(project: str, reports: set[str]) -> dict:
    """Every non-text view's rows of these reports, as ABI returns them: what the notes may quote."""
    out = {"rows": []}
    listed = abi.call("list_reports", project=project)
    for r in listed.get("reports", listed):
        if isinstance(r, dict) and r.get("id") in reports:
            for p in r["pages"]:
                for v in p["visuals"]:
                    if v.get("type") != "text" and v.get("text") is None:
                        out["rows"] += abi.call("visual_data", project=project, report=r["id"], page=p["id"], visual=v["id"]).get("rows", [])
    return out


def make(project: str, spec: dict) -> dict:
    if not ID.match(spec.get("id", "")):
        raise SystemExit("A pack id is lowercase letters, digits and _, starting with a letter.")
    items = spec.get("items") or []
    if not items:
        raise SystemExit("A pack lists at least one item.")
    seen = rows_of(project, {i["report"] for i in items if i.get("report")})
    notes = spec.get("notes", "")
    if notes and not video.grounded(notes, seen):
        bad = [n.strip() for n in video.NUM.findall(notes) if not video.grounded(n, seen)]
        raise SystemExit(f"The notes use numbers that are in none of the pack's report views: {', '.join(bad[:8])}.")
    d = home(project) / spec["id"]
    d.mkdir(parents=True, exist_ok=True)
    done = []
    for n, it in enumerate(items, 1):
        k, rep = it.get("kind"), it.get("report")
        name = f"{n:02d}-{rep}-{it.get('visual') or it.get('page') or 'pages'}.{'mp4' if k == 'video' else k}"
        rec = {**{x: it[x] for x in it if x != "captions"}, "file": name}
        try:
            if k in DOC:
                args = {x: it[x] for x in ("page", "pages", "visual", "filters") if it.get(x) not in (None, [])}
                (d / name).write_bytes(abi.artifact(abi.call("report_snapshot", project=project, report=rep, format=k, **args)["snapshot"]))
            elif k in DATA:
                link = abi.call("export_visual", project=project, report=rep, page=it["page"], visual=it["visual"], format=k,
                                filters=it.get("filters") or [])["link"]
                (d / name).write_bytes(abi.artifact(link.removeprefix("/api/artifacts/")))
            elif k == "video":
                props = video.build(project, rep, it.get("pages"), it.get("views"), it.get("theme", "paper"), it.get("seconds", 6),
                                    captions=it.get("captions"))
                video.render(props, d / name)
                rec["slides"] = len(props["slides"])
            else:
                raise ValueError(f"unknown kind {k!r}")
            rec["status"] = "ok"
        except (abi.ActionError, SystemExit, ValueError, KeyError) as e:
            body = e.body if isinstance(e, abi.ActionError) and isinstance(e.body, dict) else {}
            old = any(isinstance(x, dict) and x.get("loc") in (["page"], ["format"]) for x in body.get("errors") or [])
            rec.update(status="failed", file=None, error="This ABI renders one page at a time as PDF or PNG; several pages in one file"
                       " and PPTX need a newer ABI (MULTI-EXPORT)." if old else body.get("message") or str(e)[:300])
        done.append(rec)
    (d / "notes.md").write_text(f"# {spec.get('title', spec['id'])}\n\n{notes}\n")
    ok = all(x["status"] == "ok" for x in done)
    man = {"id": spec["id"], "title": spec.get("title"), "audience": spec.get("audience"), "as_of": spec.get("as_of"), "project": project,
           "made": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "ready" if ok else "partial",
           "notes_grounded": True, "items": done, "sent": False,
           # rendered under this person's row-level security: the app shows it to them, project owners and admins only
           "made_by": (os.environ.get("ABI_APP_CLIENT", "").split("/")[0] if "/" in os.environ.get("ABI_APP_CLIENT", "") else None)}
    (d / "manifest.json").write_text(json.dumps(man, indent=1) + "\n")
    return man


def listing(project: str) -> list[dict]:
    return [json.loads(m.read_text()) for m in sorted(home(project).glob("*/manifest.json"))] if home(project).exists() else []


def main(argv: list[str]) -> None:
    if argv[:1] == ["make"] and len(argv) == 3:
        src = argv[2]
        spec = json.loads(src if src.lstrip().startswith("{") else sys.stdin.read() if src == "-" else Path(src).read_text())
        print(json.dumps(make(argv[1], spec), indent=1))
    elif argv[:1] == ["list"] and len(argv) == 2:
        print(json.dumps(listing(argv[1]), indent=1))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
