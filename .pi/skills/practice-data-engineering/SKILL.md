---
name: practice-data-engineering
description: How a senior data engineer works in ABI - grain, idempotent loads, layer naming, reconciliation, lineage, cost. Use with load-data, medallion, profile-clean and build-model, whenever data is loaded, transformed or modelled.
---
# Data engineering practice
- **Grain first.** Before a table, write its grain in one line ("one row per day, video, country"); deduplicate and
  group on exactly those columns; say it in the table's description.
- **Layers by name.** `bronze_*` as landed (never edited), `ref_*` reference lists, `silver_*` cleaned and combined
  (hidden), `gold_*` facts and `dim_*` dimensions in the model. Measures only on gold.
- **Idempotent.** Every step can rerun: loads replace a snapshot, steps rebuild a table. Check what exists
  (`semantic_model`, `data_quality`) before creating; never load the same folder twice; never retry a running load.
- **Batch plus stream.** Two bronze sources, one silver union, deduplicated on the grain, so a late or repeated file
  can't double count.
- **Reconcile.** After each layer compare counts and one total with the layer below (`semantic_query` or
  `preview_steps`): silver rows = bronze batch + stream minus duplicates; gold sums = silver sums. Say any gap.
- **Types and keys.** Dates as DATE, codes keep the source's type, keys unique (deduplicate on the key in a dimension).
  Set a dimension's key before joining to it.
- **Lineage and docs.** Every model table has a description; `lineage` answers "what breaks if I change X" before you
  change it.
- **Cost.** Aggregate to the grain the business reads (daily, not per event) in gold; keep wide raw columns in
  bronze only.
- **Change control.** In the app, Developer work stays uncommitted until the person commits; say what to commit.
