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
import uuid
from decimal import Decimal
from typing import Literal
from urllib.parse import quote

import duckdb
from pydantic import BaseModel, Field, ValidationError

from abi import actions, semantic, storage
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import SCHEMA, bundle, git
from abi.security import IN_PROJECT, Role, User, role_in
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


def keyed(o: dict, m: dict, name: str) -> tuple[dict, list[str]] | None:
    """(Object type `name`, its key as table.column) in ontology `o` over model.json `m`; None without the type or a key."""
    x = next((x for x in o.get("objects", []) if x["id"] == name), None)
    k = x and _keys(m).get(x["table"])
    return (x, [f"{x['table']}.{c}" for c in k]) if k else None


def people(o: dict, x: dict, link: str | None = None) -> list[str] | None:
    """1.2.6 M8: the properties a per-object alert may find its recipients in: those of type `x` a person gave the meaning
    email or person, or (with `link`) those columns of the type the link reaches from `x`; None: no such link on `x`."""
    if link:
        ln = next((ln for ln in o.get("links", []) if ln["id"] == link and x["id"] in (ln["from"], ln["to"])), None)
        x = ln and next((y for y in o.get("objects", []) if y["id"] == ln["to" if ln["from"] == x["id"] else "from"]), None)
    return x and [p["column"] for p in x.get("properties", []) if p.get("meaning") in ("email", "person") and not (link and "." in p["column"])]


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
        if rs := x.get("resolve"):  # 1.2.6 M11: what find_same compares, all columns of the type's own table
            if len(keys.get(t) or []) > 1:  # ponytail: one key column (the same-as table holds a pair of keys); a key tuple when a type needs it
                err(f"{at}.resolve", f"{what}: `resolve` needs a table with one key column, and '{t}' is keyed by {', '.join(keys[t])}.",
                    f"Give '{t}' one key column (set_table), or take `resolve` off the type.")
            if (s := f"same_as_{t}") in tables and keys.get(s) != ["a", "b"]:
                err(f"{at}.resolve", f"{what}: `resolve` keeps its decisions in table '{s}', and the model has another table of that name.",
                    f"Rename that table, or take `resolve` off the type.")
            for k in ("exact", "fuzzy"):
                for j, f in enumerate(rs.get(k, [])):
                    column(f"{at}.resolve.{k}[{j}].column", what, t, f["column"])
            for j, c in enumerate(rs.get("block", [])):
                column(f"{at}.resolve.block[{j}]", what, t, c)
            if "source" in rs:
                column(f"{at}.resolve.source", what, t, rs["source"])

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
        if a.get("approval") and not a.get("writes"):
            err(f"{at}.approval", f"{what}: `approval` makes its write a proposal, but it writes nothing.", "Add `writes`, or drop `approval`.")
        if a.get("flow") in flows and (bad := _templated(files[f"flows/{a['flow']}.json"])):  # a Viewer never picks what runs
            err(f"{at}.flow", f"{what}: flow '{a['flow']}' selects what runs with a {{{{ … }}}} template ({bad}),"
                              " so a run could pick what runs.", "Name each action, flow, agent, tool, report,"
                              " model, dataset, object type and type literally in that flow.")
        for j, st in enumerate((files.get(f"flows/{a.get('flow')}.json") or {}).get("steps", [])):  # owner, 7 Nov 2026: an allowlist
            if not step_ok(st):
                err(f"{at}.flow", f"{what}: step '{st['id']}' of flow '{a['flow']}' ({st.get('action') or st['type']}) isn't one an"
                                  " action type's flow may run.", f"Use only {', '.join(sorted(STEPS_OK))} steps and the actions"
                                  f" {', '.join(sorted(ACTIONS_OK))} (step {j}); start other work from a flow no action type starts.")
        if a.get("flow") in flows and (files[f"flows/{a['flow']}.json"].get("trigger") or {}).get("type") != "manual":
            err(f"{at}.flow", f"{what}: flow '{a['flow']}' doesn't have a manual trigger.",
                "Give the flow trigger {type: manual}; an action starts it with {{ data.trigger.object }} and {{ data.trigger.inputs }}.")
        if (w := a.get("writes")) and x is not None:
            if w["table"] not in tables or not keys.get(w["table"]) or not one_to_one(m, w["table"], x["table"]):
                err(f"{at}.writes.table", f"{what}: table '{w['table']}' is neither '{x['table']}' nor a keyed table one-to-one with it.",
                    f"Write to '{x['table']}' or to an input table keyed by its key with a relationship onto it.")
            for c in w["set"]:
                column(f"{at}.writes.set", what, w["table"], c)
                if c in (keys.get(w["table"]) or []):  # the row is the object's: its key comes from the object, never a run
                    err(f"{at}.writes.set", f"{what}: '{c}' is a key column of '{w['table']}'.", "Set only columns outside the key.")
    return errs


# 1.2.6 M4 (owner, 7 Nov 2026): what a flow an action type starts may run, as the person who clicks (a Viewer too): loads, sends
# (recipients as today), an agent (its question quoted), report snapshots, checks, approvals, reads selecting nothing
STEPS_OK = {"refresh", "agent", "snapshot", "condition", "approval"}
ACTIONS_OK = {"refresh", "send_email", "post_message", "run_agent", "report_snapshot", "semantic_query", "get_object",
              "list_objects", "visual_data"}
