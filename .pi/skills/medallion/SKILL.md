---
name: medallion
description: Build bronze, silver and gold layers in ABI (raw sources, cleaned and combined tables, business-grain facts and dimensions) with the semantic model on gold only. Use for "medallion", "bronze/silver/gold", "layers", "staging", "future-proof the model", batch plus stream.
---
# Medallion in ABI
Layers are table names and the `hidden` flag; the work is `add_data` (bronze) and `set_steps` (silver, gold).

## The rule that shapes everything
A table's steps **start with exactly one `source` step**, in one of two forms:
- `{"type": "source", "source": "<a loaded source>"}` reads the source's files again (into the table's own raw copy).
- `{"type": "source", "table": "<a table the project builds>"}` starts from that table's built rows: no source is read
  again and no raw copy is made. **Silver starts from its bronze table and gold from its silver table.** ABI builds
  them in that order, and a refresh of bronze rebuilds silver, then gold, once each. Tables that would read each other
  in a circle are refused.
Other tables come in with `append` (same columns, stacked) or `merge` (a join).

## Recipe
1. **Bronze**: one `add_data` per folder, exactly as landed, named `bronze_<report>`; stream folders as
   `bronze_<report>_stream`; reference lists as `ref_<name>` (load-data skill).
2. **Silver** (cleaned, combined; hidden): for a fact with a stream:
   `[{"type":"source","table":"bronze_x"}, {"type":"append","tables":["bronze_x_stream"]},
     {"type":"deduplicate","columns":[<the grain columns, e.g. date, video_id, country_code>]}]`
   Typing and derived columns: `{"type":"set_type","columns":{"col":"DATE"}}`,
   `{"type":"add_column","name":"format","expression":"CASE WHEN video_length <= 60 THEN 'Shorts' ELSE 'Long-form' END"}`
   (DuckDB SQL expression over the row), `{"type":"rename","columns":{"old":"new"}}`, `{"type":"filter","condition":"…"}`.
3. **Gold** (business grain; in the model):
   - facts: `[{"type":"source","table":"silver_x"}, {"type":"group","by":[...grain],"aggregations":[{"name":"views","function":"sum","column":"views"}, …]}]`
     (functions: sum, avg, min, max, count, median, count_distinct; `count` takes `"column": null`).
   - dimensions: `[{"type":"source","table":"silver_x"}, {"type":"select","columns":[key, attributes…]}]` or a `group` by the key for a
     dimension derived from a fact (e.g. channels from videos), plus `deduplicate` on the key.
   - Small facts with no stream can start from bronze directly.
4. Each `set_steps` loads the table; a 25M-row dedupe or group takes two to four minutes. Wait; don't retry.
   `preview_steps {steps}` first when unsure of a column name (it shows types and 1,000 rows).
5. Then build-model: `build_model`, hide bronze/silver/ref (`set_table hidden: true`), set kinds and keys on gold,
   add relationships, describe every table, readiness until Ready.
6. Measures only on gold columns.
- The first load from a new host (e.g. the S3 endpoint) asks the person once (ABI's address policy): do that
  load yourself, not in a subagent; later loads from the same host don't ask.
