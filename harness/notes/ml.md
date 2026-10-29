# ml: forecast, classify, predict, cluster, anomalies
- Train on a dataset: `save_dataset {id, name, query: {measures: [target], dimensions: ["calendar.day", ...]}}`, then
  `save_ml_model {ml: {id, name, task: forecast|classification|regression|clustering|anomaly, dataset, target, time,
  horizon}}`, then `train_model`, read `ml_models` (at most twice; a queued/running run: tell the person it trains).
- Forecasts need a date column (calendar.day) and enough history; per category: add the category dimension.
- State accuracy, baseline comparison and drift only from ml_models rows; when better_than_baseline is false, say plainly
  the model doesn't beat the baseline.
- `score_model` writes measure <id>_prediction; read it with semantic_query. `prefill_input_table` writes a forecast into
  an input table as a proposal.
