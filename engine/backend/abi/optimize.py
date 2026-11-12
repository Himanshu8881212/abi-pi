"""Optimisers: "what is the best plan?" as data. 1.2.6 M12 (docs/plans/1.2.6.md items 39-42; docs/optimizers.md).

An optimiser is `optimizers/<id>.json` (schemas/v1/optimizer.json): one number decided per object of an object type
(`over`, `decide`), a total to make as high or as low as possible (`objective`) and limits on sums over the objects
(`limits`). A value in the file is a constant, a numeric column of the type's table or a measure read per object. No
code and no industry: the schema's examples (allocation, staffing, maintenance slots) are suggestions.

`run_optimizer` reads the objects through `list_objects` and the measures through `semantic.query` as the person who
runs it, so the plan, its total, the limits it uses and the reason no plan fits are made of what that person sees and
of the file, never of a hidden row. It runs in the worker (a flow step: inline, the step already runs there) with a
time limit and a size cap, and keeps its outcome in Postgres `optimizer_runs`. A plan is never applied: an Editor's
run stores it as a write-back proposal (`writeback.propose_edit`) on `decide.write` that a person approves
(`approve_edit` asks them, whoever calls it); a Viewer's run only shows it.

`solve` is the one door to the solver (HiGHS, MIT, the `highspy` wheel), imported inside it: a project without
optimisers never loads it.
"""

import contextlib
import logging
import math
import re
import time
import uuid
from decimal import Decimal

from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError, field_validator

from abi import db, flows, security, semantic, storage, writeback
from abi.actions import ActionFailed, action
from abi.project import ID, SCHEMA, git, imports
from abi.project.bundle import bundle
from abi.security import Role, User, role_in
from abi.semantic import model, ontology, rls

log = logging.getLogger(__name__)
# ponytail: 1,000 objects a run (one list_objects page; the proposal writes a cell at a time, write-back's own pace).
# Page the read and write the proposal in bulk when plans outgrow it; HiGHS itself takes far more.
CAP = ontology.LIMIT
TIME = 60  # seconds a run may take unless the file says `time_limit_s`
TOL = 1e-6
BIG = 1e12  # the largest number a file may hold: beyond it sums lose their cents and the solver its footing
OPEN = 10  # runs queued or running at once in one project
KEEP = 5  # finished runs kept per optimiser and person: the latest ones
STALE = "The run did not finish (its worker stopped). Run it again."
COLUMNS = ["what", "name", "plan", "now", "limit", "key"]  # a run's rows: the total, each limit, then each object
_NUMERIC = (*ontology._INT, "DOUBLE", "FLOAT", "REAL")


def solve(cost: list[float], lo: list[float | None], hi: list[float | None], integer: bool,
          rows: list[tuple[dict[int, float], float | None, float | None]], maximize: bool, seconds: float) -> tuple[str, list[float] | None]:
    """Item 39, the one door to the solver: the values (one per variable, each within lo..hi, whole when `integer`)
    that make sum(cost * value) as high (`maximize`) or as low as it can be while each row's sum of coefficient * value
    stays within its (least, most); None = no bound. Returns ("optimal", values); ("found", values): the time ran out
    with a plan that holds every limit but isn't proven the best; or ("infeasible" | "unbounded" | "timeout", None);
    "unbounded" also when the solver can't tell it from infeasible (the caller asks again without a cost). Swap the
    solver here: nothing else knows HiGHS."""
    import highspy  # not at top: only a run pays the import
    import numpy as np

    if seconds <= 0:
        return "timeout", None
    h, inf = highspy.Highs(), highspy.kHighsInf
    h.setOptionValue("output_flag", False)
    h.setOptionValue("time_limit", float(seconds))
    h.setOptionValue("mip_rel_gap", 0.0)  # "the best plan" is the best one, not one within 0.01 % of it
    h.setOptionValue("threads", 1)  # the same plan every time, and one core of the worker
    for j, c in enumerate(cost):
        h.addVar(-inf if lo[j] is None else lo[j], inf if hi[j] is None else hi[j])
        h.changeColCost(j, c)
        if integer:
            h.changeColIntegrality(j, highspy.HighsVarType.kInteger)
    for coefs, least, most in rows:
        h.addRow(-inf if least is None else least, inf if most is None else most, len(coefs),
                 np.array(list(coefs), dtype=np.int32), np.array(list(coefs.values()), dtype=np.float64))
    h.changeObjectiveSense(highspy.ObjSense.kMaximize if maximize else highspy.ObjSense.kMinimize)
    h.run()
    st, s = h.getModelStatus(), highspy.HighsModelStatus
    if st == s.kOptimal:
        return "optimal", list(h.getSolution().col_value)
    if st == s.kInfeasible:
        return "infeasible", None
    if st in (s.kUnbounded, s.kUnboundedOrInfeasible):
        return "unbounded", None
    if st == s.kTimeLimit:  # 2 = kSolutionStatusFeasible: the plan found so far holds every limit
        return ("found", list(h.getSolution().col_value)) if h.getInfo().primal_solution_status == 2 else ("timeout", None)
    raise RuntimeError(f"The solver ended with {h.modelStatusToString(st)}.")


