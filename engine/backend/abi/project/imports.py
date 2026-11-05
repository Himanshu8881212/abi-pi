"""Import pipeline. PRD §6.6 PC-3, PC-7 (revert applies through it), DECISIONS D-1.

A bundle ({path: content}, M-05) is the whole project. `apply` runs: schema check → reference check →
MEL compile → tool tests → plain-language diff → lineage impact → atomic apply. Every rejection is
HTTP 422 with `errors: [{file, path, error, fix}]` and changes nothing. M-07's revert applies the
target commit's files with `apply`, so the roles rule (D-1) and the table refresh (PC-7) hold there too.
"""

import json
import re
import shutil
from functools import cache
from graphlib import CycleError, TopologicalSorter
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from pydantic import BaseModel, Field, ValidationError

from abi import actions, transform
from abi.actions import ActionFailed, action, require_confirmation
from abi.project import ID, SCHEMA, SCHEMAS, bundle, git
from abi.security import ALLOWED, Role, User, role_in
from abi.semantic import SemanticQuery, lineage, mel, model, planner, rls

ROLES = "security/roles.json"
RLS = git.RLS
MEASURES = "model/measures.json"
LAYOUT = {  # §6.6 paths that have a published schema (M-04); other object types ship with their tasks
    r"project\.json": "project", r"sources/[^/]+\.json": "source", r"tables/[^/]+\.json": "table",
    r"model/model\.json": "model", r"model/glossary\.json": "glossary", r"model/measures\.json": "measures", r"model/datasets\.json": "datasets", r"model/models\.json": "models",
    r"security/roles\.json": "roles", r"security/rls\.json": "rls", r"reports/[^/]+\.json": "report", r"tools/[^/]+\.json": "tool",
    r"docs/descriptions\.json": "descriptions", r"flows/[^/]+\.json": "flow",
    r"agents/[^/]+\.eval\.json": "eval", r"agents/[^/]+\.json": "agent",  # eval first: agents/x.eval.json matches both
    r"ml/[^/]+\.json": "ml", r"environments/[a-z0-9][a-z0-9_-]{0,62}\.json": "environment",  # PC-9, file name = branch
}
CI, CI_MAX = r"\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.yml", 64_000  # PC-13 (X-04.6)
NAMES = {"project": "project settings", "model": "semantic model", "roles": "role assignments", "rls": "row-level security rules"}  # else the kind
FIXES = {
    "required": "Add the missing field.",
    "enum": "Use one of the allowed values.",
    "type": "Change the value to the expected type.",
    "pattern": "Change the value to match the pattern.",
    "minLength": "Fill in a non-empty value.",
    "minItems": "Add at least one item.",
    "minProperties": "Add at least one entry.",
    "uniqueItems": "Remove the duplicate items.",
    "oneOf": "Set `type` to one of the types the file's schema (schemas/v1) allows and give that type's fields.",
}


@cache
def _known(kind: str) -> tuple[frozenset, tuple]:
    """Every property name and patternProperties regex anywhere in schemas/v1/<kind>.json."""
    names, pats = set(), []

    def walk(x):
        if isinstance(x, dict):
            names.update(x.get("properties", {}) if isinstance(x.get("properties"), dict) else ())
            pats.extend(x.get("patternProperties", {}) if isinstance(x.get("patternProperties"), dict) else ())
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(_validator(kind).schema)
    return frozenset(names), tuple(pats)


def json_path(e, kind: str) -> str:
    """`e.json_path` with each key the schema doesn't name (a free map key, a pasted credential) as [*] (SC-4)."""
    names, pats = _known(kind)
    path = "$"
    for k in e.absolute_path:
        if isinstance(k, int):
            path += f"[{k}]"
        elif k not in names and not any(re.search(p, k) for p in pats):
            path += "[*]"
        else:
            path += f".{k}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) else f"['{k}']"
    return path


def _explain(e, kind: str) -> tuple[str, str]:
    """(error, fix) for a schema error, naming fields and rules from the schema only (D-8): never `e.message`, which
    quotes the instance (a pasted credential, SC-4), nor an instance key."""
    sch = e.schema if isinstance(e.schema, dict) else {}  # a boolean subschema (`false`) has no properties
    where, props = f"{SCHEMAS.name}/{kind}.json", sch.get("properties", {})
    if e.validator == "required" and isinstance(e.instance, dict):
        missing = [p for p in e.validator_value if p not in e.instance]
        return (f"Missing required field(s) {', '.join(f'`{p}`' for p in missing)} (see {where}).",
                "Add " + ", ".join(f"`{p}`" + (f": {json.dumps(props[p]['const'])}" if "const" in props.get(p, {}) else "")
                                   for p in missing) + ".")
    if e.validator == "additionalProperties":  # the unexpected key is input: list what's allowed instead
        allowed = [f"`{p}`" for p in props] + [f"keys matching `{p}`" for p in sch.get("patternProperties", {})]
        return (f"A field here isn't allowed by {where}.",
                f"Use only these fields: {', '.join(allowed)}." if allowed else f"Remove the extra field (see {where}).")
    v = e.validator_value  # always the schema's own value
    rule = f": {json.dumps(v)}" if isinstance(v, (str, int, float)) or isinstance(v, list) and all(
        isinstance(x, (str, int, float)) for x in v) else ""  # not subschemas (oneOf, $ref, items)
    fix = f"Use {json.dumps(v)}." if e.validator == "const" else FIXES.get(e.validator, f"See {where}.")
    return f"Fails '{e.validator}'{rule} in {where}.", fix


def _err(file: str, path: str, error: str, fix: str) -> dict:
    return {"file": file, "path": path, "error": error, "fix": fix}


def _kind(rel: str) -> str | None:
    return next((k for p, k in LAYOUT.items() if re.fullmatch(p, rel)), None)


def _id(rel: str) -> str:
    """sources/erp.json -> erp"""
    return rel.rsplit("/", 1)[-1].removesuffix(".json")


