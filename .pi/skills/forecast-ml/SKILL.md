---
name: forecast-ml
description: Forecasts and ML in ABI - forecast a measure, classify, cluster, find anomalies; train, score, report accuracy vs baseline.
---
# Forecast and ML
1. `abi_tools ["ml", "model"]`. A forecast needs calendar.day and history; "per channel" adds that dimension.
2. Dataset first: `save_dataset {id, name, query: {measures: [...], dimensions: ["calendar.day", <per-dimension>]}}`.
3. Forecast: `save_ml_model {ml: {id, name, task: "forecast", dataset, target: <measure id>, time: "calendar.day",
   horizon: 28, method: "auto"}}`. Anomaly: `{task: "anomaly", dataset, time: "calendar.day", features: [<measure ids>]}`
   (no target). Clustering: `{task: "clustering", dataset, features}`.
4. `train_model {model}`, then `ml_models` (it trains in the worker: ask at most twice; still running: say so).
5. Accuracy and the baseline comparison only from ml_models' rows (`latest_run.metrics`, `baseline`,
   `better_than_baseline`); say plainly when it doesn't beat the baseline.
6. `score_model {model}`; predictions land in a table `ml_<id>` with measures; show them (a line of actual and
   prediction) if asked.
