"""Visual specs, auto chart, filters, how-is-this-calculated. PRD §6.7 RP-2..RP-5 (backend; the canvas is S-07).

A report is `reports/<id>.json` (schemas/v1/report.json): pages of visuals, each bound to the model by ID only
(PC-1): its `query` is the semantic query format. Every visual's data comes from the semantic engine
(`semantic.query`, principle 4). Filters (RP-4) are ANDed in order: report, page, visual, then `interaction`
(slicer selections and cross-filters the canvas sends with the request). Chart type and number format are
chosen by the deterministic rules below (RP-3, principle 5: automatic, overridable in the spec).
"""

import contextvars
import functools
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from abi import actions, db, semantic, storage
from abi.actions import ActionFailed, action
from abi.project import ID, SCHEMA, git, imports
from abi.project.bundle import bundle
from abi.security import Role, User, role_in
from abi.semantic import Filter, live, mel, model, planner

PCT = re.compile(r"%|pct|percent", re.IGNORECASE)
COUNTRY = re.compile(r"countr|nation", re.IGNORECASE)
WORLD = Path(__file__).resolve().parents[3] / "frontend" / "src" / "world.json"  # the map's shapes (X-05.4)


@functools.cache
def countries() -> frozenset[str]:
    """Lower-cased names and ISO-2/ISO-3 codes of the map's countries (Natural Earth 1:110m)."""
    return frozenset(s.lower() for f in json.loads(WORLD.read_text())["features"]
                     for s in (f["properties"]["name"], f["properties"].get("iso_a2"), f["properties"].get("iso_a3"),
                               *f["properties"].get("alt", [])) if s)


def _visuals(r: dict, page: str | None, visual: str | None) -> list[tuple[dict, dict]]:
    out = [(p, v) for p in r["pages"] if page in (None, p["id"]) for v in p["visuals"] if visual in (None, v["id"])]
    if not out:
        raise ActionFailed(404, f"No page '{page}'{f' with visual {visual!r}' if visual else ''} in report '{r['id']}'.")
    return out


def _shared_filters(conn, user: User, project: str, r: dict, interaction: list[Filter]) -> None:
    """SH-1: an account reading the report through a share (no role) filters only on the report's own dimensions (its
    slicers and cross-filters), so a share can't slice the shown measures by other columns of the model."""
    dims = {d for p in r["pages"] for v in p["visuals"] for d in v.get("query", {}).get("dimensions", [])}
    if (bad := sorted({f.column for f in interaction} - dims)) and role_in(conn, user, project) is None:
        raise ActionFailed(403, f"A shared report takes filters only on its own columns, not {', '.join(bad)}.")


def _query(project: str, r: dict, p: dict, v: dict, interaction: list[dict]) -> tuple[dict, list[dict], list[str]]:
    """RP-4: the visual's query with every filter that applies to it, those filters with their level, and the columns
    of interaction filters left out (UI-1.4.3): a slicer or cross-filter on a dimension some measure of the visual
    can't reach doesn't filter that visual, which says so; report, page and visual filters stay refused (D-8)."""
    trees = mel.compile({m["id"]: m["expression"] for m in model._read(project, "measures.json").get("measures", [])})
    facts = {n.col.table for m in v["query"]["measures"] for n in mel._walk(trees.get(m)) if isinstance(n, mel.Agg)}
    mj = model._read(project, "model.json")

    def reach(f: dict) -> bool:
        t = f["column"].split(".", 1)[0]
        try:
            return all(t == x or planner.path(mj, x, t) for x in facts)
        except planner.PlanError:
            return False
    off = list(dict.fromkeys(f["column"] for f in interaction if not reach(f)))
    fs = [{"level": lvl, **f} for lvl, fs in [("report", r.get("filters", [])), ("page", p.get("filters", [])),
                                              ("visual", v["query"].get("filters", [])),
                                              ("interaction", [f for f in interaction if f["column"] not in off])]
          for f in fs]
    return {**v["query"], "filters": [{k: f[k] for k in ("column", "op", "value")} for f in fs]}, fs, off