# ---------- the file: import checks ----------

def _refs(o: dict) -> list[tuple[str, dict | float]]:
    """(JSON path, value) of every value in the file that may name a property or a measure."""
    return [(f"$.decide.{k}", o["decide"][k]) for k in ("min", "max") if k in o["decide"]] + [("$.objective.per_unit", o["objective"]["per_unit"])] + [
        (f"$.limits[{i}].per_unit", lim["per_unit"]) for i, lim in enumerate(o.get("limits", [])) if "per_unit" in lim]


def references(project: str, files: dict) -> list[dict]:
    """Import checks of the optimiser files (imports._references, after the schema passed): the object type exists and
    has a key; `write`, `group` and each {property} are columns of its table (numeric, but `group`; checked once the
    table is loaded); each {measure} exists and the type then has one key column; filters are list_objects'."""
    err, errs = imports._err, []
    some = {rel: o for rel, o in files.items() if imports._kind(rel) == "optimizer"}
    if not some:  # a project without optimisers pays nothing
        return []
    onto, mj = files.get(ontology.FILE) or {}, files.get("model/model.json") or {}
    measures = {m["id"] for m in (files.get(imports.MEASURES) or {}).get("measures", [])}
    lake = ontology._cols(project)
    for rel, o in some.items():
        if bad := _numbers(o):  # an import's file: save_optimizer refuses it before anything is logged
            errs.append(err(rel, *bad))
            continue
        if o["id"] != imports._id(rel):
            errs.append(err(rel, "$.id", f"The id '{o['id']}' differs from the file name.", f"Set id to '{imports._id(rel)}' or rename the file."))
        got = ontology.keyed(onto, mj, o["over"]["type"])
        if got is None:
            errs.append(err(rel, "$.over.type", f"Unknown object type '{o['over']['type']}' (or its table has no key).",
                            "Use an object type of model/ontology.json: "
                            f"{', '.join(sorted(x['id'] for x in onto.get('objects', []))) or 'none yet (save_ontology first)'}."))
            continue
        x, key = got[0], [k.split(".", 1)[1] for k in got[1]]
        try:
            ontology.Objects.model_validate({"project": project, "type": x["id"], "filters": o["over"].get("filters", [])})
        except ValidationError as e:
            errs.append(err(rel, "$.over.filters", f"A filter isn't one list_objects takes: {e.errors()[0]['msg']}.",
                            "Give each filter {property, op, value} (a list of values with op 'in') and, to filter on a linked type, `link`."))
        cols = lake.get(x["table"])  # None before the table is loaded: its columns are checked by the run

        def column(at: str, c: str, number: bool = True, x=x, cols=cols, rel=rel) -> None:
            if cols is not None and c not in cols:
                errs.append(err(rel, at, f"Table '{x['table']}' of object type '{x['id']}' has no column '{c}'.", f"Use one of: {', '.join(cols)}."))
            elif cols is not None and number and not (cols[c] in _NUMERIC or cols[c].startswith(("DECIMAL", "NUMERIC"))):
                errs.append(err(rel, at, f"Column '{c}' of table '{x['table']}' is {cols[c]}, not a number.", "Use a numeric column, a measure or a constant."))

        if (w := o["decide"]["write"]) in key:
            errs.append(err(rel, "$.decide.write", f"'{w}' is a key column of table '{x['table']}'.", "Name the column the plan is proposed for."))
        elif cols and cols.get(w) in ontology._INT and not o["decide"].get("integer"):
            errs.append(err(rel, "$.decide.integer", f"The plan is proposed for '{w}', a whole-number column, but may hold fractions.",
                            "Set `integer: true`, or propose it for a decimal column."))
        else:
            column("$.decide.write", w)
        for i, lim in enumerate(o.get("limits", [])):
            if "group" in lim:
                column(f"$.limits[{i}].group", lim["group"], number=False)
        for at, v in _refs(o):
            if isinstance(v, dict) and "property" in v:
                column(f"{at}.property", v["property"])
            elif isinstance(v, dict) and v["measure"] not in measures:
                errs.append(err(rel, f"{at}.measure", f"Unknown measure '{v['measure']}'.", "Use a measure ID of model/measures.json, a property or a constant."))
            elif isinstance(v, dict) and len(key) != 1:  # ponytail: one key column, as ML models on objects; a relationship per key column when needed
                errs.append(err(rel, f"{at}.measure", f"Object type '{x['id']}' is identified by {len(key)} columns; a measure is read per object for"
                                                      " types with one key column.", "Use a property or a constant, or a type with one key column."))
    return errs


