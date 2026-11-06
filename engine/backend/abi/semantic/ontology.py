"""Ontology on the semantic model (1.2.6): object types, links and action types, kept in model/ontology.json
(schemas/v1/ontology.json).

It names what model.json already defines and copies nothing: an object type is a keyed model table (its key is the
identity), a property a column (types come from the lake), a link a chain of model.json relationship IDs, each walked
along or against its many-to-one direction. Cardinality and property types are derived, never stored. Nothing in the
code knows a domain: every name comes from the project's data or a person. A project without the file behaves as before.
Changes go through the import pipeline (imports.apply: schema, references, Git, undo) after the person confirms a
preview of what changes and what reads it (lineage).
"""

import json
import re
from typing import Literal
from urllib.parse import quote

from pydantic import Field

from abi import semantic, storage
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import SCHEMA, bundle, git
from abi.security import Role, User
from abi.semantic import lineage, live, model, rls

FILE = "model/ontology.json"
KINDS = {"object": "objects", "link": "links", "action": "actions"}
_INT = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")
M2M_CAP = 20  # many-to-many links a proposal offers at most


def read(project: str) -> dict:
    return model._read(project, "ontology.json")


# ---------- the shape: walking `via`, cardinality, one-to-one tables ----------

def _keys(m: dict) -> dict[str, list[str]]:
    return {e["table"]: e["key"] for e in m.get("entities", [])}


def walk(m: dict, start: str, via: list[str]) -> list[tuple[dict, bool]] | str:
    """[(relationship, along its many-to-one direction?)] walking `via` from table `start` (model.json form `m`), or why
    it can't be walked: an unknown relationship, one that doesn't touch the table reached, a hidden (staging) table, or
    a table reached twice."""
    rels = {r["id"]: r for r in m.get("relationships", [])}
    hidden = {t["table"] for t in m.get("tables", []) if t.get("hidden")}
    at, seen, out = start, [start], []
    for rid in via:
        r = rels.get(rid)
        if r is None:
            return f"names relationship '{rid}', which model/model.json doesn't have"
        if r["from"]["table"] == at:
            at, along = r["to"]["table"], True
        elif r["to"]["table"] == at:
            at, along = r["from"]["table"], False
        else:
            return f"can't walk relationship '{rid}' from table '{at}' (it joins {r['from']['table']} to {r['to']['table']})"
        if at in hidden:
            return f"walks through table '{at}', which is hidden (staging)"
        if at in seen:
            return f"reaches table '{at}' twice"
        seen.append(at)
        out.append((r, along))
    return out


def _one(m: dict, r: dict) -> bool:
    """A relationship whose many side is its table's whole key joins one row to one row."""
    return sorted(r["from"]["columns"]) == sorted(_keys(m).get(r["from"]["table"]) or [None])


def cardinality(m: dict, hops: list[tuple[dict, bool]]) -> str:
    """Derived from the hops: one-to-one hops don't count; all the rest along their many-to-one direction is
    many-to-one, all against it one-to-many, mixed many-to-many."""
    kinds = {along for r, along in hops if not _one(m, r)}
    return {frozenset(): "one-to-one", frozenset({True}): "many-to-one", frozenset({False}): "one-to-many"}.get(
        frozenset(kinds), "many-to-many")


INVERSE = {"many-to-one": "one-to-many", "one-to-many": "many-to-one"}


def one_to_one(m: dict, t: str, of: str) -> bool:
    """Table t is the object table `of`, or has a relationship from its whole key onto `of`'s key (an input table or
    ml_<id> keyed by the object's key)."""
    return t == of or any(r["from"]["table"] == t and r["to"]["table"] == of and _one(m, r) for r in m.get("relationships", []))


# ---------- references: the import check, readiness and the guards on model changes ----------