def auto_type(project: str, q: dict, members: list = (), styles: list = ()) -> str:
    """RP-3 chart type: no dimension → kpi; a time dimension (date table or DATE/TIMESTAMP column) among one or two
    → line (the other is the series); one country dimension (`members`: its values) and one measure → map; one
    dimension and two measures of which exactly one is a percent (`styles`: their format styles) → combo (RP-8: the
    amount as bars, the share as a line); one dimension → bar; two → matrix; more → table. A country column: every
    value is a country name or ISO-2/ISO-3 code, or its name says country/nation and some value is one. Waterfall,
    funnel, heatmap, treemap and Sankey carry meaning the query doesn't (steps, stages, flows): set in the spec only."""
    dims = q.get("dimensions", [])
    if not dims:
        return "kpi"
    if len(dims) > 2:
        return "table"
    date = (model.load(project)["date_table"] or {}).get("table")
    cols = model.lake_columns(project)
    for d in dims:
        t, c = d.split(".", 1)
        if t == date or cols.get(t, {}).get(c, "").startswith(("DATE", "TIMESTAMP")):
            return "line"
    if len(dims) == 1 and len(q["measures"]) == 1:
        vals = {str(x).lower() for x in members if x is not None}
        if vals and (vals <= countries() or COUNTRY.search(dims[0].split(".", 1)[1]) and vals & countries()):
            return "map"
    if len(dims) == 1 and len(styles) == 2 and list(styles).count("percent") == 1:
        return "combo"
    return "bar" if len(dims) == 1 else "matrix"


def auto_format(m: dict, tree, values: list) -> dict:
    """RP-3 number format for one measure: COUNT/DISTINCT_COUNT → integer; a name or ID with %, pct or percent →
    percent (the value is a ratio, 1 decimal); else number with 0 decimals if every value is whole, 2 otherwise.
    `compact` (1.2M) once the largest absolute value reaches a million."""
    while isinstance(tree, (mel.Where, mel.Ignore, mel.Time)):
        tree = tree.expr
    nums = [float(x) for x in values if isinstance(x, (int, float, Decimal)) and not isinstance(x, bool)]
    compact = bool(nums) and max(map(abs, nums)) >= 1e6
    if isinstance(tree, mel.Agg) and tree.fn in ("COUNT", "DISTINCT_COUNT"):
        return {"style": "integer", "decimals": 0, "compact": compact}
    if PCT.search(m["id"]) or PCT.search(m["name"]):
        return {"style": "percent", "decimals": 1, "compact": False}
    return {"style": "number", "decimals": 0 if all(x.is_integer() for x in nums) else 2, "compact": compact}


class ReportRef(storage.ProjectRef):
    report: str = Field(pattern=ID, description="report ID (file name in reports/)")


# UI-DS.6 (UI-DS.1 R1/R2): a chat answer's visual is a spec without numbers; `text` visuals have no query
TYPES = Literal["kpi", "bar", "line", "pie", "table", "matrix", "scatter", "slicer", "map", "combo", "waterfall",
                "funnel", "heatmap", "treemap", "sankey"]


class ChatQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # strict: a number is never read as a measure ID
    measures: list[str] = Field(min_length=1, description="measure IDs")
    dimensions: list[Annotated[str, Field(pattern=storage.COLUMN)]] = Field([], description="table.column to group by")
    filters: list[Filter] = Field([], description="row filters (report filter format)")


class ChatVisual(ChatQuery):
    """An `abi-visual` block of a chat answer (R1): measure IDs, dimensions, filters and an optional chart type."""
    type: TYPES | None = None


class _Inline(storage.ProjectRef):
    """A saved report's visual(s), or (`query`) a chat answer's visual (UI-DS.6 R4)."""
    report: str | None = Field(None, pattern=ID, description="report ID (file name in reports/)")
    query: ChatQuery | None = Field(None, description="a chat answer's visual instead of report/page/visual")
    type: TYPES | None = Field(None, description="with `query`: the chart type; omit for automatic")
    interaction: list[Filter] = Field([], description="slicer selections and cross-filters; ANDed after the report, page and visual filters")

    @model_validator(mode="after")
    def _one(self):
        if (self.report is None) == (self.query is None) or self.query and (self.page or self.visual) or self.type and not self.query:
            raise ValueError("Give report (with page and visual), or query (and type) for a chat answer's visual.")
        return self

    def load(self, conn, user: User) -> tuple[dict, list[tuple[dict, dict]]]:
        """(report, [(page, visual)]): a chat visual is one visual of an empty report (no report or page filters).
        A share recipient never gets here with `query`: actions.SHARED reaches only a `report` (R4)."""
        if self.query:
            v = {"id": "chat", "query": self.query.model_dump(mode="json")}
            return {"id": None}, [({}, v | ({"type": self.type} if self.type else {}))]
        r = imports.load(self.project, "report", self.report)
        _shared_filters(conn, user, self.project, r, self.interaction)
        return r, _visuals(r, self.page, self.visual)


