# ABI-PI

A self-hosted agentic BI app on its own copy of ABI's engine: the Pi coding agent (with a team
of ten specialists) builds and answers through ABI's actions, and a one-page app gives people the whole project in
pipeline order — Data, Data Model, Visualizations, Export, Automate, Knowledge, Govern — with a chat that also
navigates. Everyone signs in with their own ABI account; the app acts as them.

## Run it in Docker

One container holds everything: ABI-PI's own engine (a copy of ABI's backend, `engine/`) with its Postgres, the
agent and the app. You need only a model endpoint (or connect one in the app).

1. Create `.env` next to `docker-compose.yml` (it is git-ignored; keep it to yourself):

   ```
   ABI_USER=admin                 # the workspace owner, created on the first start
   ABI_PASSWORD=...               # 8 characters or more
   ABI_PI_PROJECT=main            # the project to open first (created if missing)
   ABI_PI_SECRET_KEY=...          # optional: encrypts the API keys admins save (Fernet key)
   ```

2. Start it:

   ```
   docker compose up -d
   ```

3. Open <http://localhost:8790> and sign in as `ABI_USER`. As the workspace owner:
   - **Govern › Settings**: connect the agent's model provider (OpenAI, Anthropic, OpenRouter, Ollama, Gemini or any
     OpenAI-compatible endpoint), pick the model and how hard it reasons. API keys are stored encrypted and never shown
     again. (Or pass one endpoint as `ABI_PI_LLM_BASE` / `ABI_PI_LLM_KEY`.)
   - **Govern › Users & access**: give people roles, sections and reports.

All data (the engine's database and lake, projects, vault, chats, board packs) lives in the `abi-pi-data` volume:
back it up as a whole. For other devices, put it behind an HTTPS reverse proxy that sends `X-Forwarded-Proto: https`.

## Back up and restore

```
docker exec abi-pi scripts/backup.sh backup > abi-pi-backup.tar   # the database dump + all files, checksummed
docker exec abi-pi scripts/backup.sh keys   > abi-pi-keys.tar     # the two key files: keep them somewhere else
```

The app keeps serving during a backup (the worker pauses). To restore, into a new, empty volume (the same `/data` path):

```
docker run --rm -i -v abi-pi-data:/data -v $PWD/abi-pi-keys.tar:/keys.tar:ro --entrypoint scripts/backup.sh \
  abi-pi:latest restore /keys.tar < abi-pi-backup.tar
docker compose up -d
```

Without the keys the data, reports, history and logins come back, but stored source secrets and the API keys saved in
Settings must be entered again.

## Run it on a machine

```
uv sync && (cd engine/backend && uv sync --frozen --no-dev)                       # once
(cd engine/frontend && npm ci && npx vite build --outDir ../backend/static --emptyOutDir)   # once
scripts/engine.sh start                                   # Postgres (brew install postgresql@17), engine, worker
scripts/accounts.py                                       # first run: accounts, passwords in data/accounts.txt
.venv/bin/python scripts/app.py <project> 8790            # sign-in for everyone
.venv/bin/python scripts/app.py <project> 8790 --single   # one person, this machine only
```

## What is where

| Path | What |
|---|---|
| `scripts/app.py` | the app (one server, every workspace and project) |
| `engine/` | the own engine: a copy of ABI's backend, schemas and UI (`engine/README.md`) |
| `data/` | the engine's database, lake and projects on a machine (git-ignored; `/data` in the container) |
| `.pi/` | the agent: system prompt, extensions (ABI tools, team, confirmations, grounding), skills, specialists |
| `harness/` | the team (`team.json`) and ABI's tool groups (`areas.json`) |
| `scripts/` | harness steps: `abi.py`, `team.py`, `pack.py`, `video.py`, `transfer.py`, `vault.py` |
| `tests/` | `.venv/bin/python -m pytest -q tests` |
| `REPORT.md` | what was built, ABI's gaps, the release loop and the release checklist |

The video export uses [Remotion](https://www.remotion.dev): free for individuals and companies of up to three people;
larger companies need Remotion's company licence.
