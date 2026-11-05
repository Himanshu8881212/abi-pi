# ABI-PI 1.2.0

A self-hosted agentic BI app in one container: ABI-PI's own copy of ABI's engine (Postgres, semantic engine, flows,
ML, custom agents), the Pi agent with a team of specialists, and a one-page app. Install: see [README.md](README.md)
(`.env` with the owner account, `docker compose up -d`).

## 1.2.0

The milestones' own measurements are in [bench/RESULTS.md](bench/RESULTS.md), each beside 1.1.0's.

### What is new

- **Your data, as it is** (M1): upload CSV, TSV, Excel (pick the sheet), JSON/NDJSON, Parquet or Avro files of any
  size, point at an S3 glob of any of those or a Delta or Iceberg table, or (workspace owners) add a Postgres or MySQL
  table with **Add a database table**; its login is stored encrypted for that host only and it reloads daily past an
  increasing column. An uploaded CSV keeps quoted line breaks in one row. A server path loads only what you may read.
- **HTTPS in one command** (M1): `COMPOSE_PROFILES=https` and `ABI_DOMAIN` in `.env` put Caddy in front with a
  certificate (a public name's from Let's Encrypt, `ABI_TLS=internal` for a LAN name or an IP address, or your own).
- **Results reach people** (M1): **Email this report** sends a report as a PDF on the days and at the time you pick;
  Monitor's measure checks alert in the new **Inbox**, and by email, with no browser open; Settings › Usage shows
  the cost of the models you give prices; every page links the running version's source (AGPL-3.0 §13).
- **Charts the report format promises** (M1): pie (donut), combo, stacked and grouped bars, a line per member,
  scatter, waterfall, funnel and treemap, each with tooltips from its rows; a click on any mark filters the report.
  Slicers filter the report again (they did nothing in 1.1), drawn as toggles; the filter bar is chips and a
  **+ Filter** picker.
- **"Why did it change?" with figures you can trust** (M2): one call (`explain_change`) gives the member that moved
  most, both periods, the change and its share; questions can compare periods inline (a change, a % change, a share)
  without saving a measure; a project's first build adds governed `*_prev`, `*_change` and `*_change_pct` measures;
  an answer is checked only against the rows of its own exchange and the one before.
- **Pages that don't queue** (M3, M3.6): page data is computed once for everyone who sees the same rows and handed to
  nobody else; people with row-level security share one read of a fact and each gets their own rows from it. Reads
  stop after `ABI_QUERY_TIMEOUT`, and at most `ABI_MAX_AGENTS` chats run their agent at once (one person at most half).
- **Big work in the background** (M3): a load of more than `ABI_INLINE_ROWS` rows answers at once and loads as a run
  you can follow on the Sources page and in the chat; a 100-million-row upload now builds its model; Publish reloads
  nothing it didn't change; one project's load no longer holds up another project, a schedule or an answer; PDF, PNG
  and PowerPoint exports and videos have time limits.
- **Self-healing** (M3): a killed engine or worker serves again by itself, Docker's health check sees the engine and
  the workers, and `docker stop` shuts Postgres down cleanly. A deleted project's uploads go with its lake, 30 days
  after the deletion, never one a live project still reads.
- **Heavy jobs on demand** (M4): the heavy worker (big loads, query copies, heavy flow runs, ML) runs only while there
  is work and is gone `ABI_RUNNER_IDLE` seconds after the last job, so its memory goes back; the `runner` profile
  gives heavy jobs a container and memory limit of their own; `abi-run <job id>` runs one queued heavy job for a
  launcher (the 1.3 Kubernetes Job contract).
- **Silver feeds gold** (M4): a table's `source` step may start from a table the project builds, so a bronze, silver,
  gold rebuild reads the source once and stores less.
- **A lighter image** (M4): `:latest` no longer carries Remotion; `:latest-video` adds it for explainer videos.
- **Data out** (M4): mint a **personal token** (click your name › Personal tokens) and read ABI as yourself from an
  MCP client at `/mcp` (13 read tools) or, with `ABI_SQL_PORT`, from psql, DBeaver or a BI tool over TLS; row-level
  security and column rules hold, nothing can be changed, every call is audited.

### Upgrade notes

`git pull && scripts/upgrade.sh` backs up the install, then upgrades it in place (README › Update). Before you do:

- **No way back without the backup.** The engine's database schema goes from 2 to 5 on the first start, and the
  volume records 1.2.0: a 1.1 engine refuses the upgraded database and a 1.1 image refuses the volume. To go back,
  restore the backup `scripts/upgrade.sh` made into a new volume (README › Back up and restore).