class VisualRef(_Inline):
    page: str | None = Field(None, pattern=ID)
    visual: str | None = Field(None, pattern=ID, description="omit for every visual of the page")

    @model_validator(mode="after")
    def _both(self):
        if self.report and not self.page:
            raise ValueError("A report visual needs page and visual (or page alone: every visual of the page).")
        return self


AT_ONCE = 6  # a page's visuals read at once (M3.3's app asked as many)


@action(Role.viewer)
def visual_data(conn, user: User, args: VisualRef) -> dict:
    """Data of one report visual (or `query`: a chat answer's visual) through the semantic engine: its type (automatic unless set), number format per measure, the columns (dimensions, then measure IDs), measure display names and rows, with report, page, visual and interaction filters applied, and the time zone and display currency they are in. With page and no visual: {page, visuals: that for each visual of the page, or {id, error, status}}."""
    r, pvs = args.load(conn, user)
    if args.query or args.visual:
        return _visual(conn, user, args, r, *pvs[0])

    def one(pv: tuple[dict, dict]) -> dict:  # M3.6 round 2: one read action per page, not per visual
        try:
            return _visual(conn, user, args, r, *pv)
        except ActionFailed as e:  # audited as the visual's own call would be (`actions.run`), on its own connection
            detail = f"<sha256:{actions.sha(str(e))}>" if actions.inbound() else str(e)
            with db.connect() as c:
                actions.audit(c, user, actions.VIA.get(), "visual_data", args.project, actions._audited(
                    "visual_data", {**json.loads(actions._call.get()[2]), "visual": pv[1]["id"]}), e.status, detail)
            return {"id": pv[1]["id"], "title": pv[1].get("title"), "error": e.body["message"], "status": e.status}
    # each visual in a copy of this action's context: its memo (role, rules, snapshot), call and caller
    runs = [(contextvars.copy_context(), pv) for pv in pvs]
    with ThreadPoolExecutor(min(AT_ONCE, len(runs))) as ex:
        return {"page": args.page, "visuals": list(ex.map(lambda c: c[0].run(one, c[1]), runs))}


def _visual(conn, user: User, args: VisualRef, r: dict, p: dict, v: dict) -> dict:
    if "query" not in v:
        return {"id": v["id"], "type": "text", "title": v.get("title"), "text": v["text"]}
    q, _, off = _query(args.project, r, p, v, [f.model_dump(mode="json") for f in args.interaction])
    if r.get("model"):  # ABI-PI: a report built in a named model reads only that model
        model.in_scope(args.project, r["model"], q["measures"], q["dimensions"] + [f["column"] for f in q["filters"]])
    out = semantic.query(args.project, q, user.id, timeout=semantic.TIMEOUT)
    ms = {m["id"]: m for m in model._read(args.project, "measures.json").get("measures", [])}
    trees = mel.compile({i: m["expression"] for i, m in ms.items()})  # all: a measure may reference any other
    at = {c: i for i, c in enumerate(out["columns"])}
    fmt = {i: {**auto_format(ms[i], trees[i], [row[at[i]] for row in out["rows"]]), **v.get("format", {}).get(i, {})}
           for i in q["measures"]}
    auto = v.get("type") or auto_type(args.project, q, [r[0] for r in out["rows"]], [fmt[i].get("style") for i in q["measures"]])
    for d in q.get("dimensions", []):  # RP-3: a DATE dimension of month starts shows as months ("Jan 2018")
        vals = {row[at[d]] for row in out["rows"]} - {None}
        if len(vals) > 1 and all(type(x) is date and x.day == 1 for x in vals):
            fmt[d] = {"style": "month"}
    return {"id": v["id"], "type": auto, "title": v.get("title"), "format": fmt, **out, "edit": editable(args.project, q, trees),
            "names": names(args.project, q.get("dimensions", []) + off), "unfiltered": off,
            "warnings": forecast_warnings(conn, user, args.project, q["measures"], trees)}


WEAK_WAPE, WEAK_GAIN = 0.4, 0.1  # ML-SEASONAL.3: off by more than 40% of the actuals, or under 10% better than the baseline


