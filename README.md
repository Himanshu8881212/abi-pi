# ABI-PI

An agent harness and app for [ABI](https://github.com/), the agentic open BI platform: the Pi coding agent (with a team
of ten specialists) builds and answers through ABI's actions, and a one-page app gives people the whole project in
pipeline order — Data, Data Model, Visualizations, Export, Automate, Knowledge, Govern — with a chat that also
navigates. Everyone signs in with their own ABI account; the app acts as them.

## Run it in Docker

You need a running ABI (its URL and its projects folder) and an OpenAI-compatible model endpoint.

1. Create `.env` next to `docker-compose.yml` (it is git-ignored; keep it to yourself):

   ```
   ABI_URL=http://host.docker.internal:8770
   ABI_PROJECTS_DIR=/path/to/abi/projects       # ABI's projects folder (mounted read-only)
   ABI_PI_PROJECT=my_project                    # the project to open first (created in ABI if missing)
   ABI_USER=service-account                     # an ABI account the app syncs project copies with
   ABI_PASSWORD=...
   ABI_PI_SECRET_KEY=...                        # optional: encrypts the API keys admins save (Fernet key)
   ```

2. Start it:

   ```
   docker compose up -d
   ```

3. Open <http://localhost:8790> and sign in with your ABI account. As a workspace owner:
   - **Govern › Settings**: connect the agent's model provider (OpenAI, Anthropic, OpenRouter, Ollama, Gemini or any
     OpenAI-compatible endpoint), pick the model and how hard it reasons. API keys are stored encrypted and never shown
     again. (Or pass one endpoint as `ABI_PI_LLM_BASE` / `ABI_PI_LLM_KEY`.)
   - **Govern › Users & access**: give people roles, sections and reports.

The container keeps its data (project copies, vault, chats, board packs) in the `abi-pi-data` volume. For other
devices, put it behind an HTTPS reverse proxy that sends `X-Forwarded-Proto: https`.

## Run it on a machine

```
uv sync
.venv/bin/python scripts/app.py <project> 8790            # sign-in for everyone
.venv/bin/python scripts/app.py <project> 8790 --single   # one person, this machine only
```

## What is where

| Path | What |
|---|---|
| `scripts/app.py` | the app (one server, every workspace and project) |
| `.pi/` | the agent: system prompt, extensions (ABI tools, team, confirmations, grounding), skills, specialists |
| `harness/` | the team (`team.json`) and ABI's tool groups (`areas.json`) |
| `scripts/` | harness steps: `abi.py`, `team.py`, `pack.py`, `video.py`, `transfer.py`, `vault.py` |
| `tests/` | `.venv/bin/python -m pytest -q tests` |
| `REPORT.md` | what was built, ABI's gaps, the release loop and the release checklist |

Limits in the container: the release agent's `abi_cli` needs ABI's own code next to it and is not available.
The video export uses [Remotion](https://www.remotion.dev): free for individuals and companies of up to three people;
larger companies need Remotion's company licence.