SELECTORS = ("action", "flow", "model", "dataset", "object", "type", "agent", "agents", "tool", "tools", "report", "reports")


def step_ok(st: dict) -> bool:
    """A step a flow an action type starts may run (STEPS_OK, ACTIONS_OK)."""
    return st["type"] in STEPS_OK or st["type"] == "action" and st.get("action") in ACTIONS_OK


def _templated(x, at: str = "$") -> str | None:
    """The JSON path of the first selector value (`agent`, `flow`, `model`, … at any depth) holding a template."""
    if isinstance(x, list):
        return next((p for i, v in enumerate(x) if (p := _templated(v, f"{at}[{i}]"))), None)
    if not isinstance(x, dict):
        return None
    for k, v in x.items():
        if k in SELECTORS and "{{" in json.dumps(v):
            return f"{at}.{k}"
        if p := _templated(v, f"{at}.{k}"):
            return p
    return None


def _cols(project: str) -> dict:
    try:
        return model.lake_columns(project)
    except Exception:  # noqa: BLE001 - no lake yet: columns are checked once loaded (readiness)
        return {}


def check(project: str, files: dict) -> list[dict]:
    """`references` against the project's lake; [] when `files` has no ontology (nothing read). 1.2.6 M11: `resolve`
    on a live table is refused here too (records are compared and merged in loaded tables only)."""
    if FILE not in files:
        return []
    from abi.semantic import same  # not at top: it imports this module

    return references(files, _cols(project)) + same.blockers(project, files) + [
        {"file": FILE, "path": f"$.objects[{i}].resolve", "error": f"Object type '{x['id']}': `resolve` works on loaded tables, and '{x['table']}' is a live table.",
         "fix": "Take `resolve` off the type, or load the table (a source that imports it) instead of reading it live."}
        for i, x in enumerate((files[FILE] or {}).get("objects", []) if isinstance(files[FILE], dict) else []) if "resolve" in x and live.is_live(project, x["table"])]


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

def _seen(cols: dict, hid: dict, t: str) -> list[str]:
    """The columns of table `t` the reader may read (D-110), in lake order."""
    return [c for c in cols.get(t, {}) if c.lower() not in hid.get(t, ())]


def _properties(m: dict, x: dict, cols: dict, hid: dict) -> list[dict]:
    """Every visible column of the type's table in lake order, then its listed one-to-one properties; the listed
    name, meaning and description over each, the lake type beside it. A column hidden from the reader is left out."""
    listed = {p["column"]: p for p in x.get("properties", [])}
    own = _seen(cols, hid, x["table"])
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
        out.append(mask({**{k: v for k, v in x.items() if k != "properties" and not (hide and k == "resolve")}, "key": keys.get(x["table"]),
                         "properties": [mask(p) for p in _properties(m, x, cols, hid)], "links": ways,
                         "actions": [a["id"] for a in o.get("actions", []) if a["object"] == x["id"]]}))
    return {"objects": out, "links": links, "actions": [mask(a) for a in o.get("actions", [])]}


def terms(o: dict, hide: bool = False) -> list[dict]:
    """1.2.6 M6: the described object types, properties and links of an ontology (the file, or `describe`'s answer,
    masked already) as glossary terms: the description is the definition. Their IDs (object:…, property:…, link:…)
    are never glossary.json's, so memory links only to free terms, as before. `hide`: definitions as rls.MARK."""
    said = lambda x: rls.MARK if hide else x["description"]  # noqa: E731
    out = []
    for x in o.get("objects", []):
        if x.get("description"):
            out.append({"id": f"object:{x['id']}", "term": x["name"], "definition": said(x), "objects": [x["id"]]})
        for p in x.get("properties", []):
            if p.get("description"):
                t, _, c = p["column"].rpartition(".")
                out.append({"id": f"property:{x['id']}.{p['column']}", "term": p.get("name") or _label(c), "definition": said(p),
                            "objects": [x["id"]], "columns": [{"table": t or x["table"], "column": c}]})
    return out + [{"id": f"link:{ln['id']}", "term": ln["name"], "definition": said(ln), "links": [ln["id"]]}
                  for ln in o.get("links", []) if ln.get("description")]


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
        if not i["key"] or t == date or t not in cols or t.startswith("ml_") and (git.path(project) / "ml" / f"{t[3:]}.json").is_file():
            continue  # M9: a model's predictions table is keyed like its object type, and is no type itself
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
    # 1.2.6 M6: the glossary terms whose columns belong to a proposed type (set_glossary_term {objects} attaches them)
    attach = [{"term": g["id"], "objects": about} for g in model._read(project, "glossary.json").get("terms", [])
              if (about := sorted({types[c["table"]]["id"] for c in g.get("columns", []) if c["table"] in types} - set(g.get("objects", []))))]
    return {"ontology": doc, "cardinality": cards, **({"glossary": attach} if attach else {}),
            "note": "A draft from the model and your data, not saved. Names are naive: refine names, plurals and descriptions"
                    " (search the vault and cite it), drop what isn't wanted, then save_ontology; the person confirms."
                    + (" `glossary` lists the terms whose columns belong to a type: once it is saved, attach each with"
                       " set_glossary_term {objects} (send the term's other fields again)." if attach else "")}