def forecast_warnings(conn, user: User, project: str, measures: list[str], trees: dict) -> list[dict]:
    """ML-3/ML-4/ML-5 (ML-SEASONAL.3): one entry per forecast model whose predictions the measures read that is weak
    (backtest wape > 40%, or error under 10% below the baseline's) or implausible (its horizon total vs the same periods
    last year, ML-SEASONAL.1), from the training run its scored predictions came from. The UI words it (i18n); its
    figures are that run's rows in ml_models. None for a restricted Viewer: the figures come from every row (SE-6, as
    ml_models hides them)."""
    ids = sorted({n.table[3:] for m in measures for n in mel._walk(trees[m])  # ml_<id>.prediction (abi.ml.table_of)
                  if type(n) is mel.Col and n.table.startswith("ml_") and n.column == "prediction"})
    if not ids or semantic.rls.restricted(project, user.id):
        return []
    out = []
    for r in conn.execute("SELECT DISTINCT ON (s.model) t.spec, t.metrics, t.baseline, t.holdout FROM ml_runs s"
                          " JOIN ml_runs t ON t.id = s.trained WHERE s.project = %s AND s.model = ANY(%s) AND s.kind = 'score'"
                          " AND s.status = 'ok' ORDER BY s.model, s.created DESC", [project, ids]):
        if r["spec"]["task"] != "forecast":
            continue
        m, b, h = r["metrics"], r["baseline"], r["holdout"] or {}
        weak = (m.get("wape") or 0) > WEAK_WAPE or m["mae"] > (1 - WEAK_GAIN) * b["mae"]
        odd = h.get("plausible") is False  # runs before ML-SEASONAL.1 have no plausibility check
        if weak or odd:
            out.append({"model": r["spec"]["name"], "weak": weak, "wape": m.get("wape"), "baseline_wape": b.get("wape")}
                       | ({"forecast_total": h["forecast total"], "last_year_total": h["same periods last year total"]} if odd else {}))
    return out


def names(project: str, columns: list[str]) -> dict[str, str]:
    """ML-4 (UI-1.4.3): display names of ML output columns, the only model columns with technical names: in ml_<id>,
    cluster/anomaly/prediction is the ML model's name, score its anomaly score, and a dataset dimension copied as
    <table>_<column> that column's name (as other visuals show it). Other columns: none (the UI shows the column)."""
    ml = {f"ml_{m['id']}": m["name"] for m in imports.objects(project, "ml")}  # abi.ml.table_of (not imported: cycle)
    tables = model._read(project, "model.json").get("tables", [])
    out = {}
    for c in columns:
        t, col = c.split(".", 1)
        if t not in ml:
            continue
        if col in ("cluster", "anomaly", "prediction"):
            out[c] = ml[t]
        elif col == "score":
            out[c] = f"{ml[t]} anomaly score"
        elif src := next((x["table"] for x in tables if col.startswith(x["table"] + "_")), None):
            out[c] = col[len(src) + 1:]
    return out


ONE_ROW = {"SUM", "MIN", "MAX", "AVG"}  # over exactly one row, these are the row's value


def editable(project: str, q: dict, trees: dict) -> dict | None:
    """WB-1 in table and matrix visuals, V-02 C-7: when the visual's dimensions include every key column of a model
    table, each visual row is at most one row of it, so a cell of a non-key dimension of that table, or of a measure
    that is a bare SUM/MIN/MAX/AVG of one of its columns, is that row's value: {table, key: [dimension of each key
    column], columns: {visual column: table column}} (detail rows only when the visual has totals). WB-11: any other
    cell or total row of a bare SUM measure of a keyed table is spread by `edit_total`: `total` lists those measures,
    `filters` the visual's filters (the UI adds the cell's members). None: nothing is editable."""
    tables = model.load(project)["tables"]
    out = {}
    dims = set(q.get("dimensions", []))
    for t, info in tables.items():
        key = [f"{t}.{k}" for k in info.get("key") or []]
        if not key or not set(key) <= dims or t == model.CALENDAR:  # D-21: the generated calendar is read-only
            continue
        cols = {d: d.split(".", 1)[1] for d in q["dimensions"] if d.split(".", 1)[0] == t and d not in key}
        cols |= {m: tr.col.column for m in q["measures"] if isinstance(tr := trees.get(m), mel.Agg)
                 and type(tr.col) is mel.Col  # a converted amount (SE-13) isn't the row's value
                 and tr.fn in ONE_ROW and tr.col.table == t and f"{t}.{tr.col.column}" not in key}
        if cols:
            out = {"table": t, "key": key, "columns": cols}
            break
    total = [m for m in q["measures"] if isinstance(tr := trees.get(m), mel.Agg) and tr.fn == "SUM" and type(tr.col) is mel.Col
             and (tables.get(t := tr.col.table) or {}).get("key") and t != model.CALENDAR and not live.is_live(project, t)]
    return out | {"total": total, "filters": q.get("filters", [])} if total else out or None


