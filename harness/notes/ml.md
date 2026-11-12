# ml: forecast, classify, predict, cluster, anomalies
- Train on a dataset: `save_dataset {id, name, query: {measures: [target], dimensions: ["calendar.day", ...]}}`, then
  `save_ml_model {ml: {id, name, task: forecast|classification|regression|clustering|anomaly, dataset, target, time,
  horizon}}`, then `train_model`, read `ml_models` (at most twice; a queued/running run: tell the person it trains).
- Forecasts need a date column (calendar.day) and enough history; per category: add the category dimension.
- State accuracy, baseline comparison and drift only from ml_models rows; when better_than_baseline is false, say plainly
  the model doesn't beat the baseline.
- `score_model` writes measure <id>_prediction; read it with semantic_query. `prefill_input_table` writes a forecast into
  an input table as a proposal.
- A prediction about a business object (a customer's churn risk, a café's expected revenue): read `ontology`, pick the
  object type, and add `object: <type id>` to the model. Its dataset is grouped by the type's key (`table.column`) plus
  columns of its table as features, with no date columns; a feature from another type is a measure reached through a
  link: name the link in the dataset query's `links`. Task: yes/no → classification (the property is the probability of
  yes; a target with two other outcomes, such as Churned/Retained: ask which one the probability is for and set
  `positive` to it; save_ml_model's `note` names the two),
  a number → regression, groups → clustering, unusual ones → anomaly, a figure per object over time → forecast (keep
  the date columns: one series per object). Say the type, target, features, task and method you chose, then train once
  the person agrees. After `score_model` the prediction is a property on each object's page under the model's name,
  seen only by people who see the object; a monitor with `per` can alert on it (measure <id>_probability, <id>_prediction).
  A dataset column from another table is refused: make it a measure. A flow with trigger after_refresh on `ml_<id>` runs
  after each scoring. Removing a property from the type keeps it out; taking `object` off the model removes it.
- "What is the best plan?" (hours per café, units per warehouse, which slots to use) is an optimiser, not a model:
  read `ontology` for the object type and `semantic_model` for measures, then `save_optimizer {optimizer: {id, name,
  over: {type, filters?}, decide: {name, write, min?, max?, integer?}, objective: {goal: maximize|minimize, name,
  per_unit}, limits?: [{name, per_unit?, group?, max? | min? | equal?}], time_limit_s?}}`. One number is decided per
  object; `write` is the column of the type's table the plan is proposed for (whole-number column: `integer: true`).
  `per_unit`, `decide.min` and `decide.max` are a number, `{property: <numeric column>}` or `{measure: <id>}` (read per
  object; one key column). A limit bounds the sum of per_unit × the decided number (per_unit default 1); `group`
  makes it hold within each value of a column. Names (`decide.name`, `objective.name`, each limit's `name`) are the
  person's business words: the plan's text uses them. Every object needs a number for each property and measure
  used: one without stops the run and is named (fill it in, or filter such objects out). Shapes to start from, never
  a rule: allocation (units per warehouse, max = capacity, one total limit), staffing (hours per café between the
  hours it needs and the hours it can use, the most revenue within a wage budget and a limit per region), maintenance
  slots (0/1 per slot with integer and max 1, `equal: 1` per machine, a limit per week). Say the
  goal, what is decided and each limit in words and save once the person agrees.
- `run_optimizer {optimizer}` queues the run (default 60 s, at most 1,000 objects); read it with `optimizers
  {optimizer}` (at most twice; still queued or running: say it is being worked out). State the plan, the total and
  the limits only from its rows (`what`: Total; Limit and Limit fully used: at most `limit`, the plan uses `plan`; At
  least and At least, just met; Exactly; Limit in conflict; else the object's type with `plan` and `now`); relay
  `text` for what happened and add no count of your own. `outcome` found: the time ran out with a plan that may not be
  the best; say so. A plan is never applied: an Editor's run stores it as a proposal (`proposal`), which the person
  approves under Data > Edits (`approve_edit` asks them), and withdraws the proposal an earlier run of the same
  optimiser left open; a Viewer's run only shows the plan for what they see. "No plan fits": name the limits in
  conflict and ask which one to relax; never loosen a limit yourself. On a schedule: a flow step `{type: action,
  action: run_optimizer, input: {optimizer}}`; a run that finds a plan leaves one proposal, never a change, and a run
  that finds none fails the step with the reason. A published copy has no plans.