@action(Role.viewer)
def propose_ontology(conn, user: User, args: storage.ProjectRef) -> dict:
    """Drafts an ontology from the model and the data, as you (row-level security applies), and saves nothing: each visible keyed table but the date table as an object type (title column and property meanings from names, types and value counts), each relationship between two types as a many-to-one link, each table linking two types as a many-to-many link per pair (several ways between two types stay separate links; none is picked); `glossary` suggests the glossary terms to attach to a type (their columns are its table's). Refine names and descriptions, then save_ontology."""
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
    # one confirmation with the import gate's (a follow link: the Owner, and who sees fewer rows), after its checks
    return imports.apply(conn, user, project, after, ask=(f"This changes the ontology of project '{project}': {' '.join(changes)}", preview)) | {
        "ontology": preview}


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
    """Deletes one object type, link or action type from the ontology after the person confirms. Refused while another item uses it (a link or action type on an object type) or a glossary term names it."""
    o, key = read(args.project), KINDS[args.kind]
    if all(x["id"] != args.id for x in o.get(key, [])):
        raise ActionFailed(404, f"No {args.kind} '{args.id}' in the ontology of project '{args.project}'.")
    if named := [t["id"] for t in model._read(args.project, "glossary.json").get("terms", []) if args.id in t.get(key, [])]:
        raise ActionFailed(409, " ".join(f"Term '{t}' names {'object type' if args.kind == 'object' else args.kind} '{args.id}'." for t in named)
                           + " Remove it from the term first (set_glossary_term).")
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


# ---------- objects: one by its key, many by filters, walked along links (row-level reads, as the person) ----------

LIMIT = 1000  # D-17


def _obj(project: str, name: str, user_id: int | None) -> tuple[dict, dict, dict, dict, dict]:
    """(ontology, model.json, the object type, lake columns, columns hidden from the reader) for type `name`: 404
    without it or before its table is loaded; refused when its key is hidden from the reader (objects have no identity
    then; the column isn't named)."""
    o = read(project)
    x = next((x for x in o.get("objects", []) if x["id"] == name), None)
    if x is None:
        raise ActionFailed(404, f"No object type '{name}' in project '{project}': `ontology` lists them.")
    cols, hid, m = model.lake_columns(project), rls.hidden(project, user_id), model._read(project, "model.json")
    if x["table"] not in cols:
        raise ActionFailed(404, f"The {x['plural']} of project '{project}' aren't loaded yet: table '{x['table']}' has no data.")
    if not set(_keys(m)[x["table"]]) <= set(_seen(cols, hid, x["table"])):
        raise ActionFailed(403, f"You can't read the {x['plural']} of project '{project}' one by one: what identifies them is hidden from you.")
    return o, m, x, cols, hid


def _col(cols: dict, hid: dict, t: str, c: str) -> str:
    """Column `c` of table `t` if the reader may read it; a hidden column reads like a missing one (400)."""
    if c not in _seen(cols, hid, t):
        raise ActionFailed(400, f"Table '{t}' has no column '{c}' you can read. Use one of: {', '.join(_seen(cols, hid, t))}.")
    return c


def _hidden_join(cols: dict, hid: dict, r: dict) -> bool:
    return not all(c in _seen(cols, hid, side["table"]) for side in (r["from"], r["to"]) for c in side["columns"])


def _tuple(cols: list[str]) -> str:
    return storage._q(cols[0]) if len(cols) == 1 else f"({', '.join(map(storage._q, cols))})"


def _semi(hops: list[tuple[dict, bool]], start: str, cond: str) -> str:
    """A condition on the table `hops` reach from table `start`: its rows that link to a row of `start` holding `cond`,
    one `IN (SELECT …)` semi-join per hop, so a row counts once however many paths reach it (no fan-out, many-to-many
    and several hops alike). Every table is unqualified: a restricted reader's are their row-level security views."""
    at = start
    for r, along in hops:
        mine, theirs = (r["from"], r["to"]) if along else (r["to"], r["from"])
        cond = f"{_tuple(theirs['columns'])} IN (SELECT {', '.join(map(storage._q, mine['columns']))} FROM {storage._q(at)} WHERE {cond})"
        at = theirs["table"]
    return cond


def _follow(project: str, m: dict, o: dict, x: dict, ids: list[str], cols: dict, hid: dict) -> tuple[list, dict]:
    """(the hops, the object type reached) walking links `ids` in order from type `x`, each from its `from` or (its
    inverse) its `to` type, exactly along its `via`. A join column the reader can't read (hidden or not loaded)
    refuses (400) without naming it, alike."""
    objs, links = {t["id"]: t for t in o.get("objects", [])}, {ln["id"]: ln for ln in o.get("links", [])}
    hops = []
    for i in ids:
        ln = links.get(i)
        if ln is None:
            raise ActionFailed(400, f"No link '{i}' in project '{project}': `ontology` lists each type's links.")
        if x["id"] not in (ln["from"], ln["to"]):
            raise ActionFailed(400, f"Link '{i}' joins {ln['from']} and {ln['to']}, not {x['id']}.")
        fwd = ln["from"] == x["id"]
        h = walk(m, x["table"], ln["via"] if fwd else ln["via"][::-1])
        if isinstance(h, str):
            raise ActionFailed(400, f"Link '{i}' {h}.")
        if any(_hidden_join(cols, hid, r) for r, _ in h):
            raise ActionFailed(400, f"Link '{i}' joins on a column you can't read.")
        hops, x = hops + h, objs[ln["to"] if fwd else ln["from"]]
    return hops, x


