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
- `objective`: the sum over the objects of `per_unit` times the decided number, to `maximize` or `minimize`.
- `limits`: each bounds the sum of `per_unit` (default 1) times the decided number with `max`, `min` or `equal`; with
  `group` it holds within each value of a column.
- A value (`per_unit`, `decide.min`, `decide.max`) is a number, `{"property": <numeric column of the type's table>}` or
  `{"measure": <measure ID>}`, read per object. There is no expression language: a derived figure is a measure.
- The names are the business's words. The answer uses them and nothing else.

The schema's `examples` are three templates (allocation, staffing, maintenance slots). They are suggestions: nothing
in the engine knows an industry.

## A run

`run_optimizer` (the app: Automate > Plans, "Find the best plan") queues the run for the worker; `optimizers` reads it.
The answer is a sentence and rows, in this order: the total (and what it is now), the number of values the proposal
changes, the limits the sentence names, each object with its number now and in the plan (the largest first), then
the other limits. A limit's row holds what the plan uses of it (an at-most limit says when it is fully used, an
at-least limit when it is just met). The sentence names up to three limits that are fully used, three that are just
met and three that rounding took the plan past, then "and other limits"; every figure and every limit it names is in
the first rows (at most 11 before the objects): an agent is shown the first 20. When no plan fits, the answer
names the limits that can't hold together, and only those (a limit with a least and a most: the bound that is in the
way); an object whose own least is above its own most is named before anything is worked out. A plan with no end, over
nothing, or over more objects than one run takes says so and what to do.

**A run changes nothing.** An Editor's or Owner's plan is stored as one write-back proposal on `decide.write`, listed
under Data > Edits; a person approves it there (`approve_edit` asks for their confirmation whoever calls it: an agent,
a flow and a replayed call are all asked), and the approved batch is undone in one step. A Viewer's run shows the plan
and proposes nothing.

**One optimiser, at most one open proposal.** For an Editor or Owner, every run that gets as far as working a plan out
ends with at most one open proposal of that optimiser:
- it finds a plan that changes something: the new proposal is stored first, then the proposals earlier runs left open
  are withdrawn (rejected; they stay in the edit history), and the answer says so;
- it finds a plan the objects already have: nothing is proposed, and an earlier open proposal is withdrawn;
- it finds that there is no plan (the limits conflict, or the plan has no end): an earlier open proposal is
  withdrawn, and the answer says so;
- the time runs out without a plan: nothing was learned, so nothing is withdrawn;
- there is nothing to plan (no object the person sees, or none that matches the filters): nothing is withdrawn, and
  the answer says what to do;
- it is refused before anything is worked out (an object without a number, too many objects, a file that no longer
  fits the model): nothing is withdrawn.
A run that fails after its proposal was stored takes that proposal back.
A proposal a person already approved or rejected is never touched.

An optimiser a flow runs can't be deleted: the refusal names the flow, before anything is asked; take the step out
of the flow first.

The agent can set an optimiser up (`save_optimizer`) and run it; a flow runs it with a step
`{"type": "action", "action": "run_optimizer", "input": {"optimizer": "hours"}}`, on a schedule too: a run that finds
a plan leaves a proposal, never a change, and a run that finds none fails the step with the reason. A flow started by
an action type, and an alert for one object, may not run one.

**Row-level security.** The objects and measures are read as the person who runs it. A restricted Viewer's plan, its
total, the limits it uses and the reason no plan fits are made of the objects they see; a plan that needs a column
hidden from them is not worked out for them, and the column is not named. They read the runs they made while
restricted, and of the file only its business names (no column, filter or figure); everyone else reads runs made over
every row. So two people can get different plans from one optimiser: a limit such as "100 hours in total" is applied
to what each one sees.

**Published copies have no plans.** Publish leaves `optimizers/` out, as it leaves flows out: a plan is worked out and
approved in the working project, and its runs are kept there. The published version has no Plans page; what an
approved plan wrote reaches it with the data, as every edit does.

## Limits, stated plainly

- Solver: HiGHS (MIT, the `highspy` wheel), one thread, linear and whole-number problems only. One function calls it
  (`abi/optimize.py` `solve`). Nothing quadratic, no "either/or" conditions other than through 0/1 numbers.
- One number per object. "Hours per café per shift" needs an object type whose objects are café-shifts.
- At most 1,000 objects a run; more is refused with what to do.
- Time: 60 seconds by default, `time_limit_s` 1 to 600. When the time runs out with a plan that holds every limit,
  that plan is the answer, labelled "found in the time it is allowed" (a better one may exist), and it is proposed
  like any other; with no plan found, nothing is proposed. The proposal is written after the limit (a cell at a time).
- Numbers in the file are finite and at most 1e15 in size (a filter's value: any).
- The limit's right-hand side is a constant; per-object bounds can be a property or a measure.
- An object without a number for a property or a measure the optimiser uses stops the run and is named (up to
  three, "and others"): nothing counts as 0 silently. For a measure: leave such objects out with a filter, or use a
  measure that gives 0.
- A measure is read per object only for types with one key column.
- The plan's numbers are rounded the way `decide.write` holds them (a DECIMAL(10,2) column: two decimals; otherwise
  six) after the best plan is found. Which limits are fully used or just met is judged on the plan before rounding;
  the rows show what the rounded plan uses, and when rounding takes it past a limit (by less than half a unit of the
  last decimal, times the limit's `per_unit`, per object) the sentence says so ("After rounding, the plan is slightly
  over: …") with both figures, unless they read the same: the plan is not re-solved in the column's units. A
  whole-number column, a DECIMAL without decimals included, needs `integer: true`.
- Among plans that are equally good, which one comes out is not specified.
- "Which limits conflict" is one smallest set; other sets may exist. For a limit with `group` it does not say which
  group.
- Runs are kept in the engine's database, not in Git: the latest 5 finished runs per optimiser and person, and any
  run whose proposal may still be open. At most one run per person and optimiser at a time, 3 per person and 10 in a
  project queued or running (a flow's step that meets these is tried again later). A run whose worker died ends
  failed (within about a minute; at the latest when it has been running for 30 minutes, or waiting for 6 hours, and
  the project's next run starts).
- The app shows and runs plans; setting one up is done with the agent or by editing the file. The generated
  project documentation and the lineage map do not list optimisers yet.
