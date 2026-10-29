---
name: forecast-ml
description: Forecasts and ML in ABI - forecast a measure, classify, cluster, find anomalies; train, score, report accuracy vs baseline.
---
# Forecast and ML
1. `abi_tools ["ml", "model"]`. A forecast needs a date column (calendar.day) and history; "per category" adds the
   category dimension to the dataset.
2. `save_dataset {id, name, query: {measures: [target], dimensions: ["calendar.day", <per-dimension>]}}`.
3. `save_ml_model {ml: {id, name, task: "forecast", dataset, target, time: "calendar.day", horizon}}` (4 weeks = 28 days).
4. `train_model`, then `ml_models` (at most twice; still running: tell the person it is training, ask again later).
5. Report accuracy and the baseline comparison only from ml_models' rows; say plainly when it doesn't beat the baseline.
6. `score_model`, read `<id>_prediction` with semantic_query; add a line visual (actual + prediction) if asked.