def _live(project: str, tables) -> None:
    """ponytail: objects read DuckLake tables only; a live (ClickHouse) table answers 400 until objects need them."""
    if t := next((t for t in tables if live.is_live(project, t)), None):
        raise ActionFailed(400, f"Objects are read from loaded tables; '{t}' is a live table.")


def _read(project: str, sql: str, asked: dict, user_id: int | None) -> list[tuple]:
    """One read through the semantic engine as the person: RLS views, rls.check, the query log, D-32 redaction."""
    try:
        return [tuple(map(semantic.tidy, r)) for r in semantic.execute(project, sql, asked, user_id, timeout=semantic.TIMEOUT)]
    except duckdb.ConversionException as e:
        raise ActionFailed(400, f"A value doesn't fit its column: {e}") from None
    except duckdb.Error as e:
        raise ActionFailed(400, f"The read failed: {e}") from None


class Get(storage.ProjectRef):
    type: str = Field(description="an object type ID (`ontology`)")
    key: list[storage.Value] = Field(min_length=1, description="the object's key value(s), in key column order")
    links: list[str] = Field([], max_length=10, description="link IDs to list this object's neighbors along (the type's own links or inverses)")
    limit: int = Field(20, ge=1, le=LIMIT, description="neighbors per link")


@action(Role.viewer)
def get_object(conn, user: User, args: Get) -> dict:
    """One object by its key, as you see it (row-level security applies): its properties (the columns not hidden from you, then its one-to-one properties), its type's measures filtered to it, and up to `limit` neighbors (key and title) along each asked link. A hidden object answers like a missing one (404). Every figure you state must come from these rows."""
    o, m, x, cols, hid = _obj(args.project, args.type, user.id)
    t, keys = x["table"], _keys(m)[x["table"]]
    if len(args.key) != len(keys):
        raise ActionFailed(400, f"{x['name']} is keyed by {', '.join(keys)}: give {len(keys)} value(s), in that order.")
    walks = {i: _follow(args.project, m, o, x, [i], cols, hid) for i in dict.fromkeys(args.links)}
    props = _properties(m, x, cols, hid)
    own = [p["column"] for p in props if "." not in p["column"]]
    ones = []  # one-to-one properties: (table, columns, its relationship onto the type), read only over readable joins
    for pt in dict.fromkeys(p["column"].split(".", 1)[0] for p in props if "." in p["column"]):
        r = next((r for r in m.get("relationships", []) if r["from"]["table"] == pt and r["to"]["table"] == t and _one(m, r)), None)
        if r is not None and not _hidden_join(cols, hid, r):
            ones.append((pt, [p["column"].split(".", 1)[1] for p in props if p["column"].startswith(pt + ".")], r))
    _live(args.project, [t, *(pt for pt, _, _ in ones), *(r[s]["table"] for h, _ in walks.values() for r, _ in h for s in ("from", "to"))])
    where = " AND ".join(f"{storage._q(k)} = {rls.cast_lit(v, cols[t][k])}" for k, v in zip(keys, args.key))
    asked = {"get_object": x["id"], "key": args.key}
    found = _read(args.project, f"SELECT {', '.join(map(storage._q, own))} FROM {storage._q(t)} WHERE {where} LIMIT 1", asked, user.id)
    if not found and "resolve" in x and len(keys) == 1:  # 1.2.6 M11: a record merged into another one they see is that object
        from abi.semantic import same  # not at top: it imports this module

        try:
            if to := same.shown_as(args.project, x, args.key[0], user.id):
                return get_object(conn, user, args.model_copy(update={"key": to}))
        except duckdb.Error:  # a key that doesn't fit its column: missing, as below
            pass
    if not found:  # SE-6: a hidden object and a missing one answer alike
        raise ActionFailed(404, f"No visible {x['name']} with key {json.dumps(args.key)} in project '{args.project}'.")
    columns, row = own, list(found[0])
    for pt, pcs, r in ones:  # a second read each
        there = [c for c in pcs if c in cols.get(pt, {})]  # a column that is gone reads empty (readiness names it), never fails the object
        got = there and _read(args.project, f"SELECT {', '.join(map(storage._q, there))} FROM {storage._q(pt)} WHERE {_semi([(r, False)], t, where)}"
                                            " LIMIT 1", asked, user.id)
        # 1.2.6 M9: a table denied to the reader (predictions made from rows their rules filter) says "hidden", not "no value"
        gap = rls.MARK if not got and rls.restricted(args.project, user.id) and pt in rls.unfiltered_outputs(args.project) else None
        vals = dict(zip(there, got[0])) if got else {}
        columns, row = columns + [f"{pt}.{c}" for c in pcs], row + [vals.get(c, gap if c in there else None) for c in pcs]
    out = {"type": x["id"], "name": x["name"], "key": [row[own.index(k)] if k in own else v for k, v in zip(keys, args.key)],
           "title": row[own.index(x["title"])] if x["title"] in own else None}
    if ms := x.get("measures"):
        kv = [v if isinstance(v, str | int | float | bool) else str(v) for v in out["key"]]
        try:
            q = semantic.query(args.project, {"measures": ms, "filters": [{"column": f"{t}.{k}", "op": "=", "value": v} for k, v in zip(keys, kv)]},
                               user.id, timeout=semantic.TIMEOUT)
            columns, row, out["measures"] = columns + ms, row + list(q["rows"][0] if q["rows"] else [None] * len(ms)), q["measures"]
        except ActionFailed as e:
            out["measures_error"] = e.body["message"]
    out |= {"columns": columns, "rows": [row], "links": []}
    for i, (hops, y) in walks.items():
        yt, show = y["table"], [c for c in dict.fromkeys([*_keys(m)[y["table"]], y["title"]]) if c in _seen(cols, hid, y["table"])]
        sel = ", ".join(map(storage._q, show))
        rows = _read(args.project, f"SELECT {sel} FROM {storage._q(yt)} WHERE {_semi(hops, t, where)} ORDER BY {sel} LIMIT {args.limit + 1}",
                     asked | {"link": i}, user.id)
        out["links"].append({"link": i, "to": y["id"], "columns": show, "rows": rows[:args.limit], "more": len(rows) > args.limit})
    return out


