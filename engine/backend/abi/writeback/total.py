"""Aggregate-level planning: edit a total (PRD WB-11; work/validation/E-04.md decision 5).

A new total of a bare-SUM measure is spread over the rows the semantic engine returns for the cell's filters
(principle 4, so RLS and the effective table apply): proportionally, or evenly when the total is 0. Integer and
DECIMAL columns are rounded at their scale with largest remainder, so the rows sum exactly to the new total. All
arithmetic is exact (`Fraction`, principle 7). The rows are written as one `edit_cells` batch (WB-1, WB-8 one undo);
an agent's call is a WB-9 proposal.
"""

import math
import re
from decimal import Decimal
from fractions import Fraction
from typing import Annotated

from pydantic import Field

from abi import semantic, storage
from abi.actions import ActionFailed, action
from abi.project import ID
from abi.security import Role, User
from abi.semantic import mel, model
from abi.writeback import CellEdit, Edits, _table, edit_cells

MAX_ROWS = 10_000  # D-17
INT = re.compile(r"^U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)$")
DEC = re.compile(r"^DECIMAL\(\d+,(\d+)\)$")


def scale(dt: str) -> int | None:
    """Decimal places a column keeps: 0 for integers, s for DECIMAL(p,s), None for floats (nothing to round to)."""
    if INT.match(dt):
        return 0
    if m := DEC.match(dt):
        return int(m[1])
    if dt in ("FLOAT", "DOUBLE"):
        return None
    raise ActionFailed(422, f"Can't spread a total over a {dt} column.")


def spread(values: list, value: Fraction, places: int | None) -> list:
    """New row values summing to `value`: proportional to `values` (None = 0), evenly when they sum to 0. With
    `places`, largest remainder at that scale makes the sum exact (Decimal results); without, exact shares as float."""
    w = [Fraction(v or 0) for v in values]
    total = sum(w)
    shares = [value * x / total for x in w] if total else [value / len(w)] * len(w)
    if places is None:
        return [float(s) for s in shares]
    units = value * 10 ** places
    if units.denominator != 1:
        raise ActionFailed(422, f"The new total has more than {places} decimal place(s), which the column keeps.")
    exact = [s * 10 ** places for s in shares]
    out = [math.floor(e) for e in exact]
    # the leftover units go one each to the largest fractional parts (ties: row order, sorted is stable)
    for i in sorted(range(len(out)), key=lambda i: out[i] - exact[i])[:int(units) - sum(out)]:
        out[i] += 1
    return [Decimal(u).scaleb(-places) for u in out]


def _plain(v):
    """A value as the edit API's Value (str, int, float, bool)."""
    return v if v is None or isinstance(v, (str, int, float, bool)) else str(v)


class Total(storage.ProjectRef):
    measure: str = Field(min_length=1, description="ID of a SUM measure (model/measures.json)")
    filters: list[semantic.Filter] = Field([], description="the edited cell's filters (its row and column members)")
    value: Decimal = Field(description="the new total")
    old: Decimal | None = Field(None, description="the total the user saw; if it changed since, the edit is refused"
                                                  " with 'value changed, reload?' (WB-7)")
    comment: str | None = Field(None, max_length=2000)
    links: list[Annotated[str, Field(pattern=ID)]] = Field([], description="the visual's links (its semantic query's)")


@action(Role.editor)  # SC-2: Editor = write-back
def edit_total(conn, user: User, args: Total) -> dict:
    """Edits a total of a SUM measure: spreads the new total over the underlying rows (those matching the filters), proportionally to their current values, or evenly when the total is 0, rounded so the rows add up exactly. The changes are one edit batch (one undo). When the Main Agent calls it, the batch is stored as a proposal the user approves."""
    expr = {m["id"]: m["expression"] for m in model._read(args.project, "measures.json").get("measures", [])}
    if args.measure not in expr:
        raise ActionFailed(404, f"No measure '{args.measure}' in project '{args.project}'.")
    try:
        tree = mel.compile(expr)[args.measure]
    except mel.MelError as e:
        raise ActionFailed(400, str(e))
    if not (isinstance(tree, mel.Agg) and tree.fn == "SUM" and type(tree.col) is mel.Col):  # not a converted SUM (SE-13)
        raise ActionFailed(422, f"'{args.measure}' isn't a plain SUM of one column; totals can be edited for SUM measures only.")
    t, col = tree.col.table, tree.col.column
    _, types, key = _table(args.project, t)  # WB-5: keyed tables only
    places = scale(types[col])
    rows = semantic.query(args.project, {
        "measures": [args.measure], "dimensions": [f"{t}.{k}" for k in key],
        "filters": [f.model_dump() for f in args.filters], "links": args.links, "limit": MAX_ROWS + 1}, user.id)["rows"]
    if not rows:
        raise ActionFailed(404, "No rows match this cell's filters, so there is nothing to spread the total over.")
    if len(rows) > MAX_ROWS:
        raise ActionFailed(422, f"This total covers more than {MAX_ROWS:,} rows; filter it further to edit it.")
    current = sum(Fraction(r[-1] or 0) for r in rows)
    # exact for integer/DECIMAL; a float sum differs in its last bits between DuckDB and here, so compare as `norm` does
    if args.old is not None and (Fraction(args.old) != current if places is not None
                                 else semantic.norm(args.old) != semantic.norm(float(current))):
        raise ActionFailed(409, f"The total changed to {semantic.norm(float(current))} since you read it. Reload?")
    new = spread([r[-1] for r in rows], Fraction(args.value), places)
    edits = [CellEdit(key=[_plain(k) for k in r[:-1]], column=col, value=_plain(n), old=_plain(r[-1]))
             for r, n in zip(rows, new) if r[-1] is None or Fraction(n) != Fraction(r[-1])]
    if not edits:
        raise ActionFailed(400, f"The total is already {args.value}; nothing to change.")
    out = edit_cells(conn, user, Edits(project=args.project, table=t, edits=edits, comment=args.comment))
    # D-22: the rows the server found (keys) are quotable too; D-114: the spread stays out (one row's is the caller's total)
    return out | {"columns": [*out["columns"], "row"],
                  "rows": [[*r, ", ".join(map(str, c["row"].values()))] for r, c in zip(out["rows"], out["changes"])]}
