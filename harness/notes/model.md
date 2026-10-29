# model: tables, keys, relationships, glossary, datasets
- `build_model` after loading: fact/dimension, keys, many-to-one relationships, and a generated `calendar` when a
  fact has a DATE column. `readiness` says what is missing for Ready (a fact with a valid many-to-one to a dimension,
  a description on every table).
- `set_table` overrides kind, key, description, hidden (staging tables: hidden). `add_relationship from: fact.col,
  to: dim.key`. `set_date_table` when the date table isn't detected.
- Measures: see measures notes (`set_measure`). Glossary: `set_glossary_term` ties words to measures/columns.
- Datasets (`save_dataset`): a named semantic query (measures + dimensions); ML models train on datasets.
- Row-level security lives in the security notes (`set_rls_rule`, `set_column_rule`).

# measures (MEL)
- `set_measure {id, name, expression, description}`; IDs lowercase snake_case. Checked against loaded columns.
- Functions: SUM, COUNT, DISTINCT_COUNT, AVG, MIN, MAX, DIVIDE(a, b), WHERE(expr, condition), PREVIOUS_PERIOD([m]),
  YTD([m]), ROLLING([m], n, day). Reference other measures as [id].
- Running totals (views, likes per day snapshot) must not be summed over days: take the latest row
  (`WHERE(SUM(t.views), t.latest = TRUE)` with a `latest` step column) or define the measure on gains.
- Ratios: DIVIDE of measures, never AVG of a ratio column.