RECIPIENTS = 50  # send_email takes as many addresses


def nested(columns: list, row: list) -> dict:
    """An object's values by column; a one-to-one property (`table.column`) also under its table, so a flow reads it as
    {{ data.trigger.object.<table>.<column> }} (a path splits on dots). A column of the object named like that table wins."""
    out = dict(zip(columns, row))
    for c, v in zip(columns, row):
        t, _, col = c.partition(".")
        if col and isinstance(out.setdefault(t, {}), dict):
            out[t][col] = v
    return out


def recipients(conn, project: str, got: dict, rc: dict, user: User) -> dict:
    """1.2.6 M8 (owner, 6 Nov 2026): who a per-object alert is for. The values of the email or person property
    `rc.property` of object `got` (get_object's answer), or of the objects `rc.link` reaches from it, read as `user` (the
    flow's owner). A value only ever names a project member, by their account's email or name, and never becomes an
    address itself; a member is kept only when they see the object themself (get_object as them).
    {to: their account emails, members: their ids, skipped: [{value, why}]} (the run's trace)."""
    p, key, skipped = project, got["key"], []
    if link := rc.get("link"):
        o, m, x, cols, hid = _obj(p, got["type"], user.id)
        hops, y = _follow(p, m, o, x, [link], cols, hid)
        _live(p, [r[s]["table"] for r, _ in hops for s in ("from", "to")])
        c, t = storage._q(_col(cols, hid, y["table"], rc["property"])), x["table"]
        where = " AND ".join(f"{storage._q(k)} = {rls.cast_lit(v, cols[t][k])}" for k, v in zip(_keys(m)[t], key))
        vals = [r[0] for r in _read(p, f"SELECT DISTINCT {c} FROM {storage._q(y['table'])} WHERE {_semi(hops, t, where)} AND {c} IS NOT NULL"
                                       f" ORDER BY 1 LIMIT {RECIPIENTS + 1}", {"recipients": x["id"], "key": _json(key), "link": link}, user.id)]
    else:
        vals = [v for v in [dict(zip(got["columns"], got["rows"][0])).get(rc["property"])] if v is not None]
    if len(vals) > RECIPIENTS:
        vals, skipped = vals[:RECIPIENTS], [{"value": None, "why": f"more than {RECIPIENTS} recipients: only the first {RECIPIENTS} are used"}]
    vals = list(dict.fromkeys(str(v).strip() for v in vals))
    users = conn.execute("SELECT id, name, workspace_owner, email FROM users WHERE (email = ANY(%s::text[]) OR name = ANY(%s::text[]))"
                         f" AND {IN_PROJECT}", [[v.lower() for v in vals], vals, p]).fetchall()
    kept, nomail = {}, []
    for v in vals:
        u = next((u for u in users if v.lower() == u["email"] or v == u["name"]), None)
        if u is None:
            skipped.append({"value": v, "why": "not a member of this project"})
            continue
        try:  # ponytail: a full object read per recipient (its measures too); a key-only read when alerts get many recipients
            get_object(conn, User(u["id"], u["name"], u["workspace_owner"]), Get(project=p, type=got["type"], key=_json(key)))
        except ActionFailed:
            skipped.append({"value": v, "why": f"can't see this {got['name']}"})
            continue
        kept[u["id"]] = u["email"]
        nomail += [v] * (not u["email"])
    return {"to": [e for e in kept.values() if e], "members": list(kept), "skipped": skipped, "nomail": nomail}


class Where(BaseModel):
    property: str = Field(description="a column of the object type's table, or of the type reached by `link`")
    op: Literal["=", "<>", "<", "<=", ">", ">=", "in"]
    value: storage.Value | list[storage.Value] = Field(description="one value; a list for `in`")
    link: str | None = Field(None, pattern=r"^[a-z][a-z0-9_-]{0,62}(\.[a-z][a-z0-9_-]{0,62}){0,3}$",
                             description="link IDs joined by dots, walked in order from the listed type, e.g. visited or order_product.order_cafe:"
                                         " the object qualifies when an object reached holds the condition")