def references(files: dict, cols: dict | None = None) -> list[dict]:
    """Every reference of model/ontology.json in bundle `files` that doesn't hold, as import errors {file, path, error,
    fix}. `cols` ({table: {column: type}}, the lake): columns of loaded tables are checked too; a table not loaded yet
    is left to readiness, as measures' columns are."""
    o = files.get(FILE)
    if not isinstance(o, dict):
        return []
    m = files.get("model/model.json") or {}
    tables = {t["table"]: t for t in m.get("tables", [])}
    keys, cols = _keys(m), cols or {}
    measures = {x["id"] for x in (files.get("model/measures.json") or {}).get("measures", [])}
    flows = {f[6:-5] for f in files if re.fullmatch(r"flows/[^/]+\.json", f)}
    errs = []

    def err(path: str, error: str, fix: str) -> None:
        errs.append({"file": FILE, "path": path, "error": error, "fix": fix})

    def column(path: str, what: str, t: str, c: str) -> None:
        if t in cols and c not in cols[t]:
            err(path, f"{what}: table '{t}' has no column '{c}'.", f"Use one of: {', '.join(cols[t])}.")

    def unique(kind: str, items: list) -> None:
        seen = set()
        for i, x in enumerate(items):
            if x["id"] in seen:
                err(f"$.{KINDS[kind]}[{i}].id", f"Two {kind}s have the id '{x['id']}'.", f"Give each {kind} its own id.")
            seen.add(x["id"])

    objs = {x["id"]: x for x in o.get("objects", [])}
    unique("object", o.get("objects", []))
    for i, x in enumerate(o.get("objects", [])):
        at, t, what = f"$.objects[{i}]", x["table"], f"Object type '{x['id']}'"
        if t not in tables:
            err(f"{at}.table", f"{what}: unknown model table '{t}'.", f"Use a table of model/model.json: {', '.join(sorted(tables))}.")
            continue
        if tables[t].get("hidden"):
            err(f"{at}.table", f"{what}: table '{t}' is hidden (staging).", "Show it with set_table hidden=false, or use another table.")
        if not keys.get(t):
            err(f"{at}.table", f"{what}: table '{t}' has no key, so its rows have no identity.", f"Declare the key of '{t}' with set_table.")
        column(f"{at}.title", what, t, x["title"])
        for j, p in enumerate(x.get("properties", [])):
            pt, _, pc = p["column"].rpartition(".")
            if pt and not one_to_one(m, pt, t):
                err(f"{at}.properties[{j}].column", f"{what}: table '{pt}' is not one-to-one with '{t}'.",
                    f"Add a relationship from the key of '{pt}' to the key of '{t}', or use a column of '{t}'.")
            else:
                column(f"{at}.properties[{j}].column", what, pt or t, pc)
        errs.extend({"file": FILE, "path": f"{at}.measures[{j}]", "error": f"{what}: unknown measure '{ms}'.",
                     "fix": "Use a measure ID of model/measures.json."} for j, ms in enumerate(x.get("measures", [])) if ms not in measures)

    unique("link", o.get("links", []))
    defaults: dict = {}
    for i, ln in enumerate(o.get("links", [])):
        at, what = f"$.links[{i}]", f"Link '{ln['id']}'"
        ends = [objs.get(ln["from"]), objs.get(ln["to"])]
        for k, e in zip(("from", "to"), ends):
            if e is None:
                err(f"{at}.{k}", f"{what}: unknown object type '{ln[k]}'.", "Use an id of $.objects.")
        if None in ends:
            continue
        hops = walk(m, ends[0]["table"], ln["via"])
        if isinstance(hops, str):
            err(f"{at}.via", f"{what} {hops}.", f"List relationship IDs of model/model.json leading from table '{ends[0]['table']}'"
                                                f" to table '{ends[1]['table']}'.")
        elif hops[-1][0]["to" if hops[-1][1] else "from"]["table"] != ends[1]["table"]:
            err(f"{at}.via", f"{what} ends at table '{hops[-1][0]['to' if hops[-1][1] else 'from']['table']}', not at"
                             f" '{ends[1]['table']}'.", f"Add the relationships that lead on to '{ends[1]['table']}'.")
        if ln.get("default"):
            pair = (ends[0]["table"], ends[1]["table"])
            if pair in defaults:
                err(f"{at}.default", f"{what}: link '{defaults[pair]}' is already the default from '{pair[0]}' to '{pair[1]}'.",
                    "Keep one default link per pair of tables.")
            defaults.setdefault(pair, ln["id"])

    unique("action", o.get("actions", []))
    for i, a in enumerate(o.get("actions", [])):
        at, what, x = f"$.actions[{i}]", f"Action type '{a['id']}'", objs.get(a["object"])
        if x is None:
            err(f"{at}.object", f"{what}: unknown object type '{a['object']}'.", "Use an id of $.objects.")
        if a.get("flow") and a["flow"] not in flows:
            err(f"{at}.flow", f"{what}: unknown flow '{a['flow']}'.", f"Add flows/{a['flow']}.json or name another flow.")
        if "inputs" in a:
            from jsonschema import Draft202012Validator  # noqa: PLC0415
            from jsonschema.exceptions import SchemaError  # noqa: PLC0415

            try:
                Draft202012Validator.check_schema(a["inputs"])
            except SchemaError as e:
                err(f"{at}.inputs", f"{what}: `inputs` is not a valid JSON Schema (fails `{e.validator}`).", "Fix it to follow JSON Schema draft 2020-12.")
        if (w := a.get("writes")) and x is not None:
            if w["table"] not in tables or not keys.get(w["table"]) or not one_to_one(m, w["table"], x["table"]):
                err(f"{at}.writes.table", f"{what}: table '{w['table']}' is neither '{x['table']}' nor a keyed table one-to-one with it.",
                    f"Write to '{x['table']}' or to an input table keyed by its key with a relationship onto it.")
            for c in w["set"]:
                column(f"{at}.writes.set", what, w["table"], c)
    return errs


