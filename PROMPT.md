# Build "ABI on Pi": an agent harness that is ABI

Paste everything below the line into a new Claude Code session started in `~/Work/Working/abi-pi`.

---

## Goal

Build a working prototype that turns **Pi** (Mario Zechner's minimal, extensible coding agent: repo `badlogic/pi-mono`,
package `@mariozechner/pi-coding-agent`) into **ABI**, the Agentic Open BI platform. When it's done I point it at a data
source, ask a business question or give a brief ("load this, model it, build a report, forecast next month, email me
every Monday"), and it does it end to end through ABI.

This is a spike before ABI's phase 11 task AGENT-HARNESS (see `~/Work/Working/ABI/work/BACKLOG.md`, decision D-131 in
`work/DECISIONS.md`). Work only in `~/Work/Working/abi-pi`. **Never modify `~/Work/Working/ABI`.** Other agents build
there all the time; read it, never write it, never run its tests or `docker` commands on its containers.

## What ABI already gives you (read these first)

- `~/Work/Working/ABI/work/FEATURES.md`: everything ABI does today.
- `~/Work/Working/ABI/CLAUDE.md`: the rules every ABI agent follows (principles 1-7).
- `~/Work/Working/ABI/backend/abi/agent/__init__.py`: the Main Agent's system prompt (`_system`), tool areas
  (`AREAS`, `chat_tools`), grounding and confirmation flow. This is the harness ABI built itself; you are re-housing it.
- `~/Work/Working/ABI/backend/abi/agent/mcp.py`: ABI's MCP server.
- `~/Work/Working/ABI/backend/abi/sdk.py`: the Python SDK (REST client).
- `~/Work/Working/ABI/work/demo/seg/*.py` and `work/demo/GUIDE.md`: a full worked build (YouTube trending data:
  load → model → reports → ML → write-back → flows → promote → portal → agents → quality). Use it as the source for
  playbooks.

**The running ABI to use:**
- ABI runs at **http://127.0.0.1:8770** with the YouTube demo already built, in projects `yt` and `yt_prod`.
- **Sign in:** user `himanshu`. Read the password with
  `grep -E '^[[:space:]]*himanshu\b' ~/abi-preview/accounts.txt | awk '{print $NF}'` into a variable, never into
  output, files, git or the LLM.
- **Get a session token:** `POST /api/login {"name","password"}` returns `{"token"}`. Keep it in an env var only.
- **MCP:** Streamable HTTP at `http://127.0.0.1:8770/api/mcp`, header `Authorization: Bearer <token>`. It exposes the
  Main Agent's tools (about 170 actions).
- **REST:** `POST /api/actions/<name>` with the same bearer token. A 409 answer carries `message`, `confirm_token` and
  `preview`; repeat the call with header `X-ABI-Confirm: <token>` after the person agrees. Check the exact header name
  in `sdk.py`.

## Model

Use **GPT-6.1 Sol** as Pi's model for the harness, for the main agent and for every subagent.
- **Sign-in:** I sign Pi in myself (Pi's `/login` with my ChatGPT/OpenAI account, or an API key I set in the
  environment). Never ask me to paste a key into the chat, and never write one to a file.
- **Model ID:** pick the exact model ID Pi lists for GPT-6.1 Sol (for example `gpt-6.1-sol`, the ID ABI itself uses on the ChatGPT plan). If Pi doesn't list it,
  stop and tell me what it does list; don't swap in another model silently.
- **Configuration:** set it in the project-level Pi settings in this folder, so the harness always starts on it.
  Record the ID and the reasoning-effort setting in `SUPPORT.md`.

## Step 0: check Pi supports all eight parts, install what's missing

Do this before building anything. For each of the eight parts below (system prompt, rules, memory, tool notes, skills,
scripts, subagents, plugins), check whether Pi supports it natively:
- **Read Pi's own docs:** the `pi-mono` README, its docs folder and examples for extensions, skills, prompt templates,
  `AGENTS.md`/context files, settings and packages. Run `pi --help` once it is installed.
- **If not native, find an add-on:** for anything Pi lacks (likely candidates: MCP client, subagents, persistent memory,
  a permission/confirmation gate), look for an existing Pi extension or package that adds it: official `pi-mono`
  packages first, then well-maintained community packages (check the source before installing; no install scripts that
  phone home).
  - Install it locally in this folder (project-level, not global) and record name, version and source.
  - Only if no add-on exists, write a small Pi extension yourself.