class By(BaseModel):
    property: str
    desc: bool = False


class Objects(storage.ProjectRef):
    type: str = Field(description="an object type ID (`ontology`)")
    filters: list[Where] = Field([], max_length=20, description="all must hold")
    search: str | None = Field(None, min_length=1, max_length=200, description="text in the title (case-insensitive)")
    order: list[By] = Field([], max_length=5, description="default: the title, then the key")
    limit: int = Field(100, ge=1, le=LIMIT)
    offset: int = Field(0, ge=0)


@action(Role.viewer)
def list_objects(conn, user: User, args: Objects) -> dict:
    """Lists objects of one type, as you see them (row-level security applies): their columns not hidden from you, filtered on their own properties or on a property of objects reached through a chain of links (`link`: e.g. customers who visited a North café: {link: visited, property: region, op: '=', value: North}), each object once however many ways it links. `search` matches text in the title. `more` says whether another page follows (`offset`). Every figure you state must come from these rows."""
    o, m, x, cols, hid = _obj(args.project, args.type, user.id)
    t = x["table"]
    col = lambda tbl, c: _col(cols, hid, tbl, c)  # noqa: E731

    conds, tables = [], [t]
    for f in args.filters:
        hops, y = _follow(args.project, m, o, x, f.link.split("."), cols, hid) if f.link else ([], x)
        tables += [r[s]["table"] for r, _ in hops for s in ("from", "to")]
        c, typ = col(y["table"], f.property), cols[y["table"]][f.property]
        vals = f.value if isinstance(f.value, list) else [f.value]
        if (f.op == "in") != isinstance(f.value, list) or not vals:
            raise ActionFailed(400, "Use op 'in' with a list of values, any other op with one value.")
        cond = f"{storage._q(c)} IN ({', '.join(rls.cast_lit(v, typ) for v in vals)})" if f.op == "in" else f"{storage._q(c)} {f.op} {rls.cast_lit(vals[0], typ)}"
        conds.append(_semi([(r, not a) for r, a in reversed(hops)], y["table"], cond) if hops else cond)
    if args.search:
        conds.append(f"CAST({storage._q(col(t, x['title']))} AS VARCHAR) ILIKE '%{storage._lit(args.search)}%'")  # ponytail: % and _ match any text
    _live(args.project, tables)
    keys, show = _keys(m)[t], _seen(cols, hid, t)
    order = [f"{storage._q(col(t, b.property))}{' DESC' if b.desc else ''}" for b in args.order] or [storage._q(x["title"])] * (x["title"] in show)
    order += [storage._q(k) for k in keys if k in show]  # then the key: pages never overlap
    sql = (f"SELECT {', '.join(map(storage._q, show))} FROM {storage._q(t)} WHERE {' AND '.join(conds) or 'TRUE'}"
           f" ORDER BY {', '.join(dict.fromkeys(order))} LIMIT {args.limit + 1} OFFSET {args.offset}")
    rows = _read(args.project, sql, {"list_objects": x["id"], **args.model_dump(mode="json", exclude={"project", "type"})}, user.id)
    return {"type": x["id"], "name": x["plural"], "key": keys, "title": x["title"], "columns": show, "rows": rows[:args.limit],
            "offset": args.offset, "more": len(rows) > args.limit}


# ---------- action types: run on one object, as the person; its history ----------

def _row_of(m: dict, x: dict, t: str, key: list) -> list:
    """The key of table t's row for object `key` (in key order) of type x: t is x's table, or one-to-one with it (its
    whole key a relationship onto x's key)."""
    if t == x["table"]:
        return key
    ks = dict(zip(_keys(m)[x["table"]], key))
    r = next((r for r in m.get("relationships", []) if r["from"]["table"] == t and r["to"]["table"] == x["table"] and _one(m, r)), None)
    on = dict(zip(r["from"]["columns"], r["to"]["columns"])) if r else {}
    if not on or not set(on.values()) <= ks.keys():
        raise ActionFailed(409, f"Table '{t}' is no longer one-to-one with '{x['table']}': fix the relationship or the action type.")
    return [ks[on[c]] for c in _keys(m)[t]]


def _json(v):
    """A value read for an object as JSON (a DECIMAL as a number, a date as ISO text), for checks, previews and flows."""
    return json.loads(json.dumps(v, default=lambda y: float(y) if isinstance(y, Decimal) else str(y)))


class RunAction(storage.ProjectRef):
    type: str = Field(description="an object type ID (`ontology`)")
    key: list[storage.Value] = Field(min_length=1, description="the object's key value(s), in key column order")
    action: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$", description="an action type ID of that object type (`ontology`)")
    inputs: dict = Field({}, description="what the action type's `inputs` JSON Schema asks for")