def _cols(project: str) -> dict:
    try:
        return model.lake_columns(project)
    except Exception:  # noqa: BLE001 - no lake yet: columns are checked once loaded (readiness)
        return {}


def check(project: str, files: dict) -> list[dict]:
    """`references` against the project's lake; [] when `files` has no ontology (nothing read)."""
    return references(files, _cols(project)) if FILE in files else []


def gaps(project: str) -> list[str]:
    """Readiness (§3): the ontology's dangling references, in plain words; none without an ontology."""
    if not (git.path(project) / FILE).is_file():
        return []
    return [f"Ontology: {e['error']} {e['fix']}" for e in check(project, bundle.bundle(git.path(project)))]


def guard(project: str, model_json: dict, what: str) -> None:
    """Refuses (409) a model.json change that would break a reference of the ontology, naming each use, as a measure
    or dataset in use is refused; nothing happens without an ontology."""
    if not (git.path(project) / FILE).is_file():
        return
    files = bundle.bundle(git.path(project))
    before = {e["error"] for e in check(project, files)}
    if new := [e for e in check(project, files | {"model/model.json": model_json}) if e["error"] not in before]:
        raise ActionFailed(409, f"{what} is used by the ontology: " + " ".join(e["error"] for e in new)
                                + " Change or remove those first (save_ontology, delete_ontology_item).", errors=new)


def uses(project: str, node: str) -> list[str]:
    """The ontology nodes (object:, link:, action:) that read `node` directly (lineage)."""
    if not (git.path(project) / FILE).is_file():
        return []
    g = lineage.graph(bundle.bundle(git.path(project)))
    return sorted(n for n, ups in g.items() if node in ups and n.startswith(("object:", "link:", "action:")))


# ---------- reading it: types, properties, links both ways, actions ----------

def _properties(m: dict, x: dict, cols: dict, hid: dict) -> list[dict]:
    """Every visible column of the type's table in lake order, then its listed one-to-one properties; the listed
    name, meaning and description over each, the lake type beside it. A column hidden from the reader is left out."""
    listed = {p["column"]: p for p in x.get("properties", [])}
    own = [c for c in cols.get(x["table"], {}) if c.lower() not in hid.get(x["table"], ())]
    more = [c for c in listed if "." in c and c.split(".", 1)[1].lower() not in hid.get(c.split(".", 1)[0], ())]
    out = []
    for c in own + more:
        t, _, col = c.rpartition(".")
        out.append({"column": c, "type": cols.get(t or x["table"], {}).get(col), **{k: v for k, v in listed.get(c, {}).items() if k != "column"}})
    return out