- **Videos need the `-video` image.** An install that makes explainer videos sets
  `ABI_IMAGE=ghcr.io/himanshu8881212/abi:latest-video` in `.env` first; without it the Video page says so, and PDF,
  PNG and PowerPoint exports work as before.
- **Profiles.** HTTPS is `COMPOSE_PROFILES=https`, the runner container `COMPOSE_PROFILES=runner`, both
  `COMPOSE_PROFILES=https,runner`; `scripts/upgrade.sh` and a plain `docker compose up -d` keep what `.env` says.
- **`ABI_DOMAIN` alone closes port 8790 to the network**: with HTTPS on, ABI's own port listens on this machine only
  (`ABI_BIND=0.0.0.0` opens it again), and `ABI_PUBLIC_URL` becomes `https://ABI_DOMAIN` unless `.env` sets one:
  remove or change an old `http://` address there.
- **Settings › Email now serves every project without a mail server of its own**: one set up for invitations in 1.1
  then also sends flows', the agent's and custom agents' emails, the first send to a domain asking once per project.
- **Monitor checks from 1.1** (or made by the agent) alert with the page closed only once you click **Alert with the
  page closed** on their card.
- **Published copies lose their flows** at the next Publish, so nothing sends twice; flows run once, in the project.
  Publish no longer reloads the copy's sources: only tables it changed load again.
- **Server paths** (files already in ABI's files folder) load only an upload into, or a path already loaded by, a
  project where the person reads every row; any other file there is the workspace owner's to load. Everyone else
  uploads their files.
- **Loads past `ABI_INLINE_ROWS`** (default 4,400,000 rows) answer with a run instead of waiting; a script that calls
  `add_data`, `set_steps` or `refresh` gets a run ID back for them.
- **DuckDB uses half the CPUs** ABI's process may use per project (a third in the runner), so one project's load leaves
  the rest to other projects' questions; set `ABI_DUCKDB_THREADS` for one big load or query to use more.
- **Heavy jobs move** off the always-on worker to the heavy worker (`ABI_HEAVY_CONCURRENCY`, `1` or `2`; any other
  value stops ABI from starting), or to the runner container with the runner profile. Jobs a 1.1 volume queued run
  as before.
- **Uploads of deleted projects** are removed 30 days after the deletion; for projects deleted under 1.1 (no list of
  their uploads), unlisted uploads wait until the last of those lakes is purged.
- **New settings** (all optional, defaults in README › All settings): `ABI_BIND`, `ABI_SOURCE_URL`, the HTTPS ones,
  the SQL endpoint's (`ABI_SQL_PORT`, `ABI_SQL_BIND`, `ABI_SQL_TLS_CERT`, `ABI_SQL_TLS_KEY`), `ABI_HEAVY_CONCURRENCY`,
  `ABI_RUNNER_IDLE`, `ABI_RUNNER_MEMORY`, `ABI_RUNNER_DUCKDB_MEMORY_LIMIT`, `ABI_DUCKDB_MEMORY_LIMIT` and
  `ABI_QUERY_CACHE_MB` (now passed by compose), `ABI_DUCKDB_THREADS`, `ABI_MAX_AGENTS`, `ABI_QUERY_TIMEOUT`, `ABI_RLS_SHARED_ROWS`, `ABI_PG_POOL`, `ABI_INLINE_ROWS`, `ABI_RENDER_TIMEOUT`
  and `ABI_VIDEO_TIMEOUT`.

### Known limits in 1.2

- A "why" answer is masked at times: M2's round 3 passed 19 of 20 "why" runs, and 8 of 10 in a small sample after
  its review fixes; the misses were masked count words ("two Retail products"). Counts never ground, so a sentence
  that counts is masked rather than trusted.
- While a big load builds its model, other projects' uncached questions slow down more than during the rest of the
  load (bench/RESULTS.md, M4 round 3: p95 x3.30 during build_model, x1.39 over the whole load).
- After a heavy job the runner container's memory stays above idle in reclaimable kernel cache (its processes' own
  memory goes back).
- Row-level and column rules go on the gold tables; a bronze or silver table left visible (not hidden) reads the same
  for everyone with a role over every door. Hide staging tables.
- AVG visuals read each restricted person's own rows (no shared read).
- Remote runners (Kubernetes) are 1.3; 1.2 runs heavy jobs on the same machine.
- The heavy worker has crashed in a query copy right after a project was deleted; the supervisor restarts it and the
  job ends failed within a minute.

### Checked for 1.2.0

The release run fills this in with measured numbers only.

