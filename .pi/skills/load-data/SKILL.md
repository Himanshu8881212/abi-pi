---
name: load-data
description: Load data into a new or existing ABI project: CSV, TSV, Excel, JSON/NDJSON, Parquet, Avro or Arrow files, S3 folders (also Delta and Iceberg tables), https files, Postgres or MySQL tables. Use for "load this", "here are some files/csv/excel/parquet", "data is in a bucket", "connect to postgres/mysql", a folder of data.
---
# Load data
ABI reads CSV, TSV, Excel (.xlsx), JSON/NDJSON, Parquet, Avro and Arrow files, S3 folders of them, Delta and Iceberg
tables on S3, https files, and Postgres and MySQL tables.
1. IDs: workspace (default `main` or the person's preference) and project (lowercase, digits, `_`, starts with a
   letter). A new project for a bucket or database: `abi_project {workspace, project, name}` first.
2. Files on this machine: `abi_load {workspace, project, paths}` uploads each data file as a table (CSVs with explicit
   quoting, so quoted fields with line breaks stay whole; Excel: the first sheet), types a date, builds the model,
   starter measures and an Overview report: a **quick look** only (a throwaway project, "just show me"). For a project
   people will use, the uploaded tables are the bronze sources (one per file, as landed): hide them and build the
   layers on top (step 6). Another Excel sheet: the person picks it in **Data › Sources › Add source** (Excel sheet).
3. **A folder on S3 (RustFS, MinIO, AWS):** one `add_data` per folder; the folder is one table.
   ```json
   {"project": "p", "table": "bronze_basic", "location": "s3://bucket/batch/report_a/*.parquet", "format": "parquet",
    "s3": {"endpoint": "127.0.0.1:9100", "url_style": "path", "use_ssl": false, "region": "us-east-1"}}
   ```
   - Any format: `*.csv` (add `"csv": {"quote": "\"", "escape": "\""}` for quoted multi-line fields), `*.json`,
     `*.xlsx` (`"xlsx": {"sheet": "Orders"}` for another sheet), `*.avro`; a Delta or Iceberg table is its folder
     (`s3://bucket/table/` with `"format": "delta"` or `"iceberg"`; Delta needs the endpoint and `url_style: path`).
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
4. **A database table (Postgres, MySQL):** the login is the person's: they add the table in **Data › Sources › Add a
   database table** (host, port, database, schema, table, user, password, an increasing column), and the app keeps the
   login as two ABI secrets bound to that host. Never ask for, repeat or pass a password or user name. Another table
   on the same database: `add_data` with the location and the same `${secret:NAME}` references (`list_secrets`):
   `{"table": "bronze_customers", "location": "postgres://db.example.com:5432/shop", "database": {"table":
   "public.customers", "user": "${secret:DB_P_ORDERS_USER}", "password": "${secret:DB_P_ORDERS_PASSWORD}",
   "incremental": "id"}}`. It reloads daily, appending rows past the increasing column (rows changed in place at
   the source stay as loaded until a full reload).
5. After loading: `data_quality` (rows, freshness, gaps). Row counts come from its rows only.
6. **Always continue with the medallion skill**: bronze sources (hidden), silver staged tables (cleaned, typed,
   combined; hidden: they are the Staged page), gold facts and dimensions (the model). profile-clean decides the
   silver steps; then build-model. Cleaning never happens outside ABI: it is silver's steps, so its lineage stays.
- A server path (a file in ABI's files folder) loads only what the person may read: their uploads, or a path one of
  their projects already loads; the workspace owner reads every path.
- The first load from a new host (the S3 endpoint, a database host, an https site) asks the person once (ABI's
  address policy): do that load yourself, not in a subagent; later loads from the same host don't ask.