@action(Role.viewer)
def run_object_action(conn, user: User, args: RunAction) -> dict:
    """Runs an action type on one object, as you: needs the action type's role (viewer, editor or owner); the object must be visible to you (else 404, as a missing one); `inputs` must match its schema and its checks must hold (else 422 with the check's message). An action type with `confirm` (and any agent call that starts a flow) asks the person to confirm a preview of old and new values first. Its write is one logged, undoable edit batch as you, stored as a proposal an Editor approves when the action type says `approval` or an agent calls it; its flow starts with the object and inputs as {{ data.trigger.object }} and {{ data.trigger.inputs }}, and every step of that run runs as you (your role and row-level security), not as the flow's owner."""
    from abi import flows, tools, writeback  # not at top: they import the semantic package

    p, o = args.project, read(args.project)
    a = next((a for a in o.get("actions", []) if a["id"] == args.action and a["object"] == args.type), None)
    if a is None:
        raise ActionFailed(404, f"No action type '{args.action}' on object type '{args.type}' in project '{p}': `ontology` lists them.")
    need = Role[a.get("role", "editor")]
    if (role_in(conn, user, p) or 0) < need:  # (1) run() checked the Viewer role only
        raise ActionFailed(403, f"Action type '{a['id']}' needs the {need.name} role in project '{p}'.")
    got = get_object(conn, user, Get(project=p, type=args.type, key=args.key))  # (2) a hidden object: 404, as a missing one
    obj, key = _json(dict(zip(got["columns"], got["rows"][0]))), _json(got["key"])
    if errs := tools.validate(a.get("inputs") or {"type": "object", "maxProperties": 0}, args.inputs):  # (3)
        raise ActionFailed(422, f"Inputs don't match the `inputs` schema of action type '{a['id']}'.", errors=errs)
    data = {"object": obj, "inputs": args.inputs, "user": {"id": user.id, "name": user.name}}
    for c in a.get("checks", []):  # (4) the flow `when` shape
        try:
            ok = flows._holds(c, data)
        except ValueError as e:
            raise ActionFailed(422, f"{c['message']} ({e})") from None
        if not ok:
            raise ActionFailed(422, c["message"], check=c["value"])
    m, x, w = model._read(p, "model.json"), next(t for t in o["objects"] if t["id"] == args.type), a.get("writes")
    preview: dict = {"action": a["id"], "object": {"type": x["id"], "key": key, "title": _json(got["title"])}, "inputs": args.inputs}
    if w:
        try:
            vals = _json(flows.resolve(w["set"], data))
        except ValueError as e:
            raise ActionFailed(422, f"Action type '{a['id']}' can't fill its write: {e}") from None
        t = w["table"]
        con, types, wkey = writeback._table(p, t)
        kv, cur = [str(v) if isinstance(v, Decimal) else v for v in _row_of(m, x, t, got["key"])], con.cursor()  # a DECIMAL stays exact
        if gone := sorted(set(vals) - set(types)):
            raise ActionFailed(409, f"Action type '{a['id']}' writes column(s) {', '.join(gone)} that table '{t}' no longer has: fix the action type.")
        now = writeback._row(cur, writeback._src(cur, t), t, wkey, types, kv)  # a system read: old values hidden from you are masked
        hid = rls.hidden(p, user.id).get(t, ())
        if rls.restricted(p, user.id) and not _read(p, f"SELECT 1 FROM {storage._q(t)} WHERE " + " AND ".join(
                f"{storage._q(k)} = {rls.cast_lit(v, types[k])}" for k, v in zip(wkey, kv)) + " LIMIT 1", {"run_object_action": a["id"]}, user.id):
            if t in rls.filtered(p) | set(rls.unfiltered_outputs(p)):  # never write a row you can't see; hidden and missing alike
                raise ActionFailed(404, f"No row of '{t}' you can see for this {x['name']}: action type '{a['id']}' writes only rows you see.")
            hid = {c.lower() for c in vals}  # its row is hidden from you: none of its old values are shown
        preview["writes"] = {"table": t, "row": "changed" if now else "added", "changes": [
            {"column": c, "old": (rls.MARK if c.lower() in hid else now.get(c)) if now else None, "new": v} for c, v in vals.items()]}
    if a.get("flow"):
        preview["flow"] = a["flow"]
    proposed = bool(w) and bool(a.get("approval") or actions.by_agent() or actions.inbound())
    if a.get("confirm") or a.get("flow") and actions.by_agent():  # (5) the token signs the preview: a changed old value asks again
        said = [f"{t}.{c['column']}: {json.dumps(c['old'])} → {json.dumps(c['new'])}" for c in preview.get("writes", {}).get("changes", [])]
        require_confirmation(f"Run '{a['name']}' on {x['name']} {json.dumps(preview['object']['title'])}: " + "; ".join(
            said + ([f"starts flow '{a['flow']}'"] if a.get("flow") and not proposed else [])) + "." + (
            " The write is a proposal an Editor approves; no flow starts." if proposed else ""), preview)
    out: dict = {"action": a["id"], "type": x["id"], "key": key}
    if w:  # (6) as the runner: logged with old and new values, undoable
        status = "proposed" if proposed else "active"
        try:
            batch = writeback.Edits(project=p, table=t, comment=f"action {a['id']}", **(
                {"edits": [writeback.CellEdit(key=kv, column=c, value=v, old=now[c]) for c, v in vals.items()]} if now else
                {"add": [{**vals, **dict(zip(wkey, kv))}]}))
        except ValidationError as e:
            raise ActionFailed(422, f"Action type '{a['id']}' writes a value that isn't a cell value.", errors=actions.invalid(e)) from None
        try:
            out["write"] = wr = writeback._batch(user, batch, status)
        except ActionFailed as e:  # WB-7's "changed to <value>" may quote a value hidden from you
            if e.status == 409 and not t.startswith("same_as_"):  # (a table kept for decide_same says so itself)
                raise ActionFailed(409, f"What action type '{a['id']}' writes changed since it was read; run it again.") from None
            raise
        hide = lambda c: c is not None and c.lower() in hid  # noqa: E731  old values from the system read, as the preview masks them
        wr["changes"] = [ch | {"old": rls.MARK} if hide(ch["column"]) else ch for ch in wr["changes"]]
        wr["rows"] = [[*r[:4], rls.MARK] if hide(r[3]) else r for r in wr["rows"]]
        if status == "active":
            writeback._fire(p, t, out["write"]["snapshot"])
    if a.get("flow") and out.get("write", {}).get("status") == "proposed":  # the flow runs on the write, not on a proposal
        a, out["note"] = {**a, "flow": None}, (f"The write is a proposal an Editor approves (approve_edit); flow"
                                               f" '{a['flow']}' doesn't start for a proposal.")
    try:
        run = a.get("flow") and flows.start(p, a["flow"], f"action-{uuid.uuid4().hex}", by=user, keep=True,  # (7) every step as the runner (flows.execute)
                                            trigger={"action": a["id"], "type": x["id"], "key": key, "object": obj, "inputs": args.inputs})
    except flows.Queued as q:  # the write stays; the run waits as queued, and flows.requeue queues it within a minute or two
        run, out["note"] = q.run, "The flow's run couldn't be queued now; it stays queued and starts once the queue is back."
    if run:
        out["run"] = run
    return out | {"columns": ["status", "run"], "rows": [[out.get("write", {}).get("status"), run and flows.run_key(run)]]}


