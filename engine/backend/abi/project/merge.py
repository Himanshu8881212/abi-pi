"""Structural three-way merge of project bundles. PRD PC-10 (conflicts per measure, visual, step), PC-9 (promote by merge).

A line merge conflicts on neighbouring edits to different measures in one file, so files are merged as parsed JSON:
dicts by key, lists of objects with an `id` by id (measures, pages, visuals, flow steps), a table's `steps` by
position, anything else (and non-JSON files) as a whole value. A conflict is reported at the smallest id-carrying
object around it, with base/ours/theirs (null = absent), and resolved per item: `pick` maps "<file>#<path>" to
"ours" or "theirs".
"""

MISSING = object()  # a file, key or item that isn't there
KINDS = {"measures": "measure", "pages": "page", "visuals": "visual", "steps": "step"}


def _ids(items) -> dict | None:
    """{id: item} when every item is an object with a unique string id, else None."""
    if not all(isinstance(x, dict) and isinstance(x.get("id"), str) for x in items):
        return None
    out = {x["id"]: x for x in items}
    return out if len(out) == len(items) else None


def _out(v):
    return None if v is MISSING else v


def _merge(file: str, path: str, b, o, t, pick: dict, conflicts: list, kind: str = "value", id=None):
    if o == t:
        return o
    if b == o:
        return t
    if b == t:
        return o
    key = f"{file}#{path}"
    if key in pick:
        return o if pick[key] == "ours" else t
    if all(isinstance(x, dict) for x in (b, o, t)):
        merged = {k: _merge(file, f"{path}.{k}", b.get(k, MISSING), o.get(k, MISSING), t.get(k, MISSING), pick, conflicts)
                  for k in [*o, *(k for k in t if k not in o), *(k for k in b if k not in o and k not in t)]}
        return {k: v for k, v in merged.items() if v is not MISSING}
    if all(isinstance(x, list) for x in (b, o, t)):
        name = path.rsplit(".", 1)[-1]
        if None not in (ids := (_ids(b), _ids(o), _ids(t))):
            order = [*ids[1], *(i for i in ids[2] if i not in ids[1])]
            items = [(i, f"{path}[id={i}]", *(d.get(i, MISSING) for d in ids)) for i in order]
        elif name == "steps" and file.startswith("tables/") and len(b) == len(o) == len(t):
            # ponytail: transform steps carry no id, so they merge by position only when no side added or removed
            # one; otherwise the whole list conflicts. Give steps ids if parallel step edits become common.
            items = [(n, f"{path}[{n}]", *x) for n, x in enumerate(zip(b, o, t, strict=True))]
        else:
            items = None
        if items is not None:
            out = []
            for i, at, *vals in items:
                sub: list = []
                v = _merge(file, at, *vals, pick, sub, KINDS.get(name, "value"), i)
                if any(c["id"] is None for c in sub):  # a field-level clash: the user picks the whole object
                    sub = [{"file": file, "kind": KINDS.get(name, "value"), "id": i, "path": at,
                            "base": _out(vals[0]), "ours": _out(vals[1]), "theirs": _out(vals[2])}]
                conflicts += sub
                if v is not MISSING:
                    out.append(v)
            return out
    conflicts.append({"file": file, "kind": kind, "id": id, "path": path, "base": _out(b), "ours": _out(o), "theirs": _out(t)})
    return o


def merge(base: dict, ours: dict, theirs: dict, pick: dict | None = None) -> tuple[dict, list[dict]]:
    """Three-way merge of bundles ({path: content}): (merged bundle, conflicts). Where a conflict is left
    unpicked, the merged bundle holds ours."""
    conflicts: list = []
    files = {rel: _merge(rel, "$", base.get(rel, MISSING), ours.get(rel, MISSING), theirs.get(rel, MISSING),
                         pick or {}, conflicts, "file", rel) for rel in sorted({*base, *ours, *theirs})}
    return {rel: v for rel, v in files.items() if v is not MISSING}, conflicts