# ---------- one run, as the person ----------

def _fmt(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"


def _bound(lim: dict) -> str:
    if "equal" in lim:
        return f"exactly {_fmt(lim['equal'])}"
    return " and ".join(f"at {w} {_fmt(lim[k])}" for k, w in (("min", "least"), ("max", "most")) if k in lim)


def _some(names: list[str]) -> str:
    """Up to three names, then "and others": a sentence states no count that its rows don't hold."""
    return ", ".join(names[:3]) + (" and others" if len(names) > 3 else "")


def _broken(o: dict, at: str, why: str) -> ActionFailed:
    """The file no longer fits the model: what a person does next, then file, field and fix for a developer."""
    return ActionFailed(422, f"“{o['name']}” can't be worked out: it is set up for something that isn't there any more. Ask the agent to repair"
                             f" this plan. (For a developer: optimizers/{o['id']}.json {at}: {why})")


def _plan(conn, project: str, o: dict, user: User) -> dict:
    """Items 41, 42: optimiser `o` over the objects `user` sees. The outcome as a dict: `outcome` (best; found: the
    best plan found when the time ran out; infeasible, unbounded, timeout, empty), `text` (in the business's words),
    `columns`/`rows` (COLUMNS: every figure the text states is a value of them) and, for an Editor's plan that changes
    something, the `proposal` it was stored as."""
    start, seconds = time.monotonic(), o.get("time_limit_s", TIME)
    left = lambda: seconds - (time.monotonic() - start)  # noqa: E731
    x = next((t for t in ontology.read(project).get("objects", []) if t["id"] == o["over"]["type"]), None)
    if x is None:
        raise _broken(o, "$.over.type", f"no object type '{o['over']['type']}' in this project; name one `ontology` lists (save_optimizer).")
    try:
        args = ontology.Objects.model_validate({"project": project, "type": x["id"], "filters": o["over"].get("filters", []), "limit": CAP})
    except ValidationError as e:
        raise _broken(o, "$.over.filters", f"{e.errors()[0]['msg']}; give each filter {{property, op, value}} as list_objects takes them"
                                           " (save_optimizer).") from None
    got = ontology.list_objects(conn, user, args)  # as the person: row-level security, hidden columns, the query log
    one, many, dec, obj = x["name"], x["plural"], o["decide"], o["objective"]
    if got["more"]:  # the cap itself is in the docs: a refusal has no rows to hold a figure
        raise ActionFailed(422, f"“{o['name']}” would plan for more {many} than one run takes. Plan for fewer at a time, for example one region"
                                " or one period per plan: ask the agent to narrow this plan.")
    cols, objs, key, n = got["columns"], got["rows"], got["key"], len(got["rows"])
    out = {"optimizer": o["id"], "type": x["id"], "time_limit_s": seconds, "columns": COLUMNS, "rows": []}
    if not n:
        return out | {"outcome": "empty", "text": f"There is nothing to plan: “{o['name']}” found no {many} you can see"
                                                  + (" that match its filters." if o["over"].get("filters") else ".")}
    labels = {p["column"]: p["name"] for p in x.get("properties", []) if p.get("name")}
    label = lambda c: labels.get(c) or ontology._label(c)  # noqa: E731  only ever of a column this person reads
    title = [str(r[cols.index(got["title"])]) if got["title"] in cols else " ".join(str(r[cols.index(k)]) for k in key) for r in objs]
    keys = [[v if isinstance(v, str | int | float | bool) else str(v) for v in (r[cols.index(k)] for k in key)] for r in objs]

    def number(v) -> float | None:
        return float(v) if isinstance(v, int | float | Decimal) and not isinstance(v, bool) and math.isfinite(v) else None

    def column(c: str) -> int:
        if c not in cols:  # missing, or hidden from this person: alike, never read and never named
            raise ActionFailed(422, f"“{o['name']}” can't be worked out for you: it needs a figure of the {many} that isn't there or that you"
                                    " can't read.")
        return cols.index(c)

    def missing(j: int, what: str) -> ActionFailed:
        return ActionFailed(422, f"“{o['name']}” can't be worked out: {one} “{title[j]}” has no number for {what}."
                                 f" Fill it in, or ask the agent to leave such {many} out of this plan.")

    mids = list(dict.fromkeys(v["measure"] for _, v in _refs(o) if isinstance(v, dict) and "measure" in v))
    per: dict = {}
    if mids:
        if len(key) != 1:
            raise _broken(o, "$", f"a measure is read per object for types with one key column; {one} has {len(key)}. Use a property.")
        k = f"{x['table']}.{key[0]}"
        q = semantic.query(project, {"measures": mids, "dimensions": [k], "filters": [{"column": k, "op": "in", "value": [v[0] for v in keys]}],
                                     "limit": 100_000}, user.id, timeout=semantic.TIMEOUT)
        per = {str(r[0]): r[1:] for r in q["rows"]}
    names = {m["id"]: m.get("name") or m["id"] for m in model._read(project, "measures.json").get("measures", [])}

    def vector(v) -> list[float]:
        if not isinstance(v, dict):
            return [float(v)] * n
        vals = []
        for j, r in enumerate(objs):
            if "measure" in v:  # no rows for this object: unknown, never a silent 0
                f, what = number(per.get(str(keys[j][0]), [None] * len(mids))[mids.index(v["measure"])]), names.get(v["measure"], v["measure"])
            else:
                f, what = number(r[column(v["property"])]), label(v["property"])
            if f is None:
                raise missing(j, what)
            vals.append(f)
        return vals

    def said(v) -> str:
        return _fmt(v) if not isinstance(v, dict) else f"its {label(v['property'])}" if "property" in v else f"its {names.get(v['measure'], v['measure'])}"

    w = column(dec["write"])
    if dec["write"] in key:
        raise _broken(o, "$.decide.write", f"'{dec['write']}' identifies a {one}; name the column the plan is proposed for (save_optimizer).")
    cost, lo = vector(obj["per_unit"]), vector(dec.get("min", 0))
    hi = vector(dec["max"]) if "max" in dec else [None] * n
    integer, maximize = bool(dec.get("integer")), obj["goal"] == "maximize"
    typ = model.lake_columns(project).get(x["table"], {}).get(dec["write"], "")
    if not integer and typ in ontology._INT:
        raise _broken(o, "$.decide.integer", f"the plan is proposed for '{dec['write']}', a whole-number column, but may hold fractions;"
                                             " set `integer: true`, or propose it for a decimal column (save_optimizer).")
    # a plan's numbers as the column holds them: a DECIMAL(10,2) column gets two decimals. ponytail: rounded after the
    # solve, so a limit can be off by under half a unit of the last decimal per object; solve in the column's units if that matters
    places = int(d.group(1)) if (d := re.fullmatch(r"(?:DECIMAL|NUMERIC)\(\d+,\s*(\d+)\)", typ)) else 6
    limits = []  # per limit of the file: its rows (coefficients, least, most, name), one per group value
    for lim in o.get("limits", []):
        a = vector(lim.get("per_unit", 1))
        least, most = (lim["equal"], lim["equal"]) if "equal" in lim else (lim.get("min"), lim.get("max"))
        groups: dict = {}
        if "group" in lim:
            g = column(lim["group"])
            for j, r in enumerate(objs):
                groups.setdefault(f"{lim['name']}: {'none' if r[g] is None else r[g]}", []).append(j)
        limits.append([({j: a[j] for j in js if a[j]}, least, most, name) for name, js in (groups or {lim["name"]: range(n)}).items()])

    def run(c: list[float], units: list) -> tuple[str, list[float] | None]:
        """Solved with the limits `units` holds: "least" and "most" (the bounds per object) and indexes into `limits`."""
        return solve(c, lo if "least" in units else [None] * n, hi if "most" in units else [None] * n, integer,
                     [r[:3] for u in units if isinstance(u, int) for r in limits[u]], maximize, left())

    units = ["least", *(["most"] if "max" in dec else []), *range(len(limits))]
    state, vals = run(cost, units)
    if state == "unbounded" and (again := run([0.0] * n, units)[0]) not in ("optimal", "found"):
        state = again  # the solver couldn't tell: without a cost, it is infeasible (or out of time)
    if state == "timeout":
        return out | {"outcome": "timeout", "text": f"“{o['name']}” found no plan in the time it is allowed, so nothing is proposed. Plan for fewer"
                                                    f" {many} at a time, or ask the agent to allow this plan more time."}
    if state == "unbounded":
        total, way, edge = obj.get("name") or "the total", ("grow", "fall")[not maximize], ("the most", "the most or the least")[not maximize]
        return out | {"outcome": "unbounded", "text": f"“{o['name']}” has no best plan: {total} can {way} without end."
                                                      f" Add a limit: {edge} {dec['name']} per {one}, or a limit on a total."}
    if state == "infeasible":
        keep = units  # a deletion filter: a limit the rest still conflicts without is not part of the conflict
        for u in units:
            st = run([0.0] * n, trial := [k for k in keep if k != u])[0]
            if st == "timeout":
                keep = None
                break
            if st == "infeasible":
                keep = trial
        if keep is None:
            return out | {"outcome": "infeasible", "text": f"No plan fits: the limits of “{o['name']}” can't all hold. Which of them conflict could"
                                                           " not be worked out in the time it is allowed. Loosen a limit and run it again, or ask"
                                                           " the agent which limits to look at."}
        for u in keep:
            if isinstance(u, int):
                lim = o["limits"][u]
                name, at = f"{lim['name']} ({_bound(lim)}{', for each ' + label(lim['group']) if 'group' in lim else ''})", lim.get("equal", lim.get("max", lim.get("min")))
            else:
                v = dec.get("min", 0) if u == "least" else dec["max"]
                name, at = f"{dec['name']} per {one}: at {u} {said(v)}", None if isinstance(v, dict) else v
            out["rows"].append(["Limit in conflict", name, None, None, at, None])
        return out | {"outcome": "infeasible", "text": "No plan fits: these limits can't all hold: " + "; ".join(r[1] for r in out["rows"])
                                                       + ". Relax one of them, then run it again."}

    plan = [int(round(v)) if integer else round(v, places) + 0.0 for v in vals]
    now = [number(r[w]) for r in objs]
    total = round(sum(c * p for c, p in zip(cost, plan)), 6)
    before = None if None in now else round(sum(c * v for c, v in zip(cost, now)), 6)
    near = lambda a, b: abs(a - b) <= TOL * max(1.0, abs(b))  # noqa: E731
    # what: Limit (at most), Limit fully used; At least, At least, just met; Exactly. plan = what the plan uses, limit = the bound
    rows, full, met = [["Total", obj.get("name") or "Total", total, before, None, None]], [], []
    for rs in limits:
        for coefs, least, most, name in rs:
            used = round(sum(c * plan[j] for j, c in coefs.items()), 6)
            if least is not None and least == most:
                rows.append(["Exactly", name, used, None, most, None])
            elif most is not None and (near(used, most) or least is None or not near(used, least)):
                rows.append(["Limit fully used" if near(used, most) else "Limit", name, used, None, most, None])
                full += [name] * near(used, most)
            else:
                rows.append(["At least, just met" if near(used, least) else "At least", name, used, None, least, None])
                met += [name] * near(used, least)
    order = sorted(range(n), key=lambda j: (-plan[j], title[j]))
    top = [title[j] for j in order if hi[j] is not None and near(plan[j], hi[j])]
    for j in order:
        edge = "at its most" if hi[j] is not None and near(plan[j], hi[j]) else "at its least" if "min" in dec and near(plan[j], lo[j]) else None
        rows.append([one, title[j], plan[j], ontology._json(objs[j][w]) if now[j] is not None else None, edge, keys[j]])
    given = [j for j in order if plan[j]]
    best = state == "optimal"
    text = (f"The best plan of “{o['name']}” gives " if best else f"The best plan of “{o['name']}” found in the time it is allowed gives ") + (", ".join(
        f"{_fmt(plan[j])}{' ' + dec['name'] if i == 0 else ''} to {title[j]}" for i, j in enumerate(given[:3])) or f"no {dec['name']} to any {one}")
    text += (f", and some to other {many}" if len(given) > 3 else "") + f". {obj.get('name') or 'Total'}: {_fmt(total)}"
    text += (f" (now {_fmt(before)})" if before is not None else "") + "."
    text += "" if best else " A better plan may exist: allow it more time to be sure."
    text += f" Fully used: {', '.join(full)}." if full else " No limit is fully used." if limits and not met else ""
    text += f" Just met: {', '.join(met)}." if met else ""
    text += f" At their most {dec['name']}: {_some(top)}." if top else ""
    out |= {"outcome": "best" if best else "found", "rows": rows}
    edits = [writeback.CellEdit(key=keys[j], column=dec["write"], value=plan[j]) for j in range(n) if now[j] is None or not near(now[j], plan[j])]
    if (role_in(conn, user, project) or Role.viewer) < Role.editor:
        return out | {"text": text + f" This is the plan for the {many} you see; nothing is changed. An Editor's run proposes it as changes for approval."}
    # item 41: never applied. One open proposal per optimiser: the ones its earlier runs left open are withdrawn first
    was = {r["batch"] for r in conn.execute("SELECT result->'proposal'->>'batch' AS batch FROM optimizer_runs WHERE project = %s AND optimizer = %s"
                                            " AND result->'proposal' IS NOT NULL", [project, o["id"]])}
    old = [p for p in writeback.edit_proposals(conn, user, storage.ProjectRef(project=project))["proposals"] if p["batch"] in was]
    for p in old:
        with contextlib.suppress(ActionFailed):  # decided by a person meanwhile
            writeback.reject_edit(conn, user, writeback.Proposal(project=project, table=p["table"], batch=p["batch"]))
    if not edits:
        text += f" The {many} already have these values, so there is nothing to propose."
    else:  # a proposal a person approves (approve_edit asks them, whoever calls it)
        p = writeback.propose_edit(conn, user, writeback.Edits(project=project, table=x["table"], edits=edits,
                                                               comment=f"The best plan of “{o['name']}”"[:2000]))
        out["proposal"] = {"table": x["table"], "batch": p["batch"], "changes": len(edits)}
        text += " Proposed as changes for a person to approve; nothing is changed until then."
    return out | {"text": text + (" An earlier proposal of this plan that was still open is withdrawn." if old else "")}


def _work(conn, run_id: str, project: str, o: dict, user: User) -> dict:
    """Run `run_id` to its end and keep the outcome on it, committed. A refusal (ActionFailed) is kept in its own words;
    any other failure, the outcome's own write included, only as "failed" (its text could quote a row; it is in the
    server log)."""
    start = time.monotonic()
    try:
        out = _plan(conn, project, o, user) | {"seconds": round(time.monotonic() - start, 1)}
        conn.execute("UPDATE optimizer_runs SET status = 'ok', finished = now(), result = %s WHERE id = %s", [Jsonb(out), run_id])
        conn.commit()
    except Exception as e:
        if not isinstance(e, ActionFailed):
            log.exception("optimizer run %s failed", run_id)
        conn.rollback()  # a failed statement leaves nothing to write in; the run's own row was committed when it started
        conn.execute("UPDATE optimizer_runs SET status = 'failed', finished = now(), error = %s WHERE id = %s",
                     [str(e) if isinstance(e, ActionFailed) else "The run failed unexpectedly; the details are in the server log.", run_id])
        conn.commit()  # the failed run stays in the registry
        raise
    return {"run": run_id, "status": "ok", **out}


def interrupted(run_id: str | None = None) -> None:
    """A run nobody is working on ends failed, so it never reads "running" for ever and never blocks the next one:
    `run_id` whose worker died (worker.retry_stalled_runs), and any run open for longer than a run can take."""
    with db.connect() as conn:
        conn.execute("UPDATE optimizer_runs SET status = 'failed', finished = now(), error = %s WHERE status IN ('queued', 'running')"
                     " AND (id = %s OR created < now() - interval '30 minutes')", [STALE, run_id])


def job(run_id: str) -> None:
    """The worker's job (abi.worker.run_optimizer): a queued run, as the person who asked for it."""
    with db.connect() as conn:
        r = conn.execute("UPDATE optimizer_runs SET status = 'running' WHERE id = %s AND status = 'queued' RETURNING project, spec, user_id",
                         [run_id]).fetchone()
        u = r and security.user(conn, r["user_id"])
    if r is None or u is None:  # not queued any more; a deleted account's runs went with it
        return
    with db.connect() as conn:
        try:
            _work(conn, run_id, r["project"], r["spec"], u)
        except Exception:  # noqa: BLE001, S110 - the failure is the run's outcome (_work kept it)
            pass


# ---------- actions ----------

class Ref(storage.ProjectRef):
    optimizer: str = Field(pattern=ID, description="optimiser ID (file name in optimizers/)")


class Which(storage.ProjectRef):
    optimizer: str | None = Field(None, pattern=ID, description="one optimiser: its latest run's plan as rows; none: the list")


def _numbers(x, at: str = "$", size: bool = True) -> tuple[str, str, str] | None:
    """(JSON path, error, fix) of the first number in the file that can't be used; None when every one can.
    Without `size`: only what is no number at all."""
    if isinstance(x, dict | list):
        return next((bad for k, v in (x.items() if isinstance(x, dict) else enumerate(x))
                     if (bad := _numbers(v, f"{at}[{k}]" if isinstance(x, list) else f"{at}.{k}", size))), None)
    if isinstance(x, float) and not math.isfinite(x):
        return at, "This is not a number (NaN or infinity).", "Give a finite number."
    if size and isinstance(x, int | float) and not isinstance(x, bool) and abs(x) > BIG:
        return at, f"This number is beyond {BIG:g}, the largest an optimiser takes.", "Use a smaller unit (thousands, millions)."
    return None


class Save(storage.ProjectRef):
    optimizer: dict = Field(description=(
        "The optimiser (schemas/v1/optimizer.json): {id, name, description?, over: {type: an object type ID, filters?: list_objects' filters},"
        " decide: {name, write: the column the plan is proposed for, min?, max?, integer?}, objective: {goal: maximize|minimize, name?, per_unit},"
        " limits?: [{name, per_unit?, group?: a column, min? | max? | equal?}], time_limit_s?}; per_unit, min and max of decide are a number,"
        " {property: a numeric column} or {measure: a measure ID}."))

    @field_validator("optimizer")
    @classmethod
    def _finite(cls, o: dict) -> dict:  # before anything is logged or written: NaN is no JSON
        if bad := _numbers(o, size=False):  # a number too large is refused with the file's other checks (file, field, fix)
            raise ValueError(f"optimizers/<id>.json {bad[0]}: {bad[1]} {bad[2]}")
        return o


@action(Role.editor, definition=True)
def save_optimizer(conn, user: User, args: Save) -> dict:
    """Creates or replaces an optimiser (optimizers/<id>.json), checked like an import; it changes no data. Next: run_optimizer."""
    o = {**args.optimizer, "$schema": SCHEMA + "optimizer.json"}
    rel = f"optimizers/{imports.object_id(o, 'An optimiser')}.json"
    imports.check(conn, args.project, bundle(git.path(args.project)) | {rel: o})
    imports.write(args.project, {rel: o})
    return {"id": o["id"]}


@action(Role.editor, destructive=True, definition=True)
def delete_optimizer(conn, user: User, args: Ref) -> dict:
    """Deletes an optimiser's definition; its runs and the proposals made from them stay."""
    imports.remove(args.project, "optimizer", args.optimizer)
    return {}


@action(Role.viewer)
def run_optimizer(conn, user: User, args: Ref) -> dict:
    """Works out an optimiser's best plan over the objects you see, in the worker (read it with `optimizers`); an Editor's plan is stored as a proposal a person approves, never applied."""
    o, p = imports.load(args.project, "optimizer", args.optimizer), args.project
    interrupted()  # runs left open by a worker that died no longer count
    run_id, inline = str(uuid.uuid4()), flows.IN_FLOW.get()  # a flow step already runs in the worker
    # one statement: at most OPEN runs open in the project, and (the unique index optimizer_runs_open) one per person and optimiser
    if not conn.execute("INSERT INTO optimizer_runs (id, project, optimizer, user_id, restricted, spec, status) SELECT %s, %s, %s, %s, %s, %s, %s"
                        " WHERE (SELECT count(*) FROM optimizer_runs WHERE project = %s AND status IN ('queued', 'running')) < %s"
                        " ON CONFLICT DO NOTHING", [run_id, p, o["id"], user.id, rls.restricted(p, user.id), Jsonb(o),
                                                    "running" if inline else "queued", p, OPEN]).rowcount:
        mine = conn.execute("SELECT 1 FROM optimizer_runs WHERE project = %s AND optimizer = %s AND user_id = %s AND status IN ('queued', 'running')",
                            [p, o["id"], user.id]).fetchone()
        raise ActionFailed(409, f"“{o['name']}” is already being worked out for you; its plan shows in a moment." if mine else
                                "Too many plans are being worked out in this project right now. Try again in a moment.")
    conn.execute("DELETE FROM optimizer_runs WHERE project = %s AND optimizer = %s AND user_id = %s AND status IN ('ok', 'failed') AND id NOT IN"
                 " (SELECT id FROM optimizer_runs WHERE project = %s AND optimizer = %s AND user_id = %s ORDER BY created DESC LIMIT %s)",
                 [p, o["id"], user.id, p, o["id"], user.id, KEEP])
    conn.commit()  # before the worker can pick the job up; a flow's run: before its outcome is kept
    if inline:
        out = _work(conn, run_id, p, o, user)
        if out["outcome"] not in ("best", "found"):  # a flow's step that found no plan is no "ok"
            raise ActionFailed(409, out["text"])
        return out
    from abi import worker  # not at top: the worker imports every action module

    try:
        worker.defer(worker.run_optimizer, run_id=run_id)
    except Exception:
        conn.execute("DELETE FROM optimizer_runs WHERE id = %s", [run_id])
        raise
    return {"run": run_id, "status": "queued", "columns": ["run", "status"], "rows": [[run_id, "queued"]]}


def _shown(o: dict) -> dict:
    """An optimiser as a restricted reader gets it: its business names, and no column, filter or figure of the file
    (a column may be hidden from them; authored text may quote rows)."""
    return {"id": o["id"], "name": o["name"], "over": {"type": o["over"]["type"]}, "decide": {"name": o["decide"]["name"]},
            "objective": {k: v for k, v in o["objective"].items() if k in ("goal", "name")}, "limits": [{"name": lim["name"]} for lim in o.get("limits", [])]}


@action(Role.viewer)
def optimizers(conn, user: User, args: Which) -> dict:
    """The project's optimisers, each with the latest run made of what you see (a restricted Viewer: their own); with `optimizer`, that run's plan as rows."""
    files = imports.objects(args.project, "optimizers")
    if not files and args.optimizer is None:  # a project without optimisers: no read
        return {"optimizers": []}
    # a run is made of what its runner sees: a restricted reader gets the runs they made restricted, anyone else those
    # made over every row (checked now, not when the run was queued: a person restricted since doesn't keep an old one)
    hide = rls.restricted(args.project, user.id)
    runs = {r.pop("optimizer"): r for r in conn.execute(
        "SELECT DISTINCT ON (optimizer) optimizer, id::text AS run, status, created::text, finished::text, error, result FROM optimizer_runs"
        f" WHERE project = %s AND {'restricted AND user_id = %s' if hide else 'NOT restricted'} ORDER BY optimizer, created DESC",
        [args.project, *([user.id] if hide else [])])}
    out = []
    for o in files:
        r = runs.get(o["id"])
        out.append((_shown(o) if hide else {k: v for k, v in o.items() if k != "$schema"})
                   | {"latest_run": r and {k: v for k, v in r.items() if k != "result"} | (r["result"] or {})})
    if args.optimizer is None:
        return {"optimizers": out}
    if (o := next((o for o in out if o["id"] == args.optimizer), None)) is None:
        raise ActionFailed(404, f"No optimizer '{args.optimizer}' in project '{args.project}'.")
    r = o.pop("latest_run") or {"status": "never run", "columns": COLUMNS, "rows": []}
    return {"optimizer": o} | {"columns": COLUMNS, "rows": []} | {k: v for k, v in r.items() if k != "optimizer"}
