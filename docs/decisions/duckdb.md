# DuckDB 2.0 / DuckLake 1.1: upgrade in 1.3, not in 1.2

Decided 5 Nov 2026 (ABI-PI 1.2, M4.4). 1.2 stays on what `engine/backend/uv.lock` pins: DuckDB 1.5.6 and its
DuckLake extension, whose catalog format is version 1.0. No lock file changes.

## What is current

- PyPI: `duckdb` 2.0.0 (21 Oct 2026) is the newest stable release; 2.0.1 and 2.1.0 exist only as dev builds.
- DuckDB 2.0.0 is an LTS release, supported until at least October 2027. It ships DuckLake 1.1 (extension
  `417a9117f5`), which has a new catalog format, version 1.1. DuckDB 1.5 is supported until March 2027.
- The 2.0 release notes list these breaking changes: database files written by 2.0 can't be opened by older versions;
  the PEG parser is now the default; the single-arrow lambda `x -> x + 1` is off (use `lambda x: …`) and goes in 2.1;
  `MERGE` / `UPDATE … FROM` behave differently when several rows match; ICU now lives in the `icu` extension (it is
  statically linked in the Python wheel); `osx_amd64` is being phased out.

## What was tried

The test ran on a copy, never on the shared engine's data: a private Postgres 17 on its own port, a lake folder in
a scratch directory, and a throwaway venv with `duckdb==2.0.0` plus the engine's other pinned packages. DuckDB 2.0's
extensions (`ducklake`, `postgres_scanner`, `httpfs`) went into a separate HOME. All of it was deleted afterwards.
The test drove the engine's own code (`abi.storage`, `abi.writeback`) plus the SQL patterns the semantic engine sends:

1. **1.5.6 makes the project.** `storage.connect` created a catalog with version 1.0. `storage.refresh` loaded
   2,000 orders (raw and model, one snapshot), and a write-back edit went into `lake.edits.orders` (inlined).
2. **2.0.0 opens it as it is.** `storage.connect` works without change, including the attach options,
   `pg_pool_max_connections = 0` and `lake.set_option`. The catalog is **not migrated**: `ducklake_metadata.version`
   stays `1.0`. These all gave exactly the 1.5.6 results:
   - `writeback.views` effective tables, and the edited cell read back as 100.00;
   - an RLS view in `memory.<schema>` in front of `memory.effective` and `lake.model` via `search_path`
     (North: 500 rows, 29625.00);
   - `timezone` / `date_trunc` / `strftime` / `dayofweek`;
   - time travel `AT (VERSION => 4)`, `lake.snapshots()`, and the `ducklake_table`/`ducklake_schema` read in
     `semantic._copies`;
   - a Parquet `COPY … TO` export (500 rows) and an Arrow reader (2,000).
3. **2.0.0 writes to the 1.0 catalog.** These all ran, and the catalog stayed at `1.0`: a reload of 3,000 rows, an
   edit, `storage.restore` to snapshot 4 (2,000 rows), a reload, `archive`, `expire_snapshots`.
4. **1.5.6 reads it back.** It attached the catalog 2.0 had written to and gave the same answers, including the
   Parquet files 2.0 wrote. So **running 2.0 on a 1.0 catalog can be undone by going back to the old binary**.
5. **Migrating is explicit and one-way.** Only `ATTACH … (AUTOMATIC_MIGRATION true)` moves a catalog to `1.1`. It adds
   `ducklake_view_column_tag` and new inlined-data tables, and leaves the data files alone. After that, 1.5.6
   refuses it: "DuckLake catalog version mismatch: catalog version is 1.1, but the extension requires version 1.0".
   2.0 ran steps 2 and 3 again on the 1.1 catalog with the same results.
6. **A new lake made by 2.0 starts at 1.1.** 1.5.6 can't open it.

## Decision

Upgrade to DuckDB 2.0 (DuckLake 1.1) in **1.3**, as its own step, before the lake moves to S3. Then a failure points
at one change, not two. 1.2 has M4.4's s3-safe storage paths, so the upgrade doesn't change path handling. 1.5 stays
supported until March 2027, so 1.2 doesn't need the upgrade.

## Migration steps (1.3)

1. Back up first (`scripts/backup.sh`: catalog and lake). This backup is the only way back once a catalog is at 1.1.
2. Stop the app, every worker and any runner (the `runner` compose profile, the 1.3 Kubernetes jobs). Everything
   that opens a lake must run the same DuckDB: a 1.5.6 process can't open a 1.1 catalog, and new lakes are 1.1 from
   the start.
3. Bump `duckdb` to `>=2.0,<2.1` in `engine/backend/pyproject.toml` and the lock. Rebuild the image: the Dockerfile's
   `install_extension` list must fetch the 2.0 builds, and `icu` is in the wheel.
4. Migrate each project's catalog once, with a one-off step in `scripts/upgrade.sh`:
   `ATTACH 'ducklake:postgres:' AS lake (METADATA_SCHEMA '<project>', AUTOMATIC_MIGRATION true)`. Include the
   `_deleted_*` schemas, so `purge_deleted` can still drop them. Never add `AUTOMATIC_MIGRATION` to
   `storage.connect`: a migration should happen once, on purpose, after a backup.
5. Run the offline suite and the container check on 2.0 before the release.

## Rolling back

- Before step 4: put the 1.5.6 image back. The catalogs are still 1.0, and the files 2.0 wrote can be read (step 4
  above). Projects created in between have 1.1 catalogs, so they need a backup restore.
- After step 4: restore the backup from step 1, which loses everything written since. There is no 1.1 → 1.0
  downgrade.

## Risks found

- `cur.execute(sql).to_arrow_reader(n)` (the SE-9 Arrow export in `semantic`) now warns that it copies the rows
  once more; 2.0 wants `con.sql(sql).to_arrow_reader()`. The result is the same, but memory is higher on big exports.
- The PEG parser and the MERGE/UPDATE change apply to every SQL string the planner and sqlglot produce. The probe
  covered the main patterns, not the full suite. The engine has no single-arrow lambdas and no DuckDB `MERGE` (its
  `UPDATE … FROM` statements are Postgres ones).
- During an upgrade, mixed versions break: an un-upgraded runner, or a second engine on 1.5.6, can't open a migrated
  or new lake. The runner contract (`abi-run`) should check the version.
- Leftover backups and `_deleted_*` lakes have to be migrated as well, or purged before the upgrade.
- The repo's root test venv (`uv.lock` at the root, tests only) already uses `duckdb` 2.0.0 to check figures by hand.
  It never attaches a lake, and it must not before 1.3.