def describe(project: str, user_id: int | None) -> dict:
    """The ontology as the reader sees it (the `ontology` action's answer)."""
    o, m, cols = read(project), model._read(project, "model.json"), model.lake_columns(project)
    hide, hid = rls.restricted(project, user_id), rls.hidden(project, user_id)
    if not o.get("objects"):
        return {"objects": [], "links": [], "actions": [], "note": "No ontology yet: propose_ontology drafts one from the model."}
    mask = (lambda d: {**d, "description": rls.MARK} if "description" in d else d) if hide else (lambda d: d)
    objs = {x["id"]: x for x in o["objects"]}
    links = []
    for ln in o.get("links", []):
        hops = walk(m, objs[ln["from"]]["table"], ln["via"]) if ln["from"] in objs else "unknown type"
        links.append(mask({**ln, "cardinality": cardinality(m, hops) if isinstance(hops, list) else None}))
    keys = _keys(m)
    out = []
    for x in o["objects"]:
        ways = [{"link": ln["id"], "name": ln["name"], "to": ln["to"], "cardinality": ln["cardinality"]} for ln in links if ln["from"] == x["id"]]
        ways += [{"link": ln["id"], "name": ln.get("inverse") or f"{ln['name']} (inverse)", "to": ln["from"],
                  "cardinality": INVERSE.get(ln["cardinality"], ln["cardinality"]), "inverse": True} for ln in links if ln["to"] == x["id"]]
        out.append(mask({**{k: v for k, v in x.items() if k != "properties"}, "key": keys.get(x["table"]),
                         "properties": [mask(p) for p in _properties(m, x, cols, hid)], "links": ways,
                         "actions": [a["id"] for a in o.get("actions", []) if a["object"] == x["id"]]}))
    return {"objects": out, "links": links, "actions": [mask(a) for a in o.get("actions", [])]}


@action(Role.viewer)
def ontology(conn, user: User, args: storage.ProjectRef) -> dict:
    """The project's ontology (model/ontology.json): object types (table, key, title column, properties with their lake types and meanings, measures), links in both directions with their derived cardinality (one-to-one, many-to-one, one-to-many, many-to-many) and the relationships they walk, and action types. Columns hidden from you are left out."""
    return describe(args.project, user.id)


# ---------- proposing one: read-only, from the model and the data, as the caller ----------

def _singular(t: str) -> str:
    # ponytail: naive (strips one trailing s, plural adds one); the agent and the person rename
    return t[:-1] if t.endswith("s") and not t.endswith("ss") and len(t) > 1 else t


def _label(s: str) -> str:
    return s.replace("_", " ").strip().capitalize() or s


def _meaning(col: str, typ: str, key: list, distinct: int | None, rows: int) -> str:
    """A column's meaning from its name, DuckDB type and number of values. ponytail: generic name and type rules
    only (no domain words); the agent refines them."""
    n = col.lower()
    if col in key or n == "id" or n.endswith(("_id", "_key")):
        return "identifier"
    if typ == "BOOLEAN":
        return "flag"
    if typ == "DATE":
        return "date"
    if typ.startswith("TIMESTAMP"):
        return "timestamp"
    for words, meaning in ((("email", "e_mail"), "email"), (("phone", "mobile"), "phone"), (("url", "website", "link"), "url"),
                           (("latitude", "lat"), "latitude"), (("longitude", "lon", "lng"), "longitude"),
                           (("country",), "country"), (("city",), "city"), (("name", "title", "label"), "name")):
        if any(n == w or n.endswith("_" + w) for w in words):
            return meaning
    if {"percent", "pct", "share"} & set(n.split("_")):
        return "percent"
    if re.fullmatch(r"DECIMAL\(\d+,2\)", typ):
        return "money"
    if typ in _INT or typ.startswith(("DECIMAL", "DOUBLE", "FLOAT", "REAL")):
        return "quantity"
    if typ == "VARCHAR" and distinct is not None and distinct <= 50 and rows and distinct < rows / 2:
        return "category"
    return "text"


def _title(cols: dict, key: list) -> str:
    """The column shown as an object's name: a text column named like a name, else the (first) key column."""
    text = [c for c, ty in cols.items() if ty == "VARCHAR" and c not in key]
    return next((c for c in text if c.lower() in ("name", "title", "label") or c.lower().endswith(("_name", "_title", "_label"))), key[0])