- Offline tests: _to be measured_
- Live agent tests: _to be measured_
- Container check on pve1 (5 Nov; 8c266ce built there from `git archive`, linux/amd64): `ABI_IMAGE=abi-rc:1.2.0
  tests/container_check.sh` 15 of 15 in 150 s (fresh volume up in 13 s, the engine and the worker killed with -9
  serving again in 8 s, `docker stop` in 2 s, no secret in the logs, the volume, the image or the backup; video says
  how to switch), the -video image 15 of 15 in 176 s with a video rendered. `docker image inspect --format
  '{{.Size}}'`: default 2,471,828,806 bytes, -video 2,695,718,474 (1.1.0: 2,843,622,285). On the upgraded test
  install below, each profile on test ports:
  - https with `ABI_TLS=internal`: a certificate for the name from Caddy's own authority (verified against its
    `root.crt`), http answered 308 to https, sign-in 303 over https, and `/events` streams through Caddy (an event
    the app sent 4 s into the stream arrived 0.22 s later, keep-alives every 0.3 s); 8790 then listened on 127.0.0.1
    only.
  - runner: a 7,400,054-row Parquet upload through the app answered at once (queued) and loaded in `abi-runner`
    (the job 72.1 s, done 82.3 s after the upload began; 7,400,054 rows, revenue 42,483,442.50 exact); its heavy worker stopped 180 s after
    the queue emptied ("stopped after 256 s"), the container's memory from 207 MB back to 4.3 MB.
  - `ABI_SQL_PORT` with TLS (a self-signed certificate) from the Proxmox host, a Viewer's personal token: psql
    `sslmode=verify-full`, TLSv1.3; `orders` by café gave only `CF001|54540|312054.64` (the Viewer's rule); 150,000 rows
    refused at 100,000; `DELETE` refused; a plain connection refused ("TLS is required from this address"); the
    revoked token refused.
  - MySQL 8.4 (a throwaway container on the install's network, Brewline 0.1's 739,826 orders) through the Sources
    page's Database card: revenue by region equal to MySQL's own `SUM` to the
    cent (East 1,103,521.44, North 1,012,273.62, South 1,017,927.17, West 1,126,592.58, the same row counts); the
    source file holds only `${secret:…}` references, and the password is in none of the three containers' logs, the
    files under `/data` and `/app`, the 5 Git histories, a database dump or the pages.
- A 1.1.0 volume upgraded with `scripts/upgrade.sh` on a test install (pve1, 5 Nov; the 1.1.0 image 2fcf2e3d5762,
  1.1.0's compose file and scripts). Seeded as a company uses 1.1: Brewline scale 1 (7,400,054 orders) uploaded as
  Parquet through the app, an Owner, an Editor and a Viewer whose rule shows café CF001 only, the starter report, a
  Monitor check, a flow scheduled for Mondays 07:00, a published copy, a vault document and one chat. Then the
  README's update: this branch's files over the folder (as `git pull`), `ABI_IMAGE=abi-rc:1.2.0` in `.env`,
  `scripts/upgrade.sh`: no registry, so it built the image there; done in 50 s with an 813 MB backup first. After it,
  all 16 recorded items were equal: revenue by region to the cent as the Owner (engine and app) and in the published
  copy (East 10,998,627.65, North 10,090,298.06, South 10,170,714.23, West 11,223,802.56, the generator's own sums),
  the Viewer's page on the published copy with only CF001 (54,540 orders, 312,054.64), the three sign-ins, the flow
  and its schedule, the check, the report, the rule, the vault document, the chat and the history. `schema_state`
  says schema 5 (1.1.0: 2); Monitor and Inbox drew in Chromium with no script error (the 1.1 check offers "Alert
  with the page closed"). `scripts/backup.sh` and the README's restore into a new volume: the same 16 items. Going
  back: the 1.1.0 image refuses the volume ("This volume holds data of ABI 1.2.0 ... no downgrades"); with
  `/data/VERSION` set back by hand its engine refuses the database (schema 5 over 2) and the container exits; the
  upgrade's own backup restored with the 1.1.0 image gives the same 16 items. Fixed on the way: that backup was
  world-readable (`-rw-r--r--`); `scripts/upgrade.sh` now writes it readable by its owner only.
- 50 sessions after a commit, page p95 (without and with `--rls`); a 100M-row upload; Brewline revenue by region to
  the cent, on the release image: _to be measured_
- The arm64 image started (`.github/workflows/image.yml`): _to be measured_

## What is in it

- **Data in:** Parquet only, as an uploaded file or an S3 folder of Parquet files (from 1.2: CSV, TSV, Excel, JSON
  and Avro too, Delta and Iceberg tables on S3, and Postgres and MySQL tables); medallion layers (bronze sources,
  silver staged tables, gold model) built by the agent or by hand.
- **Model:** measures, glossary, saved queries, named models, row-level security, a model canvas.
- **Visualize:** reports (drag and resize their cards; the layout is saved in the report), Explore with saved
  analyses, Monitor checks (a measure against a value, a target or its last completed period; freshness; failed
  flows), Home with pinned widgets (+ Add widget). (From 1.2: measure checks saved on the Monitor page alert in the
  Inbox and by email with no browser open, a check from 1.1 or the agent's once you click Alert with the page closed
  on its card; an Inbox screen.)
- **Automate:** flows, ML models, tools and custom agents, each from a form, from a description to the agent, or as
  JSON; the engine uses the same model as the agent, so custom agents answer and pass their evaluation sets.
- **Every page:** cards can be moved and resized (Developer); Publish shows the layout to Viewer; a collapsible sidebar.
- **Govern:** Developer works on the project and commits; Publish copies the last commit to a read-only copy that
  Viewer-only people read from their first page on; everyone with a role reads the published copy.
- **The agent:** builds from plain words through ABI's actions only, grounds every number in tool results, asks
  before destructive or outward steps, and can be followed live from another window.
- **Models:** from Settings only (an OpenAI-compatible proxy or an API provider); no ChatGPT account link.

## Checked for 1.0.0 (31 Oct 2026)

- Offline tests: 78 passed (`.venv/bin/python -m pytest -q tests`), including a real-browser pass over every page.
- Container check on a fresh machine (Proxmox, Debian 13, Docker): 13 of 13 (`tests/container_check.sh`): fresh
  volume, load and query, PDF, PPTX, video, the agent, a hard kill, backup and restore, no secrets, non-root, health.
- Deployed end to end there: 7.4 million orders from S3; the agent built the whole project from eight plain requests;
  its 2024/2025 revenue by region matched an independent DuckDB sum to the cent; flows ran, the ML forecast beat its
  baseline, a custom agent passed its evaluation set.
- Publish and a real Viewer-only sign-in: 12 of 12 (reads only the published copy and layout; every change refused;
  no uncommitted work in its page or its queries).
- Live agent tests: 18 of 18 (`ABI_PI_LIVE=1 .venv/bin/python -m pytest -q -m live tests`), the agent driven like a person.

## 1.0.1 (31 Oct 2026)

Checked: offline 80 passed, live agent tests 18 of 18, container check 13 of 13 on the Proxmox machine.


- Custom agents answer in words instead of falling back to raw tool rows: they may name the period their query
  was filtered on (a year, a month, a date range's start) and show a ratio as a percent, and the run trace keeps what
  the number check rejected or an empty model answer (9 of 9 plain questions answered, all figures checked).
- The agent form offers everything a custom agent may use: queries, report snapshots, email, messages, table refresh,
  proposed edits, your tools and other published agents, plus tables in its scope; a chosen tool brings its measures
  and tables into the scope. Custom agents send or post only when asked, and a send's number check knows the period.
- Every agent card has **Evaluation set**: see, add, change and remove its test questions, save, and run them.
- A name the person typed also grounds in its plural ("Monday" → "Mondays"), so it is no longer masked.

## Known limits

- HTTP only: put it behind an HTTPS reverse proxy for other devices (`X-Forwarded-Proto: https`). (From 1.2: HTTPS in
  one command, README › Use it from other devices.)
- Email (invitations, flow emails) needs the engine's mail settings. (From 1.1: invitations and password resets use
  Govern › Settings › Email; a flow's emails use the project's own mail server, which the agent sets. From 1.2: a
  project without its own sends all its email through Settings › Email: Email this report, Monitor alerts, flows, the
  agent and custom agents, the first send to a domain asking once per project; a mail server set up for invitations
  in 1.1 serves every such project after the upgrade.)
- DuckDB stays 1.5.6 (DuckLake catalog 1.0) in 1.2; DuckDB 2.0 / DuckLake 1.1 comes in 1.3 with a one-off catalog
  migration after a backup ([docs/decisions/duckdb.md](docs/decisions/duckdb.md)). From 1.2: the lake's data path
  may be an `s3://` URL as far as rename, delete and purge go (the lake on S3 itself is 1.3).
- From 1.2, explainer videos need the `-video` image: `:latest` no longer carries Remotion, so an install that makes
  videos sets `ABI_IMAGE=ghcr.io/himanshu8881212/abi:latest-video` in `.env` before `scripts/upgrade.sh` (README ›
  The two images). Without it the Video page says so, and PDF, PNG and PowerPoint exports work as before.