class ObjectRef(storage.ProjectRef):
    type: str = Field(description="an object type ID (`ontology`)")
    key: list[storage.Value] = Field(min_length=1, description="the object's key value(s), in key column order")
    limit: int = Field(100, ge=1, le=LIMIT, description="entries per kind")


@action(Role.viewer)
def object_history(conn, user: User, args: ObjectRef) -> dict:
    """What happened to one object, newest first, when it is visible to you (else 404, as a missing one): the runs of its action types (who, when, how, inputs), the edit history of its row in its table and in each table its action types write, and the flow runs its action types started."""
    from abi import flows, writeback  # not at top: they import the semantic package

    p = args.project
    got = get_object(conn, user, Get(project=p, type=args.type, key=args.key))
    key, m, o = _json(got["key"]), model._read(p, "model.json"), read(p)
    x = next(t for t in o["objects"] if t["id"] == args.type)
    text = [str(v) for v in key]  # ponytail: keys compared as text (as typed, as read)
    sees = (role_in(conn, user, p) or 0) >= Role.editor  # inputs are typed text: a Viewer sees only their own
    runs = [{"at": r["at"], "user": r["user_name"], "via": r["via"], "action": r["input"].get("action"),
             "inputs": r["input"].get("inputs") if sees or r["user_id"] == user.id else None}
            for r in conn.execute("SELECT at, user_id, user_name, via, input FROM audit_log WHERE project = %s AND action = 'run_object_action'"
                                  " AND status = 200 AND input->>'type' = %s AND ARRAY(SELECT jsonb_array_elements_text(input->'key')) = %s"
                                  # the audit log outlives a project: a new one under a deleted or renamed one's ID starts with no runs
                                  " AND id > COALESCE((SELECT max(id) FROM audit_log WHERE project = %s AND action IN ('delete_project', 'rename_project')"
                                  " AND status = 200), 0) ORDER BY at DESC, id DESC LIMIT %s", [p, x["id"], text, p, args.limit])]
    edits = []
    for t in dict.fromkeys([x["table"], *(a["writes"]["table"] for a in o.get("actions", []) if a["object"] == x["id"] and a.get("writes"))]):
        try:
            h = writeback.edit_history(conn, user, writeback.History(project=p, table=t, key=_row_of(m, x, t, key), limit=args.limit))
        except ActionFailed:  # not loaded, or no longer one-to-one: nothing to list
            continue
        edits.append({"table": t, "columns": h["columns"], "rows": h["rows"]})
    started = [{"run": r["id"], "flow": r["flow"], "status": r["status"], "created": r["created"], "finished": r["finished"]}
               for r in conn.execute("SELECT id, flow, status, created, finished FROM flow_runs WHERE project = %s AND id LIKE %s"
                                     " AND trigger->>'type' = %s AND ARRAY(SELECT jsonb_array_elements_text(trigger->'key')) = %s"
                                     " ORDER BY created DESC, id LIMIT %s", [p, f"{p}/%/action-%", x["id"], text, args.limit])]
    # D-22: times and statuses are quotable; inputs are typed text and stay out of `rows`
    rows = [["run", r["at"], 200, r["user"]] for r in runs] + [["flow", r["created"], r["status"], None] for r in started]
    return {"type": x["id"], "key": key, "runs": runs, "edits": edits,
            "flows": [r | {"run": flows.run_key(r["run"])} for r in started], "columns": ["kind", "time", "status", "user"], "rows": rows}