- **Write `SUPPORT.md`:** a table with one row per part:
  - part;
  - native in Pi? (yes/no, which mechanism);
  - add-on installed (name, version, link);
  - or own extension (file);
  - how you checked it works (one test or command).

Proceed to building only when every row has a working mechanism.

## The eight harness parts to build

Build each as Pi-native pieces (extensions, skills, prompt templates, `AGENTS.md`/system prompt, settings). First read
Pi's current docs and examples in `pi-mono` to see which mechanism fits each; don't guess its API.

1. **System prompt.** Port ABI's Main Agent prompt: who it is, how to find measure IDs, "every number comes from a tool
   result", project context, today's date and time zone last (stable prefix for caching).
2. **Rules.** ABI's principles as hard rules:
   - read data only through `semantic_query`/datasets (semantic engine);
   - change things only through actions;
   - numbers from tools, words from the model;
   - confirm before destructive or outward-facing steps;
   - secrets never in prompts, logs or files;
   - P0 behaviour only.
3. **Memory.** ABI project memory (`remember`, `forget`, glossary) as the long-term store, plus a small Pi-side session
   memory of the person's preferences (recipients, currency, formats), private to them.
4. **Tool notes.** Short usage notes per tool group: data, model, measures, reports, write-back, ML, flows, agents,
   tools, security, Git. Base them on the action descriptions and the `AREAS` grouping. Load a group's notes only when
   it is needed, the way ABI's `more_tools` does.
5. **Skills.** On-demand playbooks: load data, profile and clean, build the model, write measures (MEL), build a report,
   write-back/planning, forecast and ML, flows and alerts, a custom agent, promote to production. Each carries the
   lessons from the demo build (profile before modelling, forecasts need a date column, and so on).
6. **Scripts.** Deterministic helpers the agent can run: sign-in/token refresh, an import/validate/apply of a project
   bundle via ABI's CLI (`abi validate|diff|apply|watch`), a health check, and a grounding checker (below).
7. **Subagents.** At least: a *Data engineer* (load, clean, model), an *Analyst* (measures, reports, answers), an *ML*
   agent and an *Automation* agent (flows, alerts), each with only its tool groups. Also ABI's own custom agents, callable
   through `run_agent`.
8. **Plugins/extensions.** The Pi extension that connects to ABI over MCP. If Pi has no MCP client, write one, or wrap
   REST `/api/actions/<name>` as Pi tools generated from `GET /api/actions` (schemas included). Add a confirmation
   bridge and status UI.

## Everything is code: workspace and project folders

ABI is code-based: every project is a Git repository of JSON files. The harness keeps the same layout on disk, so I can
open any folder and see exactly what the agent built.

- **`workspaces/`** holds one folder per workspace. A new workspace is an **empty** folder (nothing inside until its
  first project).
- **`workspaces/<workspace>/<project>/`** is one folder per project. When a project is created, the harness creates the
  **full skeleton at once**, empty, matching ABI's layout exactly:
  ```
  <project>/
    project.json                 name, schema_version, settings (time zone, currency, delivery, remote)
    sources/                     <id>.json   where each table's data comes from
    tables/                      <id>.json   transform steps; input tables (steps: [])
    model/                       model.json, measures.json, glossary.json, datasets.json
    reports/                     <id>.json   pages, visuals, filters, layout
    ml/                          <id>.json   ML model definitions
    flows/                       <id>.json   triggers and steps
    agents/                      <id>.json + <id>.eval.json   custom agents and their evaluation sets
    tools/                       <id>.json   query, Python and API tools
    security/                    roles.json, rls.json
    environments/                <branch>.json   per-environment connections
    docs/                        descriptions.json and generated Markdown
  ```
  Empty folders get a `.gitkeep`, so Git keeps them. The folders fill up as the agent works on the project.
- **Check the layout against ABI itself:** read `~/Work/Working/ABI/schemas/v1/*.json` (one schema per file type,
  `$schema` URL `https://schema.abi.invalid/v1/<type>.json`) and a real project at `~/abi-preview/projects/yt/`. Every
  file the harness writes must validate against its schema.