def propose(project: str, user_id: int | None) -> dict:
    m = model.visible(model.load(project))
    mj, cols = model._read(project, "model.json"), model.lake_columns(project)
    date, hid = (m["date_table"] or {}).get("table"), rls.hidden(project, user_id)
    objects, types = [], {}
    for t, i in m["tables"].items():
        if not i["key"] or t == date or t not in cols:
            continue
        tc = {c: ty for c, ty in cols[t].items() if c.lower() not in hid.get(t, ())}  # D-110: never a hidden column
        text = [c for c, ty in tc.items() if ty == "VARCHAR"]
        rows, n = 0, []
        if not live.is_live(project, t):  # a live table: meanings from names and types only
            [(rows, *n)] = semantic.execute(project, "SELECT count(*)" + "".join(f", approx_count_distinct({storage._q(c)})" for c in text)
                                            + f" FROM {storage._q(t)}", {"propose_ontology": t}, user_id)  # RLS: the caller's rows
        distinct = dict(zip(text, n))
        one = _singular(t)
        x = {"id": model.slug(one), "table": t, "name": _label(one), "plural": _label(one) + "s", "title": _title(tc, i["key"]),
             "properties": [{"column": c, "meaning": _meaning(c, ty, i["key"], distinct.get(c), rows)}
                            for c, ty in tc.items() if c not in i["key"]]}
        if i["description"]:
            x["description"] = i["description"][:2000]
        objects.append(x)
        types[t] = x
    taken, links, cards = {x["id"] for x in objects}, [], {}
    ruled = {r["table"] for r in rls.row_rules(project)}

    def add(a: dict, b: dict, via: list, more: str = "") -> None:
        lid = model.slug(f"{a['id']}_{b['id']}{more}")
        while lid in taken:
            lid = model.slug(lid + "_2")
        taken.add(lid)
        card = cards[lid] = cardinality(mj, walk(mj, a["table"], via))
        ln = {"id": lid, "from": a["id"], "to": b["id"], "name": b["name"].lower() if card in ("many-to-one", "one-to-one") else b["plural"].lower(),
              "inverse": a["plural"].lower(), "via": via}
        if card != "many-to-one" and b["table"] in ruled:  # owner, 6 Nov 2026: proposed on, the Owner approves it
            ln["rls"] = "follow"
        links.append(ln)

    rels = [r for r in m["relationships"] if r["to"]["table"] in types]
    for r in rels:
        if r["from"]["table"] in types and r["from"]["table"] != r["to"]["table"]:
            add(types[r["from"]["table"]], types[r["to"]["table"]], [r["id"]])
    m2m = 0
    for f in dict.fromkeys(r["from"]["table"] for r in rels):  # each table linking two types: one link per pair of them
        out = [r for r in rels if r["from"]["table"] == f]
        for i, r1 in enumerate(out):
            for r2 in out[i + 1:]:
                if r1["to"]["table"] != r2["to"]["table"] and m2m < M2M_CAP:
                    a, b = (r2, r1) if r1["to"]["table"] in ruled and r2["to"]["table"] not in ruled else (r1, r2)  # toward a ruled type
                    add(types[a["to"]["table"]], types[b["to"]["table"]], [a["id"], b["id"]], f"_via_{model.slug(_singular(f))}")
                    m2m += 1
    doc = {"$schema": SCHEMA + "ontology.json", "objects": objects, "links": links}
    return {"ontology": doc, "cardinality": cards,
            "note": "A draft from the model and your data, not saved. Names are naive: refine names, plurals and descriptions"
                    " (search the vault and cite it), drop what isn't wanted, then save_ontology; the person confirms."}


@action(Role.viewer)
def propose_ontology(conn, user: User, args: storage.ProjectRef) -> dict:
    """Drafts an ontology from the model and the data, as you (row-level security applies), and saves nothing: each visible keyed table but the date table as an object type (title column and property meanings from names, types and value counts), each relationship between two types as a many-to-one link, each table linking two types as a many-to-many link per pair (several ways between two types stay separate links; none is picked). Refine names and descriptions, then save_ontology."""
    return propose(args.project, user.id)


# ---------- changing it: one confirmed, undoable change through the import pipeline ----------

def _items(o: dict | None) -> dict[str, dict]:
    return {f"{k}:{x['id']}": x for k, key in KINDS.items() for x in (o or {}).get(key, [])}


def _say(node: str, x: dict) -> str:
    kind, i = node.split(":", 1)
    if kind == "object":
        return f"object type '{i}' ({x['name']}, table {x['table']})"
    if kind == "link":
        return f"link '{i}' ({x['from']} {x['name']} {x['to']}, via {', '.join(x['via'])})"
    return f"action type '{i}' ({x['name']} on {x['object']})"


