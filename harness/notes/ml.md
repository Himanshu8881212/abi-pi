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
