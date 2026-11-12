# Optimisers: "what is the best plan?" (1.2.6)

An optimiser answers one question: which number should each object get so that a total is as high (or as low) as it
can be, while every limit holds. Hours per café, units per warehouse, which maintenance slots to use. It is data, not
code: a file `optimizers/<id>.json` in the project (schema `engine/schemas/v1/optimizer.json`), kept in Git and in the
project bundle like every other definition. A project without one behaves exactly as before.

## The file

```json
{"id": "hours", "name": "Hours per café",
 "over": {"type": "cafe"},
 "decide": {"name": "Hours", "write": "planned_hours", "max": {"property": "max_hours"}, "integer": true},
 "objective": {"goal": "maximize", "name": "Revenue", "per_unit": {"property": "revenue_per_hour"}},
 "limits": [{"name": "Total hours", "max": 100},
            {"name": "Hours per region", "group": "region", "max": 60}]}
```

- `over`: the object type (see [objects.md](objects.md)); `filters` narrow it, as in `list_objects`.
- `decide`: one number per object. `write` is the column of the type's table the plan is proposed for; its current
  values are the plan's "now". `min` (default 0), `max`, and `integer` for whole numbers.
- `objective`: the sum of `per_unit` × the decided number, to `maximize` or `minimize`.
- `limits`: each bounds the sum of `per_unit` (default 1) × the decided number with `max`, `min` or `equal`; with
  `group` it holds within each value of a column.
- A value (`per_unit`, `decide.min`, `decide.max`) is a number, `{"property": <numeric column of the type's table>}` or
  `{"measure": <measure ID>}`, read per object. There is no expression language: a derived figure is a measure.
- The names are the business's words. The answer uses them and nothing else.

The schema's `examples` are three templates (allocation, staffing, maintenance slots). They are suggestions: nothing
in the engine knows an industry.

## A run

`run_optimizer` (the app: Automate > Plans, "Find the best plan") queues the run for the worker; `optimizers` reads it.
The answer is a sentence and rows: the total (and what it is now), each limit with what the plan uses of it and
whether it is fully used, and each object with its number now and in the plan. When no plan fits, the answer names
the limits that can't hold together, and only those. A plan with no upper end, or over nothing, says so.

**A run changes nothing.** An Editor's or Owner's best plan is stored as one write-back proposal on `decide.write`,
listed under Data > Edits; a person approves it there (`approve_edit` asks for their confirmation whoever calls it:
an agent, a flow and a replayed call are all asked), and the approved batch is undone in one step. A Viewer's run
shows the plan and proposes nothing. The agent can set an optimiser up (`save_optimizer`) and run it; a flow runs it
with a step `{"type": "action", "action": "run_optimizer", "input": {"optimizer": "hours"}}`, on a schedule too. Each
of those runs adds a proposal, never a change. A flow started by an action type, and an alert for one object, may
not run one.

**Row-level security.** The objects and measures are read as the person who runs it. A restricted Viewer's plan, its
total, the limits it uses and the reason no plan fits are made of the objects they see; a plan that needs a column
hidden from them is not worked out for them. They read their own runs only; everyone else reads runs made over every
row. So two people can get different plans from one optimiser: a limit such as "100 hours in total" is applied to what
each one sees.

## Limits, stated plainly

- Solver: HiGHS (MIT, the `highspy` wheel), linear and whole-number problems only. One function calls it
  (`abi/optimize.py` `solve`). Nothing quadratic, no "either/or" conditions other than through 0/1 numbers.
- One number per object. "Hours per café per shift" needs an object type whose objects are café-shifts.
- At most 1,000 objects a run; more is refused with what to do.
- Time: 60 seconds by default, `time_limit_s` 1 to 600. A run that reaches it proposes nothing, also when it had
  found a plan that might not be the best. The proposal is written after the limit (a cell at a time).
- The limit's right-hand side is a constant; per-object bounds can be a property or a measure.
- A property without a number on an object stops the run and names the object; a measure without rows counts as 0.
- A measure is read per object only for types with one key column.
- "Which limits conflict" is one smallest set; other sets may exist. For a limit with `group` it does not say which
  group.
- Every run adds its own proposal: an earlier one that is still open stays open until a person rejects it.
- Runs are kept in the engine's database, not in Git. In the app, Viewer mode reads the published copy, which has no
  runs: plans are worked out in Developer.
- The app shows and runs plans; setting one up is done with the agent or by editing the file. The generated
  project documentation and the lineage map do not list optimisers yet.