def diff(old: dict | None, new: dict | None) -> list[str]:
    """Plain-language changes, one per added, removed or changed object type, link and action type."""
    a, b = _items(old), _items(new)
    out = [f"Adds {_say(n, b[n])}." for n in b if n not in a] + [f"Removes {_say(n, a[n])}." for n in a if n not in b]
    out += [f"Changes {_say(n, b[n])}: " + ", ".join(k for k in sorted(a[n].keys() | b[n].keys()) if a[n].get(k) != b[n].get(k)) + "."
            for n in b if n in a and a[n] != b[n]]
    if (old or {}).get("base") != (new or {}).get("base"):
        out.append("Changes the export's IRI base.")
    return out


def _change(conn, user: User, project: str, new: dict | None) -> dict:
    """Replaces model/ontology.json with `new` (None or empty: removes it): checked like an import first (422 with
    file, path and fix), then the person confirms a preview of the changes and the lineage downstream of every changed
    or removed item, then applied by imports.apply as one undoable change."""
    from abi.project import imports  # not at top: imports imports the semantic package

    files = bundle.bundle(git.path(project))
    old = files.get(FILE)
    rest = {k: v for k, v in files.items() if k != FILE}
    if new and any(new.get(k) for k in KINDS.values()):
        new = {**new, "$schema": SCHEMA + "ontology.json"}
        after = rest | {FILE: new}
    else:
        new, after = None, rest
    imports.check(conn, project, after)
    changes = diff(old, new)
    if not changes:
        return {"changes": []}
    a, b = _items(old), _items(new)
    hit = {n for n in a.keys() | b.keys() if a.get(n) != b.get(n)}
    ga, gb = lineage.graph(files), lineage.graph(after)
    reads = {n: lineage.reach(gb if n in b else ga, {n}, "downstream") for n in sorted(hit)}
    preview = {"changes": changes, "reads": {n: r for n, r in reads.items() if r}}
    require_confirmation(f"This changes the ontology of project '{project}': {' '.join(changes)}", preview)
    return imports.apply(conn, user, project, after) | {"ontology": preview}


class Save(storage.ProjectRef):
    ontology: dict = Field(description="the whole model/ontology.json: {objects, links, actions, base?} as schemas/v1/ontology.json"
                                       " (propose_ontology's draft, refined); it replaces the file")


@action(Role.editor, definition=True, confirms=True)
def save_ontology(conn, user: User, args: Save) -> dict:
    """Saves the project's ontology (model/ontology.json): object types over keyed model tables, links over its relationships, action types. Checked like an import (schema; every table keyed and visible, column, relationship chain, measure and flow exists); then the person confirms a preview of each change and what reads it; one undoable change."""
    return _change(conn, user, args.project, {k: v for k, v in args.ontology.items() if k != "$schema"})


class ItemRef(storage.ProjectRef):
    kind: Literal["object", "link", "action"]
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")


@action(Role.editor, definition=True, confirms=True)
def delete_ontology_item(conn, user: User, args: ItemRef) -> dict:
    """Deletes one object type, link or action type from the ontology after the person confirms. Refused while another item uses it (a link or action type on an object type)."""
    o, key = read(args.project), KINDS[args.kind]
    if all(x["id"] != args.id for x in o.get(key, [])):
        raise ActionFailed(404, f"No {args.kind} '{args.id}' in the ontology of project '{args.project}'.")
    try:
        return _change(conn, user, args.project, {**o, key: [x for x in o[key] if x["id"] != args.id]})
    except ActionFailed as e:  # the import check words a use as a dangling reference; say it as a use
        if e.status == 422 and (used := [x["error"] for x in e.body.get("errors", []) if f"'{args.id}'" in x["error"]]):
            raise ActionFailed(409, f"{args.kind.capitalize()} '{args.id}' is still used: {' '.join(used)} Remove those first.",
                               errors=e.body["errors"]) from None
        raise


# ---------- export: definitions only, as OWL in Turtle or JSON-LD ----------

XSD = [(r"BOOLEAN", "boolean"), (r"DATE", "date"), (r"TIMESTAMP.*", "dateTime"), (r"DECIMAL.*|NUMERIC.*", "decimal"),
       (r"DOUBLE|FLOAT|REAL", "double"), (r"(U?(TINY|SMALL|BIG|HUGE)?INT(EGER)?)", "integer")]
