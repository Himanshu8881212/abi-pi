---
name: load-data
description: Load files or a database into a new or existing ABI project. Use for "load this", "here are some files/parquet/csv", "connect to postgres", a folder of data.
---
# Load data
1. Pick IDs: workspace (default `main` or the person's preference) and project (lowercase, digits, `_`, starts with a letter).
2. Files on this machine: `abi_load {workspace, project, paths}`. It creates the project and folder, uploads each file
   (CSV with explicit quoting first: DuckDB's guess splits quoted multi-line fields), types a date column, builds the
   model, adds starter measures and an Overview report, and renders views. Read its JSON: `loaded`, `failed`,
   `readiness.missing`.
3. A server path, URL or database: `abi_tools ["data"]`, then `add_data` (credentials only as `${secret:NAME}` the
   person created in ABI).
4. Check row counts in the `loaded` list against what the person expects; a CSV with broken rows shows as NULLs in a
   column that should be full: say so (data_quality).
5. Continue with the profile-and-clean skill if steps are needed, then build-model.
Lessons: profile before modelling; one file = one table; staging tables that only feed another table get `hidden`.
