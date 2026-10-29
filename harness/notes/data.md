# data: load, connect, transform, refresh
- Files the person points to: `abi_load` (upload + model + starter report). One table per file, ID from the file name.
- `add_data` needs a location ABI can read (server path under its files root, s3://, http(s)://, postgres|mysql|clickhouse://).
  Credentials are `${secret:NAME}` references the person created in ABI; never ask for the value.
- CSV: pass `csv: {"quote": "\"", "escape": "\"", "strict_mode": false}` when fields hold quotes or line breaks.
- Clean with `set_steps` (first step `{"type":"source","source":<id>}`); try steps with `preview_steps` first (1,000 rows).
  Types: add_column, rename, set_type, filter, deduplicate, merge, append, group, pivot/unpivot, split_column, sort, sql.
- Text dates: `CAST(strptime(col, '%y.%d.%m') AS DATE)` (check the format in preview first).
- Profile before modelling: detect_model (types, keys, ranges), data_quality (freshness, gaps, volume, NULLs).
- `refresh` reloads a table; `list_snapshots`/`restore_snapshot` go back to an earlier load (asks confirmation).
- Deleting a table or source is destructive: the person confirms.
