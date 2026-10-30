---
name: load-data
description: Load files, S3 folders of Parquet, or a database into a new or existing ABI project. Use for "load this", "here are some files/parquet/csv", "data is in a bucket", "connect to postgres", a folder of data.
---
# Load data
1. IDs: workspace (default `main` or the person's preference) and project (lowercase, digits, `_`, starts with a
   letter). A new project for a bucket or database: `abi_project {workspace, project, name}` first.
2. Files on this machine: `abi_load {workspace, project, paths}` uploads each file as a table, types a date, builds the
   model, starter measures and an Overview report: a **quick look** only (a throwaway project, "just show me"). For a
   project people will use, the uploaded tables are the bronze sources (one per file, as landed): hide them and build
   the layers on top (step 6).
3. **A folder of Parquet on S3 (RustFS, MinIO, AWS):** one `add_data` per folder; the folder is one table.
   ```json
   {"project": "p", "table": "bronze_basic", "location": "s3://bucket/batch/report_a/*.parquet", "format": "parquet",
    "s3": {"endpoint": "127.0.0.1:9100", "url_style": "path", "use_ssl": false, "region": "us-east-1"}}
   ```
   - `endpoint` is host:port only, never `http://`; no `?` in the location; local S3 servers need `url_style: path`
     and `use_ssl: false`. A public or anonymous bucket needs no keys; otherwise `key_id`/`secret` are
     `${secret:NAME}` references the person created in ABI (never ask for a key).
   - The glob must match at least one file, or the load fails: a stream folder that is still empty can't be a source
     yet (load it once the first file has landed).
   - One file per day per report is normal: the glob reads them all; ABI reloads the table when files change (new
     days appear within a minute).
   - Batch history and the streamed days are two sources (`bronze_x`, `bronze_x_stream`); silver appends them
     (medallion skill).
   - Large folders (tens of millions of rows) take one to two minutes each. Wait for the result; never retry a load
     that is still running, and never load the same folder twice.
4. A database: `add_data` with `postgres://host/db` (or mysql, clickhouse) and `database: {table, user, password}`
   as secret references.
5. After loading: `data_quality` (rows, freshness, gaps). Row counts come from its rows only.
6. **Always continue with the medallion skill**: bronze sources (hidden), silver staged tables (cleaned, typed,
   combined; hidden: they are the Staged page), gold facts and dimensions (the model). profile-clean decides the
   silver steps; then build-model. Cleaning never happens outside ABI: it is silver's steps, so its lineage stays.
- The first load from a new host (e.g. the S3 endpoint) asks the person once (ABI's address policy): do that
  load yourself, not in a subagent; later loads from the same host don't ask.