- **ABI stays the source of truth.** The agent changes projects only through ABI actions (MCP/REST), never by editing
  JSON behind ABI's back. After each change the harness syncs ABI's files down into the project folder:
  - **Turn on auto-commit:** call `set_auto_commit` on for each harness project, so each change is a commit.
  - **Pull read-only:** pull from ABI's own repository with `git -C <project folder> pull` (the folder is a
    `git clone ~/abi-preview/projects/<id>`).
  - **Never write there:** never write into `~/abi-preview/projects/` itself.
  - **Commits only:** there is no bundle-download route; ABI's `/projects/{p}/export` exports data, not definitions.
    Don't copy uncommitted files.
  - **Hand edits:** a file I change in the folder goes back up through ABI's import checks with `abi watch <folder>`.
    The agent itself always goes through actions.
- **Project = ABI project, workspace = grouping on disk.** Create each project in ABI with `create_project` (one ABI
  instance is one ABI workspace; ABI's tenants isolate client organisations). Map the harness's workspace folders onto
  that and note the mapping in `REPORT.md`.
  - **Name rule:** project IDs use ABI's pattern, lowercase letters, digits and `_`, starting with a letter. No hyphens.
- **Known gap:** ABI's own `create_project` today writes only `project.json`, and the other folders appear when their
  first file is saved. The harness creates the full empty skeleton itself; record in `REPORT.md` that ABI should do the
  same (phase 11 input).

Add to the tests: creating a workspace yields an empty folder; creating a project yields the full skeleton (every folder
above, plus a `project.json` that validates); after scenario 1 the folder holds the source, table, model and measure
files that ABI holds (compare against ABI's bundle).

## Guardrails (non-negotiable)

- **ABI is the only door to data.** Turn off or sandbox Pi's own bash/read/write/edit tools for data work: the agent
  must not read the lake, Parquet files, Postgres or `~/abi-preview` directly. Keep file tools only for the harness's own
  folder.
- **Confirmations go to the person.** When ABI asks for a confirmation (REST 409, or an MCP error saying "do it in the
  app"), the harness shows the person ABI's `message` and `preview` and waits for an explicit yes. The model never sees
  or holds the confirm token, and never confirms on its own.
- **Grounding.** ABI grounds its own chat, but MCP/REST callers are not grounded. Build a harness-side check:
  - every number and named member in the final answer must appear in a tool result's `rows` from this conversation (or
    in the question);
  - otherwise retry once with "only state figures from the tool results", then mask the ungrounded figures;
  - copy ABI's rules from `backend/abi/agent/grounding.py` (read it); keep it a small, testable script.
- **Secrets.** No password, token or `${secret:…}` value in prompts, transcripts, logs, git or the LLM. Credentials for
  sources are created by the person in ABI (`set_secret` is never an agent tool).
- **Row-level security** applies automatically because calls run as the signed-in user; never sign in as someone else to
  see more.

## Demo and tests

Write `DEMO.md` with a script I can run live, plus automated checks in `tests/`:

1. Point it at a new CSV (copy one file from `~/Work/Working/ABI-demo/youtube_trending_real/` into a scratch folder).
   Ask it to load the file into a new project, build the model and tell me the top 5 categories by views. It must end
   with a Ready model and figures that match a hand-written DuckDB query over the same CSV (the check computes both).
2. On `yt`: "Why did views drop last month?" and "Build a one-page report of views by country and category." The report
   must exist in ABI (`list_reports`), and the answer must be grounded.
3. Write-back: "Raise the July target for Gaming by 10%." This becomes a proposal, and the harness shows ABI's preview
   and waits.
4. ML: "Forecast views per category for the next 4 weeks." Train and score through ABI, and report accuracy against the
   baseline from `ml_models` rows.
5. Automation: "Email me a weekly summary every Monday at 8." Create the flow, then stop at the first-send confirmation
   and ask me.
6. **Guardrail tests that must pass:**
   - the agent can't read a data file directly (tool refused);
   - a destructive action (delete a report) never runs without my yes;
   - an invented number in a draft answer is caught by the grounding check;
   - no token or password appears in any transcript or log (grep them).

Report what worked, what didn't, and which harness part needed which Pi mechanism, in `REPORT.md`. Also note any ABI gap
the harness exposed; that becomes input for phase 11 AGENT-HARNESS.

## How to work

- Start with a 10-line plan, then build the smallest end-to-end slice first: MCP/REST connection, plus scenario 1. After
  that, add parts one at a time, testing each.
- Prefer Pi's built-in mechanisms and existing packages over new code; keep each script short with one runnable check.
- Use a local `git init` in `~/Work/Working/abi-pi` and commit as you go. Don't push anywhere.
- If the ABI at :8770 is down, tell me; don't start or stop ABI services yourself.
