---
name: profile-clean
description: Profile loaded tables and clean them with transform steps (types, dates, duplicates, renames, joins). Use after loading or when data looks wrong.
---
# Profile and clean
1. `detect_model` (types, keys, date ranges, fact/dimension) and `data_quality` (freshness, gaps, NULL shares).
2. Draft steps and try them with `preview_steps` (first 1,000 rows, as text) before `set_steps`.
3. Common steps: text date -> `add_column day = CAST(strptime(trending_date, '%y.%d.%m') AS DATE)` (find the format in
   preview: '17.14.11' is year.day.month); exact duplicates -> `deduplicate columns: []`; two captures a day -> a
   `row_number()` step and `filter capture = 1`; lookups -> `merge`; several files of one shape -> `append` + a tag column.
4. Running totals (views, likes per snapshot day): add `latest = row_number() OVER (PARTITION BY id ORDER BY day DESC) = 1`
   so measures can take each item once; gains: `views - lag(views) OVER (PARTITION BY id ORDER BY day)`.
5. `set_steps` reloads the table; rerun data_quality. Say what changed in rows (from the tool results only).
