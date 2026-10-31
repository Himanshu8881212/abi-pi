# ABI-PI 1.0.0

A self-hosted agentic BI app in one container: ABI-PI's own copy of ABI's engine (Postgres, semantic engine, flows,
ML, custom agents), the Pi agent with a team of specialists, and a one-page app. Install: see [README.md](README.md)
(`.env` with the owner account, `docker compose up -d`).

## What is in it

- **Data in:** Parquet only, as an uploaded file or an S3 folder of Parquet files; medallion layers (bronze sources,
  silver staged tables, gold model) built by the agent or by hand.
- **Model:** measures, glossary, saved queries, named models, row-level security, a model canvas.
- **Visualize:** reports (drag and resize their cards; the layout is saved in the report), Explore with saved
  analyses, Monitor checks (a measure against a value, a target or its last completed period; freshness; failed
  flows), Home with pinned widgets (+ Add widget).
- **Automate:** flows, ML models, tools and custom agents, each from a form, from a description to the agent, or as
  JSON; the engine uses the same model as the agent, so custom agents answer and pass their evaluation sets.
- **Every page:** cards can be moved and resized (Developer); Publish shows the layout to Viewer; a collapsible sidebar.
- **Govern:** Developer works on the project and commits; Publish copies the last commit to a read-only copy that
  Viewer-only people read from their first page on; everyone with a role reads the published copy.
- **The agent:** builds from plain words through ABI's actions only, grounds every number in tool results, asks
  before destructive or outward steps, and can be followed live from another window.
- **Models:** from Settings only (an OpenAI-compatible proxy or an API provider); no ChatGPT account link.

## Checked for this release (31 Oct 2026)

- Offline tests: 78 passed (`.venv/bin/python -m pytest -q tests`), including a real-browser pass over every page.
- Container check on a fresh machine (Proxmox, Debian 13, Docker): 13 of 13 (`tests/container_check.sh`): fresh
  volume, load and query, PDF, PPTX, video, the agent, a hard kill, backup and restore, no secrets, non-root, health.
- Deployed end to end there: 7.4 million orders from S3; the agent built the whole project from eight plain requests;
  its 2024/2025 revenue by region matched an independent DuckDB sum to the cent; flows ran, the ML forecast beat its
  baseline, a custom agent passed its evaluation set.
- Publish and a real Viewer-only sign-in: 12 of 12 (reads only the published copy and layout; every change refused;
  no uncommitted work in its page or its queries).
- Live agent tests: 18 of 18 (`ABI_PI_LIVE=1 .venv/bin/python -m pytest -q -m live tests`), the agent driven like a person.

## Known limits

- HTTP only: put it behind an HTTPS reverse proxy for other devices (`X-Forwarded-Proto: https`).
- Email (invitations, flow emails) needs the engine's mail settings.
- A custom agent sometimes answers with its tool rows when ABI's strict number check rejects its wording.