def object_id(obj: dict, what: str) -> str:
    """The `id` of an object a save action got (it names the file, so it's checked before any path is built)."""
    if not isinstance(i := obj.get("id"), str) or not re.fullmatch(ID, i):
        raise ActionFailed(400, f"{what} needs an `id` of lowercase letters, digits, - and _, starting with a letter.")
    return i


def _file(project: str, kind: str, id: str):
    p = git.path(project) / ("ml" if kind == "ML model" else f"{kind}s") / f"{id}.json"
    if not p.exists():
        raise ActionFailed(404, f"No {kind} '{id}' in project '{project}'.")
    return p


def load(project: str, kind: str, id: str) -> dict:
    """<kind>s/<id>.json (tools, flows, reports, agents; ML models: ml/); 404 if it's missing."""
    return bundle.loads(_file(project, kind, id).read_text())


def remove(project: str, kind: str, id: str) -> None:
    _file(project, kind, id).unlink()


def objects(project: str, folder: str) -> list[dict]:
    """Every <folder>/*.json object, by file name."""
    d = git.path(project) / folder
    return [bundle.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def write(project: str, objs: dict) -> None:
    """PC-4: writes {rel: object} files that `check` passed, in canonical form."""
    for rel, obj in objs.items():
        p = git.path(project) / rel
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(bundle._encode(rel, obj))  # JSON canonical, other files (tools/<id>.py) as text


@cache
def _validator(kind: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((SCHEMAS / f"{kind}.json").read_text()))


def _schema(conn, project: str, files: dict) -> list[dict]:
    errs = [] if "project.json" in files else [_err("project.json", "$", "The bundle has no project.json.",
                                                    "Add project.json with the project's name and schema_version 1.")]
    for rel, content in files.items():
        try:
            bundle._encode(rel, content)
        except ValueError as e:
            errs.append(_err(str(rel), "$", str(e), "Use a relative path inside the project and UTF-8 text or JSON content."))
            continue
        kind = _kind(rel)
        if kind:
            for e in _validator(kind).iter_errors(content):
                b = best_match([e])
                errs.append(_err(rel, json_path(b, kind), *_explain(b, kind)))
            if kind == "tool" and isinstance(content, dict) and content.get("kind") == "calculation" and f"{rel[:-5]}.py" not in files:
                errs.append(_err(rel, "$.kind", "A calculation tool has no code file.", f"Add {rel[:-5]}.py defining calculate(rows, inputs)."))
        elif re.fullmatch(r"tools/[^/]+\.py", rel):  # TL-5: only the code of a calculation tool; read as text, run only in the sandbox
            if (files.get(f"{rel[:-3]}.json") or {}).get("kind") != "calculation":
                errs.append(_err(rel, "$", "Python code here belongs to no calculation tool.",
                                 f"Add {rel[:-3]}.json with kind \"calculation\", or remove the file."))
            elif "${secret:" in content:  # SC-4: the sandbox has no secrets to give it
                errs.append(_err(rel, "$", "Calculation code can't reference a secret.", "Remove the ${secret:…} reference."))
        elif re.fullmatch(CI, rel):  # PC-13: CI templates ride along as opaque text; the app never parses or runs them
            if len(content.encode()) > CI_MAX:
                errs.append(_err(rel, "$", f"A CI file is limited to {CI_MAX // 1000} KB.", "Shorten the CI file."))
        elif not rel.startswith("docs/"):
            errs.append(_err(rel, "$", "No object type is stored at this path.",
                             "Move it to a §6.6 layout path (project.json, sources/<id>.json, tables/<id>.json, model/model.json,"
                             " model/glossary.json, model/measures.json, model/datasets.json, reports/<id>.json, tools/<id>.json, flows/<id>.json,"
                             " agents/<id>.json, agents/<id>.eval.json, ml/<id>.json, environments/<branch>.json,"
                             " security/roles.json, security/rls.json, docs/, .github/workflows/<name>.yml, .gitlab-ci.yml) or remove it."))
    return errs


def _steps(files: dict) -> dict[str, list]:
    return {_id(rel): c["steps"] for rel, c in files.items() if _kind(rel) == "table" and c["steps"]}  # []: input table (WB-2)


def _references(conn, project: str, files: dict) -> list[dict]:
    errs = [_err(rel, "$.id", f"The id '{c['id']}' differs from the file name.", f"Set id to '{_id(rel)}' or rename the file.")
            for rel, c in files.items() if _kind(rel) in ("source", "table", "report", "tool", "flow", "agent", "ml") and c["id"] != _id(rel)]
    sources = {_id(rel) for rel in files if _kind(rel) == "source"}  # D-9: a source step names a source file
    raw = _steps(files)
    tables = set(raw) | sources  # each source loads into the table of its name
    graph = {}
    for t, s in raw.items():
        rel = f"tables/{t}.json"
        try:
            steps = transform.validate(s)
            transform.compile(t, steps)
        except ValueError as e:
            errs.append(_err(rel, "$.steps", str(e), "Fix the step so it compiles (TR-1); the first step must be the only `source` step."))
            continue
        graph[t] = transform.reads(steps) - {t}
        if steps[0].table:  # M4.2: steps that start from a table the project builds
            if steps[0].table not in tables - {t}:
                errs.append(_err(rel, "$.steps[0].table", f"Unknown table '{steps[0].table}'.",
                                 f"Name a loaded or stepped table other than '{t}': {', '.join(sorted(tables - {t})) or 'none yet'}."))
        elif steps[0].source not in sources:
            errs.append(_err(rel, "$.steps[0].source", f"Unknown source '{steps[0].source}'.",
                             f"Add sources/{steps[0].source}.json or use one of: {', '.join(sorted(sources)) or 'none yet'}."))
        refs = []
        for i, st in enumerate(steps):
            if st.type == "merge":
                refs.append((f"$.steps[{i}].table", st.table))
            if st.type == "append":
                refs += [(f"$.steps[{i}].tables[{j}]", x) for j, x in enumerate(st.tables)]
        errs += [_err(rel, path, f"Unknown table '{x}'.", f"Add tables/{x}.json or use one of: {', '.join(sorted(tables))}.")
                 for path, x in refs if x not in tables]
    for rel, c in files.items():  # PC-15/WB-2: a step-less table becomes an empty input table, so it must be creatable
        if _kind(rel) == "table" and c["steps"] == []:
            errs += _input_table(rel, c)
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as e:
        cycle = e.args[1]
        errs.append(_err(f"tables/{cycle[0]}.json", "$.steps", "Tables read each other in a cycle: " + " → ".join(cycle) + ".",
                         "Remove the source, merge or append step that closes the cycle."))
    measures = {x["id"] for x in (files.get(MEASURES) or {}).get("measures", [])}
    errs += [_err("model/glossary.json", f"$.terms[{i}].measures[{j}]", f"Unknown measure '{m}'.",
                  f"Add it to {MEASURES} or remove it from the term.")
             for i, term in enumerate((files.get("model/glossary.json") or {}).get("terms", []))
             for j, m in enumerate(term.get("measures", [])) if m not in measures]
    errs += [_err("model/datasets.json", f"$.datasets[{i}].query.measures[{j}]", f"Unknown measure '{m}'.",
                  f"Add it to {MEASURES} or remove it from the dataset.")
             for i, ds in enumerate((files.get("model/datasets.json") or {}).get("datasets", []))
             for j, m in enumerate(ds["query"]["measures"]) if m not in measures]
    model = files.get("model/model.json")
    tables = model and {t["table"] for t in model.get("tables", [])}  # None: no model to check against
    errs += [e for rel, c in files.items() if _kind(rel) == "report" for e in _report(rel, c, measures, tables)]
    # ABI-PI: named models (model/models.json) hold model tables and measures; a report names one that exists, and its
    # visuals stay inside it when the model lists its measures
    named = {x["id"]: x for x in (files.get("model/models.json") or {}).get("models", [])}
    date = ((model or {}).get("date_table") or {}).get("table")
    errs += [_err("model/models.json", f"$.models[{i}].tables[{j}]", f"Unknown model table '{t}'.", "Use a table of model/model.json.")
             for i, x in enumerate(named.values()) for j, t in enumerate(x["tables"]) if tables is not None and t not in tables]
    errs += [_err("model/models.json", f"$.models[{i}].measures[{j}]", f"Unknown measure '{m}'.", f"Add it to {MEASURES} or remove it from the model.")
             for i, x in enumerate(named.values()) for j, m in enumerate(x.get("measures", [])) if m not in measures]
    for rel, c in files.items():
        if _kind(rel) != "report" or not c.get("model"):
            continue
        if c["model"] not in named:
            errs.append(_err(rel, "$.model", f"No model '{c['model']}'.", "Add it to model/models.json or remove $.model."))
            continue
        nm = named[c["model"]]
        for pi, pg in enumerate(c.get("pages", [])):
            for vi, v in enumerate(pg.get("visuals", [])):
                q = v.get("query") or {}
                out = [m for m in q.get("measures", []) if nm.get("measures") and m not in nm["measures"]] + [
                    col for col in q.get("dimensions", []) + [f["column"] for f in q.get("filters", [])]
                    if col.split(".", 1)[0] not in {*nm["tables"], date}]
                errs += [_err(rel, f"$.pages[{pi}].visuals[{vi}].query", f"'{x}' is not in model '{c['model']}'.",
                              f"Use the model's tables ({', '.join(nm['tables'])}) and measures, or add it to the model.") for x in out]
    errs += [e for rel, c in files.items() if _kind(rel) == "tool" and "query" in c for e in _query(rel, "$.query", c["query"], measures, tables)]
    # SC-9: an account of another tenant can't own or hold anything here, and reads as an unknown ID (no probing)
    users = {str(r["id"]): r["workspace_owner"] for r in conn.execute(f"SELECT id, workspace_owner FROM users WHERE {ALLOWED}", [project])}
    errs += [_err(rel, "$.owner", f"No user with ID {c['owner']}.", "Use the ID of an existing account.")
             for rel, c in files.items() if _kind(rel) in ("tool", "agent") and c["owner"] not in users]
    errs += [_err(rel, "$.owner", f"No user with ID {c['owner']}.", "Use the ID of an existing account.")  # SH-1: report owners
             for rel, c in files.items() if _kind(rel) == "report" and "owner" in c and c["owner"] not in users]
    errs += [_err("model/datasets.json", f"$.datasets[{i}].owner", f"No user with ID {d['owner']}.", "Use the ID of an existing account.")
             for i, d in enumerate((files.get("model/datasets.json") or {}).get("datasets", [])) if "owner" in d and d["owner"] not in users]
    from abi import flows, ml  # not at top: they import this module
    from abi.agents import studio  # same

    old = bundle.bundle(git.path(project))
    errs += [e for rel, c in files.items() if _kind(rel) == "agent" for e in studio.references(rel, c, files, old)]

    members = {u for u, wo in users.items() if wo} | (files.get(ROLES) or {}).keys()  # a workspace owner is in every project
    errs += [e for rel, c in files.items() if _kind(rel) == "flow" for e in flows.references(rel, c, members, files)]
    errs += [e for rel, c in files.items() if _kind(rel) == "ml" for e in ml.references(rel, c, files)]
    for rel, c in files.items():  # PC-9: an overlay merged over its source file passes the source schema (SC-4 secret-only)
        for sid, part in c["sources"].items() if _kind(rel) == "environment" else ():
            if not isinstance(src := files.get(f"sources/{sid}.json"), dict):
                errs.append(_err(rel, "$.sources[*]", "An entry names no source file (sources/<id>.json).",  # SC-4: the key is input
                                 f"Name one of: {', '.join(sorted(sources)) or 'no source yet'}; or remove the entry."))
            elif e := best_match(_validator("source").iter_errors(src | part)):
                msg, fix = _explain(e, "source")
                errs.append(_err(rel, f"$.sources['{sid}']", f"Merged over sources/{sid}.json, at {json_path(e, 'source')}: {msg}", fix))
    for uid in (files.get(ROLES) or {}).keys() - {"$schema"} - users.keys():
        errs.append(_err(ROLES, f"$['{uid}']", f"No user with ID {uid}.", "Use the ID of an existing account, or remove the entry."))
    return errs


def _input_table(rel: str, c: dict) -> list[dict]:
    names = [x["name"] for x in c["columns"]]
    if not names:
        return [_err(rel, "$.columns", "A table without steps is an input table and needs its columns.",
                     "Declare each column with name and type, or add steps that load it.")]
    errs = [_err(rel, f"$.columns[{i}].name", f"The column '{n}' is declared twice.", "Give each column its own name.")
            for i, n in enumerate(names) if n in names[:i]]
    for i, x in enumerate(c["columns"]):
        try:
            transform._type(x["type"])
        except ValueError as e:
            errs.append(_err(rel, f"$.columns[{i}].type", str(e), "Use a DuckDB type, e.g. VARCHAR, INTEGER, DATE, DECIMAL(18,2)."))
    if "key" not in c:
        errs.append(_err(rel, "$", "A table without steps is an input table, and input tables are always keyed (WB-2).",
                         "Add `key` with the column(s) that identify a row."))
    errs += [_err(rel, f"$.key[{i}]", f"The key column '{k}' is not declared.", f"Use one of: {', '.join(names)}.")
             for i, k in enumerate(c.get("key", [])) if k not in names]
    return errs


def _report(rel: str, r: dict, measures: set, tables: set | None) -> list[dict]:
    """RP-2: a report binds to the model by ID: known measure IDs and model tables, unique page and visual IDs."""
    errs = []

    def cols(path: str, names: list[str]) -> None:
        errs.extend(_err(rel, f"{path}[{i}]", f"Unknown table '{c.split('.', 1)[0]}' in '{c}'.",
                         f"Use a table of model/model.json: {', '.join(sorted(tables))}.")
                    for i, c in enumerate(names) if tables is not None and c.split(".", 1)[0] not in tables)

    def unique(path: str, items: list[dict], what: str) -> None:
        seen = set()
        for i, x in enumerate(items):
            if x["id"] in seen:
                errs.append(_err(rel, f"{path}[{i}].id", f"Two {what}s have the id '{x['id']}'.", f"Give each {what} its own id."))
            seen.add(x["id"])

    cols("$.filters", [f["column"] for f in r.get("filters", [])])
    unique("$.pages", r["pages"], "page")
    for pi, p in enumerate(r["pages"]):
        at = f"$.pages[{pi}]"
        cols(f"{at}.filters", [f["column"] for f in p.get("filters", [])])
        unique(f"{at}.visuals", p["visuals"], "visual")
        for vi, v in enumerate(p["visuals"]):
            errs += _query(rel, f"{at}.visuals[{vi}].query", v.get("query", {}), measures, tables)
    return errs


def _query(rel: str, at: str, q: dict, measures: set, tables: set | None) -> list[dict]:
    """A semantic query (visual or tool) binds by ID: known measure IDs; dimension and filter tables in the model."""
    errs = [_err(rel, f"{at}.measures[{j}]", f"Unknown measure '{m}'.", f"Add it to {MEASURES} or use a measure ID from it.")
            for j, m in enumerate(q.get("measures", [])) if m not in measures]
    for path, names in [(f"{at}.dimensions", q.get("dimensions", [])), (f"{at}.filters", [f["column"] for f in q.get("filters", [])])]:
        errs += [_err(rel, f"{path}[{i}]", f"Unknown table '{c.split('.', 1)[0]}' in '{c}'.",
                      f"Use a table of model/model.json: {', '.join(sorted(tables))}.")
                 for i, c in enumerate(names) if tables is not None and c.split(".", 1)[0] not in tables]
    return errs


def _mel(conn, project: str, files: dict) -> list[dict]:
    """Every measure compiles (SE-3). Tables are checked against model.json; columns live in the lake, so readiness checks them."""
    ms = (files.get(MEASURES) or {}).get("measures", [])
    errs, seen = [], {}
    for i, m in enumerate(ms):
        for key in ("id", "name"):
            if (key, m[key]) in seen:
                errs.append(_err(MEASURES, f"$.measures[{i}].{key}", f"The {key} '{m[key]}' is also used by measures[{seen[key, m[key]]}].",
                                 f"Give each measure its own {key}."))
            seen.setdefault((key, m[key]), i)
    if errs:
        return errs
    model = files.get("model/model.json")
    try:
        mel.compile({m["id"]: m["expression"] for m in ms}, model and {t["table"]: None for t in model.get("tables", []) if not t.get("hidden")})
    except mel.MelError as e:
        at = {m["id"]: i for i, m in enumerate(ms)}
        return [_err(MEASURES, f"$.measures[{at[x['measure']]}].expression", x["error"], x["fix"]) for x in e.errors]
    return []


def _tool_tests(conn, project: str, files: dict) -> list[dict]:
    """TL-1 against the bundle: `inputs` and `outputs` are valid JSON Schemas, and the query plans with each test's
    inputs. The tests themselves need the applied model and loaded data: TL-4 runs them after
    the apply (tools.retest) and disables a tool whose tests fail."""
    from abi import tools  # not at top: tools imports this module

    errs = []
    measures = {m["id"]: m["expression"] for m in (files.get(MEASURES) or {}).get("measures", [])}
    for rel, t in files.items():
        if _kind(rel) != "tool":
            continue
        bad = []
        for key in ("inputs", "outputs"):
            try:
                Draft202012Validator.check_schema(t[key])
            except SchemaError as e:
                bad.append(_err(rel, f"$.{key}{e.json_path[1:]}", f"Not a valid JSON Schema: fails `{e.validator}`.",
                                "Fix it to follow JSON Schema draft 2020-12."))
        if bad:
            errs += bad
            continue
        for i, test in enumerate(t["tests"]):  # none: nothing to fill placeholders with; publish_tool needs a test
            try:
                q = tools.query_for(t, test["inputs"])
                if t.get("kind") != "api":  # an API tool's filled request has nothing to plan
                    planner.plan(SemanticQuery.model_validate(q).model_dump(mode="json"), measures, files.get("model/model.json") or {})
            except ActionFailed as e:
                errs.append(_err(rel, f"$.tests[{i}].inputs", f"{e} {json.dumps(e.body.get('errors', ''))}".strip(),
                                 "Give inputs that match `inputs`, and a default or value for every placeholder."))
                break
            except ValidationError as e:
                errs.append(_err(rel, "$.query", "The filled query isn't a semantic query: " + "; ".join(
                    f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in actions.invalid(e)),
                    "Make each placeholder's input type fit where it is used."))
                break
            except (planner.PlanError, mel.MelError) as e:
                errs.append(_err(rel, "$.query", f"The query doesn't plan: {e}", "Use measure IDs and table.column of the model."))
                break
    return errs


def check(conn, project: str, files: dict) -> None:
    """PC-3 validation stages, in order; the first stage with errors rejects the import."""
    for stage, name in [(_schema, "schema check"), (_references, "reference check"), (_mel, "MEL compile"), (_tool_tests, "tool tests")]:
        if errs := stage(conn, project, files):
            raise ActionFailed(422, f"Import rejected by the {name}: {len(errs)} error(s); nothing changed.", errors=errs)


def role_diff(old: dict | None, new: dict | None) -> list[dict]:
    """D-1: each changed assignment of roles.json as {user, old, new} (None = no role)."""
    old, new = old or {}, new or {}
    return [{"user": u, "old": old.get(u), "new": new.get(u)}
            for u in sorted((old.keys() | new.keys()) - {"$schema"}, key=int) if old.get(u) != new.get(u)]


def _roles(old: dict | None, new: dict | None) -> str:
    return "; ".join(f"user {d['user']}: {d['old'] or 'no role'} → {d['new'] or 'no role'}" for d in role_diff(old, new))


def diff(old: dict, new: dict) -> list[str]:
    """Plain-language changes, one sentence per added, removed or changed file."""
    out = []
    for rel in sorted(old.keys() | new.keys()):
        a, b = old.get(rel), new.get(rel)
        if a == b:
            continue
        kind = _kind(rel)
        what = f"{kind} '{_id(rel)}'" if kind in ("source", "table", "report", "tool", "flow", "agent", "ml", "environment") else NAMES.get(kind, kind or f"file {rel}")
        if a is None or b is None:
            out.append(f"{'Adds' if a is None else 'Removes'} {what}.")
        elif kind == "roles":
            out.append(f"Changes {what}: {_roles(a, b)}.")
        elif isinstance(a, dict) and isinstance(b, dict):
            out.append(f"Changes {what}: " + ", ".join(k for k in sorted(a.keys() | b.keys()) if a.get(k) != b.get(k)) + ".")
        else:
            out.append(f"Changes {what}.")
    return out


def impact(old: dict, new: dict) -> dict:
    """Lineage impact (SE-11 graph): tables to refresh, in dependency order, tables removed, and the reports that
    read a changed table, source or measure."""
    before, after = _steps(old), _steps(new)
    hit = {f"table:{t}" for t in after if before.get(t) != after[t]}
    hit |= {f"source:{_id(rel)}" for rel in new if _kind(rel) == "source" and old.get(rel) != new[rel]}  # and its readers
    for rel in old.keys() | new.keys():  # PC-9: a changed overlay reloads its sources (any branch: a reload is harmless)
        if _kind(rel) == "environment":
            a, b = ((f.get(rel) or {}).get("sources", {}) for f in (old, new))
            hit |= {f"source:{s}" for s in a.keys() | b.keys() if a.get(s) != b.get(s) and f"sources/{s}.json" in new}
    g = lineage.graph(new)
    hit |= set(lineage.reach(g, hit, "downstream"))
    ms = [{m["id"]: m for m in (f.get(MEASURES) or {}).get("measures", [])} for f in (old, new)]
    changed = {f"measure:{m}" for m in ms[0].keys() | ms[1].keys() if ms[0].get(m) != ms[1].get(m)}
    return {"refresh": [n[6:] for n in TopologicalSorter(g).static_order() if n in hit and n.startswith("table:")],
            "removed": removed(old, new),
            "reports": sorted(n[7:] for n in hit | set(lineage.reach(g, changed, "downstream")) if n.startswith("report:"))}


def removed(old: dict, new: dict) -> list[str]:
    """Tables `new` drops: a table file gone, or a source file gone with no table file left (D-9). Reads only paths."""
    def ids(files: dict, kind: str) -> set[str]:
        return {_id(rel) for rel in files if _kind(rel) == kind}

    tables = ids(new, "table")
    return sorted((ids(old, "table") - tables) | (ids(old, "source") - ids(new, "source") - tables))


def _publishing(project: str, old: dict, files: dict) -> tuple[list[dict], list[str]]:
    """§5 #3: the flows `files` switches on, and the confirmation reasons for them and for agents it publishes."""
    from abi import flows  # not at top: flows imports this module

    on = [f for rel, f in sorted(files.items()) if _kind(rel) == "flow" and f["enabled"] and not (old.get(rel) or {}).get("enabled")]
    return on, [flows.enabling(f) for f in on] + [
        f"Publishing agent '{a['id']}' makes it available to everyone in project '{project}'." for rel, a in sorted(files.items())
        if _kind(rel) == "agent" and a["status"] == "published" and (old.get(rel) or {}).get("status") != "published"]


def _sharing(files: dict) -> dict[str, dict]:
    """SH-1: {object: sharing} of every report and agent (its file) and dataset (model/datasets.json#<id>)."""
    out = {rel: f["sharing"] if isinstance(f.get("sharing"), dict) else {}  # an agent's "project": no share
           for rel, f in files.items() if _kind(rel) in ("report", "agent")}
    return out | {f"model/datasets.json#{d['id']}": d.get("sharing") or {}
                  for d in (files.get("model/datasets.json") or {}).get("datasets", [])}


def shared_wider(old: dict, files: dict) -> dict[str, dict]:
    """SH-1 (E-03.md decision 2): the objects whose sharing in `files` reaches someone `old` didn't: {object: sharing}."""
    was = _sharing(old)
    return {k: s for k, s in _sharing(files).items()
            if {str(u) for u in s.get("users", [])} - {str(u) for u in was.get(k, {}).get("users", [])}
            or s.get("workspace") and not was.get(k, {}).get("workspace")}


def api_published(old: dict, files: dict) -> list[str]:
    """D-35: the API tools `files` publishes, or changes while published (as publish_tool: Owner + host confirmation)."""
    return sorted(rel for rel, t in files.items()
                  if _kind(rel) == "tool" and t.get("kind") == "api" and t["status"] == "published" and old.get(rel) != t)


def api_shape(t: dict) -> dict:
    """D-35 confirmation preview of an API tool: the whole saved tool (request with secret names only, never values,
    rows, inputs, permissions, ...) but its publish bookkeeping, so any edit after the preview invalidates the token."""
    return {k: v for k, v in t.items() if k not in ("$schema", "status", "version", "owner")}


def _held(conn, project: str) -> dict:
    """The project's roles as Postgres holds them (D-1: the source of truth), in roles.json form."""
    return {str(r["user_id"]): r["role"] for r in conn.execute("SELECT user_id, role FROM project_roles WHERE project = %s", [project])}


def _rules(files: dict) -> list:
    """Every rule in rls.json, row and column (D-110): both change who sees what, so both take the D-42 gate."""
    f = files.get(RLS) or {}
    return f.get("rules", []) + f.get("columns", [])


def sends(conn, project: str, old: dict, files: dict) -> list[str]:
    """FL-6 targets (as `builtin._first_send` keys them) that the flows this change adds or edits name literally and the
    project hasn't confirmed: a flow run can't confirm, and in production nobody may send by hand (D-56), so a promoted
    flow's mail would fail forever. Templated addresses and an agent step's own sends still confirm at their first send."""
    new = set()
    for rel, f in files.items():
        if _kind(rel) != "flow" or old.get(rel) == f:
            continue
        if n := f.get("notify"):
            new.add("channel:" + n["channel"])
        for s in f["steps"]:
            i = s.get("input") or {}
            if s.get("action") == "send_email" and isinstance(i.get("to"), list):
                new |= {"email:" + a.rsplit("@", 1)[1].lower() for a in i["to"] if isinstance(a, str) and "@" in a and "{{" not in a}
            elif s.get("action") == "post_message" and isinstance(c := i.get("channel"), str) and "{{" not in c and c != "inbox":
                new.add("channel:" + c)  # builtin.INBOX: internal, never confirmed
    known = {r["target"] for r in conn.execute(
        "SELECT target FROM send_targets WHERE project = %s AND target = ANY(%s)", [project, sorted(new)])}
    return sorted(new - known)


def _source_hosts(files: dict) -> set[str]:
    """`ingest.hosts` of every source as any environment (PC-9: environments/<branch>.json overrides) would load it, so
    a later set_environment can't switch a load to a host nobody confirmed."""
    from abi import ingest  # not at top: ingest imports this package

    srcs = {_id(rel): f for rel, f in files.items() if _kind(rel) == "source"}
    envs = [{}] + [f.get("sources") or {} for rel, f in files.items() if _kind(rel) == "environment"]
    return {h for sid, f in srcs.items() for e in envs for h in ingest.hosts(f | (e.get(sid) or {}))}


def source_hosts(conn, project: str, old: dict, files: dict) -> list[str]:
    """H-01.5-fix-8: FL-6 targets `source:<host>` that this change makes the server read from and the project hasn't
    confirmed (the IN-6 worker loads a changed source without asking)."""
    new = _source_hosts(files) - _source_hosts(old)
    known = {r["target"] for r in conn.execute(
        "SELECT target FROM send_targets WHERE project = %s AND target = ANY(%s)", [project, sorted(new)])}
    return sorted(new - known)


def _remote(files: dict) -> dict | None:
    return (files.get("project.json") or {}).get("settings", {}).get("remote")


def gate(conn, project: str, old: dict, files: dict, asking: bool = False) -> tuple[list[str], dict | None]:
    """Confirmation reasons and signed preview of going from `old` to `files`: the exact role diff (D-1) and the API
    tools it publishes (D-35), so a token confirms only what its preview showed. Every caller asking for the same
    call's token (apply, git._confirm) must pass this same preview, or the tokens never match; `asking`: the caller
    asks a confirmation for reasons of its own anyway."""
    why, preview = [], {}
    new = files.get(ROLES, old.get(ROLES))  # D-11: no roles.json keeps roles
    # a changed file makes apply rewrite project_roles from it, so the diff is against what Postgres holds, not the old file
    if old.get(ROLES) != new and (roles := role_diff(held := _held(conn, project), new)):
        why.append(f"This changes who has access to project '{project}': {_roles(held, new)}.")
        preview["roles"] = roles
    if (rules := _rules(old)) != _rules(files):  # D-42: same gate as roles.json, showing old and new rules
        why.append(f"This changes the row-level security rules of project '{project}' (who sees which rows or columns).")
        preview["rls"] = {"old": rules, "new": _rules(files)}
        if not rules and (links := rls.share_links(conn, project)):  # D-82: the first rule revokes them (rls.first_rule)
            why.append(rls.revoking(len(links)))
            preview["revoke_share_links"] = links
    if wide := shared_wider(old, files):  # SH-1: a file widens sharing only past the Owner's eye, as set_sharing does
        why.append(f"This shares {', '.join(sorted(wide))} view-only with accounts that have no role in project '{project}'.")
        preview["sharing"] = wide
    if apis := api_published(old, files):
        hosts = sorted({urlsplit(files[rel]["request"]["url"]).hostname for rel in apis})
        why.append(f"This publishes API tools that let everyone with their role send requests to the outside host(s) {', '.join(hosts)}.")
        preview |= {"hosts": hosts, "api_tools": {rel: api_shape(files[rel]) for rel in apis}}
    # H-01.3-fix-2: pushes would go somewhere new; like actions.PERSON_CLICK, a person's own REST call is one step
    if actions.VIA.get() != "rest" and (new := _remote(files)) and new != _remote(old):
        why.append(f"This points the project's Git remote at {new.get('url')}: a push sends the whole project history there.")
        preview["remote"] = new
    if targets := sends(conn, project, old, files):  # FL-6 with D-56: the promote's Owner confirms its flows' first sends
        why.append(f"First send from project '{project}' to {', '.join(targets)}.")
        preview["send_targets"] = targets
    # H-01.5-fix-8, §5 #3: an agent's (or inbound, or a Confirm click's) change adding a source host asks first; a
    # person's own REST change asks only when it asks anyway, so its token call (always asking) signs the same preview
    if (why or asking or actions.needs_click()) and (hosts := source_hosts(conn, project, old, files)):
        why.append(f"This makes the server read data from the outside host(s) {', '.join(h[7:] for h in hosts)}.")
        preview["source_hosts"] = hosts
    return why, preview or None


def apply(conn, user: User, project: str, files: dict, dry_run: bool = False) -> dict:
    """PC-3 end to end. Call inside a definition=True action: git.change makes it one undoable change and
    restores the old files if anything fails."""
    folder = git.path(project)
    old = bundle.bundle(folder)
    if ROLES not in files and ROLES in old:  # D-11: no roles.json leaves roles as they are; clearing needs an explicit empty file
        files = files | {ROLES: old[ROLES]}
    check(conn, project, files)
    out = {"changes": diff(old, files), "impact": impact(old, files)}
    roles = old.get(ROLES) != files.get(ROLES)
    if roles and (role_in(conn, user, project) or 0) < Role.owner:  # D-1: no self-promotion by JSON edit
        raise ActionFailed(403, "Only a project Owner may change security/roles.json; nothing changed.", errors=[_err(
            ROLES, "$", f"This import changes role assignments ({_roles(_held(conn, project), files.get(ROLES))}).",
            "Ask a project Owner to import it, or keep security/roles.json as it is.")])
    if _rules(old) != _rules(files) and (role_in(conn, user, project) or 0) < Role.owner:  # D-42
        raise ActionFailed(403, "Only a project Owner may change security/rls.json; nothing changed.", errors=[_err(
            RLS, "$.rules", "This change edits the row-level security rules.",
            "Ask a project Owner to make it, or keep security/rls.json as it is.")])
    if (wide := shared_wider(old, files)) and (role_in(conn, user, project) or 0) < Role.owner:
        raise ActionFailed(403, "Only a project Owner may widen who a report, dataset or agent is shared with; nothing changed.", errors=[
            _err(k.split("#")[0], "$.sharing" if "#" not in k else f"$.datasets[id={k.split('#')[1]}].sharing",
                 "This change lets more accounts view it without a role in the project.",
                 "Keep its sharing as it is, or ask a project Owner to make the change.") for k in sorted(wide)])
    apis = api_published(old, files)
    if apis and (role_in(conn, user, project) or 0) < Role.owner:
        raise ActionFailed(403, "Only a project Owner may publish or change a published API tool; nothing changed.", errors=[
            _err(rel, "$.status", "This import publishes an API tool that sends requests to an outside host.",
                 "Import it as a draft and ask a project Owner to publish it.") for rel in apis])
    for rel, f in files.items():  # FL-6: nobody saves a flow (runs use its owner's permissions) or agent in someone else's name
        if (kind := _kind(rel)) in ("flow", "agent") and old.get(rel) != f and f["owner"] != str(user.id):
            raise ActionFailed(403, f"A changed {kind} must be owned by the person importing it; nothing changed.", errors=[_err(
                rel, "$.owner", f"The {kind} would be saved in the name of user {f['owner']}.", f"Set owner to your user ID, {user.id}.")])
    from abi import ingest  # not at top: ingest imports this package
    for loc in sorted(ingest.locations(files) - ingest.locations(old)):  # a server path is only what the importer may read
        ingest.readable(conn, user, project, loc)
    if dry_run:
        return out
    from abi import flows  # not at top: flows imports this module

    on, publishing = _publishing(project, old, files)
    why, preview = gate(conn, project, old, files, bool(publishing))
    why += publishing
    if why:  # one confirmation for every reason (a token is per call)
        require_confirmation(" ".join(why), preview)  # the token confirms this role diff and these API tools only
    for t in (preview or {}).get("send_targets", []) + (preview or {}).get("source_hosts", []):
        conn.execute("INSERT INTO send_targets VALUES (%s, %s) ON CONFLICT DO NOTHING", [project, t])
    for f in on:
        flows.enabled_now(conn, project, f["id"])
    for p in folder.iterdir():
        if p.name != ".git":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    # ponytail: a process crash mid-write leaves a partial tree (git.change rolls back only on exceptions); undo restores it
    bundle.unbundle(files, folder)
    for rel in sorted(files):  # IN-6: a hand-set incremental column is checked at the source now (git.change undoes the write)
        if _kind(rel) == "source" and old.get(rel) != files[rel]:
            ingest.probe(conn, project, _id(rel))
    if _rules(files) and not _rules(old):
        rls.first_rule(conn, project)
    if roles:  # Postgres stays the source of truth (D-1); the file is its mirror
        conn.execute("DELETE FROM project_roles WHERE project = %s", [project])
        for uid, role in (files.get(ROLES) or {}).items():
            if uid != "$schema":
                conn.execute("INSERT INTO project_roles VALUES (%s, %s, %s)", [int(uid), project, role])
    # PC-7: the IN-6 worker reloads a table whose fingerprint is unknown, then everything that reads it
    conn.execute("UPDATE sources SET fingerprint = NULL WHERE project = %s AND table_name = ANY(%s)", [project, out["impact"]["refresh"]])
    out["changes"] += schema_first(conn, project, old, files, user.name)
    if old.get(MEASURES) != files.get(MEASURES):  # V-02 MS-1: measure tests run on change; failures go to the feed
        out["readiness"] = model.report(project)
    return out


def schema_first(conn, project: str, old: dict, files: dict, author: str) -> list[str]:
    """PC-15: each step-less table (checked by `_input_table`) becomes a pure, empty keyed input table when it has no
    lake table or was a loaded one before (`old`: undo/revert of a mapping, D-36): its model entry (unless model.json
    has one) and lake.model.<t> built from the declared columns, raw.<t> and its `sources` row gone, all in one
    snapshot; its edits stay and apply on top. Call last, after every file is written: a failure here raises, the
    files are restored and the snapshot never commits. Returns the plain-language change lines."""
    # not at top: writeback imports the semantic package, which imports this module
    from abi import writeback

    new = {_id(rel): c for rel, c in sorted(files.items()) if _kind(rel) == "table" and c.get("steps") == []}
    if not new:
        return []
    lake = model.lake_columns(project)
    new = {t: c for t, c in new.items() if t not in lake or (old.get(f"tables/{t}.json") or {}).get("steps") != []}
    if not new:
        return []
    m = model.load(project)
    for t, c in new.items():
        e = m["tables"].setdefault(t, {"kind": "fact", "key": None, "description": c.get("description")})
        e["key"] = e["key"] or c["key"]
    model.save(project, m)
    conn.execute("DELETE FROM sources WHERE project = %s AND table_name = ANY(%s)", [project, sorted(new)])
    writeback.create_tables(project, {t: [{"name": x["name"], "type": transform._type(x["type"])} for x in c["columns"]]
                                      for t, c in new.items()}, author)
    return [f"Creates the empty input table '{t}' (schema-first)." if t not in lake else
            f"Makes '{t}' an empty input table again (schema-first; its edits stay)." for t in new]


class Import(BaseModel):
    project: str = Field(pattern=git.ID)
    bundle: dict = Field(description="The whole project as {path: content} (project.bundle.json); files not in it are removed")
    dry_run: bool = Field(False, description="Only validate and show the changes")


@action(Role.editor, definition=True)
def import_project(conn, user: User, args: Import) -> dict:
    """Imports a project bundle: checks schemas, references, measures and tools, lists the changes in plain language and the tables they affect, then replaces the project's files in one undoable step. Rejected imports change nothing and list each error with file, JSON path and fix."""
    return apply(conn, user, args.project, args.bundle, args.dry_run)


class MeasureRef(BaseModel):
    project: str = Field(pattern=git.ID)
    id: str = Field(pattern=ID, description="stable measure ID; other measures refer to it as [id]")


class Measure(MeasureRef):
    name: str = Field(min_length=1, max_length=200, description="display name")
    expression: str = Field(min_length=1, max_length=10000, description="MEL, e.g. SUM(sales.amount) or DIVIDE([sales], [customers])")
    description: str | None = None


def _measures(project: str) -> tuple[dict, list[dict]]:
    files = bundle.bundle(git.path(project))
    return files, (files.get(MEASURES) or {}).get("measures", [])


def _save_measures(conn, user: User, project: str, files: dict, ms: list[dict]) -> dict:
    """UI-2 measure editor: the new model/measures.json applied like an import (PC-3). S-06's reference check refuses
    dropping a measure a report, term or dataset uses; the MEL compile one another measure uses; MS-1 tests run."""
    return apply(conn, user, project, files | {MEASURES: {"$schema": SCHEMA + "measures.json", "measures": ms}})


@action(Role.editor, definition=True)
def set_measure(conn, user: User, args: Measure) -> dict:
    """Adds or replaces a measure (model/measures.json; its tests are kept). Checked like an import: schema, MEL compile against the model's tables and loaded columns, references; then the measure tests run. Rejected with file, JSON path and fix if invalid."""
    files, ms = _measures(args.project)
    at = next((i for i, x in enumerate(ms) if x["id"] == args.id), len(ms))
    m = args.model_dump(exclude={"project"}, exclude_none=True)
    if at < len(ms) and "tests" in ms[at]:
        m["tests"] = ms[at]["tests"]
    ms = ms[:at] + [m] + ms[at + 1:]
    tables, cols = model.visible(model.load(args.project))["tables"], model.lake_columns(args.project)
    try:  # the import's compile knows only model tables; the editor also checks the columns of loaded tables
        mel.compile({x["id"]: x["expression"] for x in ms}, {t: set(cols[t]) if t in cols else None for t in tables} if tables else None)
    except mel.MelError as e:
        if errs := [_err(MEASURES, f"$.measures[{at}].expression", x["error"], x["fix"]) for x in e.errors if x["measure"] == args.id]:
            raise ActionFailed(422, f"Measure rejected by the MEL compile: {len(errs)} error(s); nothing changed.", errors=errs)
    return _save_measures(conn, user, args.project, files, ms)


@action(Role.editor, destructive=True, definition=True)
def delete_measure(conn, user: User, args: MeasureRef) -> dict:
    """Deletes a measure. Refused while a report, glossary term, dataset or another measure uses it."""
    files, ms = _measures(args.project)
    at = next((i for i, x in enumerate(ms) if x["id"] == args.id), None)
    if at is None:
        raise ActionFailed(404, f"No measure '{args.id}' in project '{args.project}'.")
    try:
        return _save_measures(conn, user, args.project, files, ms[:at] + ms[at + 1:])
    except ActionFailed as e:  # the import check words a use as a dangling reference; say it as a use
        used = []
        for x in e.body.get("errors", []):
            if x["error"] in (f"Unknown measure '{args.id}'.", f"Unknown measure [{args.id}]."):
                path = x["path"]
                if x["file"] == MEASURES:  # the MEL path indexes the list without the measure; point into the file as stored
                    path = re.sub(r"^\$\.measures\[(\d+)\]", lambda g: f"$.measures[{int(g[1]) + (int(g[1]) >= at)}]", path)
                used.append(_err(x["file"], path, f"Measure '{args.id}' is used by {x['file']} {path}; remove it there first.",
                                 "Delete or change that use, then delete the measure."))
        if used:
            raise ActionFailed(422, f"Measure '{args.id}' is still used; nothing changed.", errors=used) from None
        raise