def explain(project: str, q: dict, user_id: int | None) -> dict:
    """RP-5 for one semantic query (a visual's, or a Main Agent semantic_query in the chat's trust footer): its measures
    with their MEL expressions (and the measures they reference), the SQL this user runs (SE-6: a restricted Viewer's
    row filters first), what ClickHouse runs (SE-5) and the rates table of converting measures (SE-13); a query the
    planner refuses gets `error` instead of `sql`."""
    ms = {m["id"]: m for m in model._read(project, "measures.json").get("measures", [])}
    used, todo = [], list(q["measures"])
    while todo:  # the query's measures, then every measure their MEL references ([id], PC-1)
        if (i := todo.pop(0)) not in used and i in ms:
            used.append(i)
            todo += re.findall(r"\[([^\]]+)\]", ms[i]["expression"])
    entry = {"measures": [{k: ms[i][k] for k in ("id", "name", "expression", "description") if k in ms[i]} for i in used]}
    if any(re.search(r"\bCONVERT\s*\(", ms[i]["expression"], re.IGNORECASE) for i in used):  # SE-13
        entry["rates_table"] = model.load(project)["rates_table"]
    try:
        _, sql, _, _, reads = semantic.plan(project, q, user_id)  # the SQL this user runs
        entry["sql"] = (semantic.rls.explain(project, user_id) if semantic.rls.restricted(project, user_id) else "") + sql
        if reads:  # SE-5: what ClickHouse runs; the DuckDB SQL reads each result as the named table
            entry["clickhouse"] = {name: ch for name, (_, ch) in reads.items()}
    except ActionFailed as e:  # one broken visual doesn't hide the others
        entry["error"] = str(e)
    return entry


class ExplainRef(_Inline):
    page: str | None = Field(None, pattern=ID, description="only this page; default: every page")
    visual: str | None = Field(None, pattern=ID, description="only this visual")


@action(Role.viewer)
def how_calculated(conn, user: User, args: ExplainRef) -> dict:
    """"How is this calculated?" for every data visual of a report (or one page or visual, or `query`: a chat answer's visual): each measure with its MEL expression (and the measures it references), every filter with its level (report, page, visual, interaction), the SQL the semantic engine runs, the time zone and display currency, and the rates table of converting measures."""
    r, visuals = args.load(conn, user)
    out = []
    for p, v in visuals:
        if "query" not in v:
            continue  # text visuals have no numbers
        q, fs, _ = _query(args.project, r, p, v, [f.model_dump(mode="json") for f in args.interaction])
        out.append({"page": p.get("id"), "visual": v["id"], "filters": fs} | explain(args.project, q, user.id))
    return {"report": r["id"], "visuals": out, **semantic.locale(args.project)}  # SE-13: zone and display currency


BLOCK = re.compile(r"^```abi-visual[ \t]*\n(.*?)\n```[ \t]*$", re.MULTILINE | re.DOTALL)
SLOT = "\ue000"  # where a block stood while the Markdown around it is grounded (no digits, no name)