OWL, RDFS, XS = "http://www.w3.org/2002/07/owl#", "http://www.w3.org/2000/01/rdf-schema#", "http://www.w3.org/2001/XMLSchema#"
TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


def _xsd(typ: str | None) -> str:
    return XS + next((x for p, x in XSD if typ and re.fullmatch(p, typ)), "string")


def triples(project: str, d: dict) -> list[tuple[str, str, tuple]]:
    """(subject, predicate, ("iri" | "lit", value)) of `describe`'s answer `d`: classes, datatype and object properties
    (functional when many-to-one, with their inverse) and action types as annotation properties. No rows, no hosts, no
    secret references: only names the definitions hold."""
    base = read(project).get("base") or ""
    base = base if base and not re.search(r'[\s<>"{}|\\^`]', base) else f"urn:abi:{project}:"  # an IRI Turtle can hold
    iri = lambda *parts: base + "/".join(quote(p, safe="") for p in parts)
    a, label, comment = TYPE, RDFS + "label", RDFS + "comment"
    out = []

    def node(s: str, kind: str, name: str, desc: str | None = None, **iris: str) -> None:
        out.extend([(s, a, ("iri", OWL + kind)), (s, label, ("lit", name))] + ([(s, comment, ("lit", desc))] if desc else []))
        out.extend((s, RDFS + p, ("iri", o)) for p, o in iris.items())

    for x in d["objects"]:
        node(iri(x["id"]), "Class", x["name"], x.get("description"))
        for p in x["properties"]:
            node(iri(x["id"], p["column"]), "DatatypeProperty", p.get("name") or p["column"], p.get("description"),
                 domain=iri(x["id"]), range=_xsd(p["type"]))
    for ln in d["links"]:
        s, inv = iri("link", ln["id"]), iri("link", ln["id"], "inverse")
        node(s, "ObjectProperty", ln["name"], ln.get("description"), domain=iri(ln["from"]), range=iri(ln["to"]))
        node(inv, "ObjectProperty", ln.get("inverse") or f"{ln['name']} (inverse)", None, domain=iri(ln["to"]), range=iri(ln["from"]))
        out.append((s, OWL + "inverseOf", ("iri", inv)))
        if ln["cardinality"] in ("many-to-one", "one-to-one"):
            out.append((s, a, ("iri", OWL + "FunctionalProperty")))
        if ln["cardinality"] in ("one-to-many", "one-to-one"):
            out.append((inv, a, ("iri", OWL + "FunctionalProperty")))
    for x in d["actions"]:
        node(iri("action", x["id"]), "AnnotationProperty", x["name"], x.get("description"), domain=iri(x["object"]))
    return out


def _clean(text: str) -> str:
    """Definitions only: a ${secret:...} reference typed into a description never leaves (SC-4)."""
    return re.sub(r"\$\{secret:[^}]*\}", "[secret reference]", text)


def turtle(ts: list) -> str:
    term = lambda o: f"<{o[1]}>" if o[0] == "iri" else json.dumps(_clean(o[1]), ensure_ascii=False)  # JSON escapes are Turtle's
    return "".join(f"<{s}> <{p}> {term(o)} .\n" for s, p, o in ts)


def jsonld(ts: list) -> dict:
    graph: dict = {}
    for s, p, o in ts:
        graph.setdefault(s, {"@id": s}).setdefault(p, []).append({"@id": o[1]} if o[0] == "iri" else {"@value": _clean(o[1])})
    return {"@graph": list(graph.values())}


class Export(storage.ProjectRef):
    format: Literal["turtle", "jsonld"] = "turtle"


@action(Role.viewer)
def export_ontology(conn, user: User, args: Export) -> dict:
    """Exports the ontology's definitions as OWL (Turtle text or JSON-LD): object types as classes, properties as datatype properties with XSD types, links as object properties with their inverse (functional when many-to-one), action types as annotations. No rows, no connection details, no secrets; there is no import from RDF."""
    d = describe(args.project, user.id)
    if not d["objects"]:
        raise ActionFailed(404, f"Project '{args.project}' has no ontology yet: propose_ontology drafts one.")
    ts = triples(args.project, d)
    return {"format": args.format, "triples": len(ts), "content": turtle(ts) if args.format == "turtle" else jsonld(ts)}