def visuals(text: str, project: str | None, user_id: int) -> tuple[str, list[str | None]]:
    """UI-DS.6 R2/R3: an answer's `abi-visual` blocks cut out of its Markdown (each left as SLOT, so AG-2 grounds only
    the words around them) and checked instead: ChatVisual's schema (no numeric field), `semantic.plan` as the user
    (measures and columns exist, column rules), every `=`/`in` text filter value held by its column
    (`grounding.filtered`, LLM-2-fix-2). Each block's canonical JSON, or None when it fails (never shown)."""
    from abi.agents import (
        grounding,  # not at top: grounding imports semantic, which imports the agent
    )

    out: list[str | None] = []

    def check(m: re.Match) -> str:
        try:
            v = ChatVisual.model_validate_json(m[1])
            q = v.model_dump(mode="json", exclude={"type"})
            if project is None:
                raise ActionFailed(400, "no project")
            semantic.plan(project, q, user_id)  # measures exist, column rules (the planner leaves columns to DuckDB)
            cols = model.lake_columns(project)
            if any(c.split(".", 1)[1] not in cols.get(c.split(".", 1)[0], {}) for c in q["dimensions"] + [f["column"] for f in q["filters"]]):
                raise ActionFailed(400, "unknown column")
            asked = {x for f in q["filters"] if f["op"] in ("=", "in") for x in
                     (f["value"] if isinstance(f["value"], list) else [f["value"]]) if isinstance(x, str)}
            fake = {"filters": q["filters"], "measures": {}, "columns": [], "rows": []}  # filtered() reads only filters
            if asked - {x for x in grounding.filtered(project, user_id, [fake]) if isinstance(x, str)}:
                raise ActionFailed(400, "unknown member")
            out.append(json.dumps(v.model_dump(mode="json", exclude_defaults=True), ensure_ascii=False))
        except (ValidationError, ActionFailed):
            out.append(None)
        return SLOT
    return BLOCK.sub(check, re.sub(r"\r\n?", "\n", text)), out  # line ends as md.tsx reads them


def shown(text: str, blocks: list[str | None]) -> str:
    """The answer with its valid blocks back in place of their SLOTs and the failed ones left out."""
    it = iter(blocks)
    return re.sub(SLOT, lambda _: f"```abi-visual\n{b}\n```" if (b := next(it, None)) else "", text)


class ReportList(storage.ProjectRef):
    report: str | None = Field(None, pattern=ID, description="only this report (needed when it is shared with you, SH-1)")


@action(Role.viewer)
def list_reports(conn, user: User, args: ReportList) -> dict:
    """Every report spec of the project (pages, visuals with their measure IDs and dimensions, layout, filters, owner, sharing), or only `report`."""
    rs = [imports.load(args.project, "report", args.report)] if args.report else imports.objects(args.project, "reports")
    if role_in(conn, user, args.project) is None:  # SH-1: a recipient doesn't learn who else it is shared with
        rs = [{k: v for k, v in r.items() if k not in ("owner", "sharing")} for r in rs]
    return {"reports": rs}


class SaveReport(storage.ProjectRef):
    report: dict = Field(description=(
        "The whole report (schemas/v1/report.json): {id, name, description?, filters?, hide_details? (true: no per-visual time zone caption and inline 'How is this calculated?' on the canvas), pages: [{id, name, filters?,"
        " visuals: [{id, type? (kpi|bar|line|pie|table|matrix|scatter|slicer|map|text|combo|waterfall|funnel|heatmap|treemap|sankey; omit for automatic), title?,"
        " query? (semantic query: measures (IDs), dimensions (table.column), filters, order_by, limit),"
        " text? (text visual), format? {measure_id: {style, decimals, compact}}, layout? {x, y, w, h} (12-column grid)}]}]}."
        " Filters are {column: table.column, op, value}."))


@action(Role.editor, definition=True)
def save_report(conn, user: User, args: SaveReport) -> dict:
    """Creates or replaces a report (reports/<id>.json). It is checked like an import (schema, measure IDs, model tables) and rejected with file, JSON path and fix if invalid. Returns its ID and, as saved, its pages (ID, name) with their visuals' IDs."""
    rel = f"reports/{imports.object_id(args.report, 'A report')}.json"
    # SH-1: the creator owns it; owner and sharing stay as stored (sharing changes only through set_sharing or a gated import)
    files = bundle(git.path(args.project))
    old = files.get(rel) or {"owner": str(user.id)}
    r = {k: v for k, v in args.report.items() if k not in ("owner", "sharing")} | {
        "$schema": SCHEMA + "report.json", **{k: old[k] for k in ("owner", "sharing") if k in old}}
    new = {rel: r}
    imports.check(conn, args.project, files | new)
    imports.write(args.project, new)
    # ABI-PI 1.2 M2: the names it saved, so an answer may say them (round 2 flagged "one page, Overview" in 4 of 5)
    return {"id": r["id"], "pages": [{"id": p["id"], "name": p["name"], "visuals": [v["id"] for v in p["visuals"]]}
                                     for p in r["pages"]]}


@action(Role.editor, definition=True, confirms=True)
def delete_report(conn, user: User, args: ReportRef) -> dict:
    """Deletes a report."""
    imports.remove(args.project, "report", args.report)
    return {}
