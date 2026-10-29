# REPORT: ABI on Pi (spike for phase 11 AGENT-HARNESS, D-131)

Pi 1.3.0 + one project extension turns Pi into ABI's agent: GPT-6.1 Sol (through the local CLIProxyAPI), ABI's 160
MCP tools, ABI's rules enforced in the harness, and a folder per project that mirrors ABI's Git repository, with HTML
views. `bin/abi` starts it; `/load <folder> <project>` goes from a folder of files to a Ready model and a report.

## What worked

- **MCP is native in Pi.** `.pi/mcp.json` points at `/api/mcp`; the bearer header is a `!command`
  (`scripts/creds.py`), so the token is fetched at connect time and lives only in process memory (children inherit it
  through the environment). `pi mcp list`: `abi: connected, 160 tools`.
- **Scenario 1** (new CSV → Ready model → top 5 categories) passes live in about 2 minutes: ABI's figures equal a
  hand-written DuckDB query over the same CSV, the answer states them, and the project folder holds exactly ABI's
  committed files (source, table, model, measures), each valid against `schemas/v1`.
- **Drop-in build.** `abi_load` (deterministic, `scripts/abi.py load`) uploads every file through ABI's resumable
  upload, finds a text date column by testing formats through `preview_steps`, builds the model (calendar,
  relationships), describes every table, adds starter measures and an Overview report, and renders views: two Parquet
  files reach Ready in 14 s. With the agent's refinement (`/load`): about 3 minutes.
- **Confirmation bridge.** MCP refuses to hand out confirm tokens ("do it in the app"). The harness catches that,
  repeats the call over REST to get ABI's 409 (message, preview, token), shows the person the message and preview in a
  Pi dialog, and on yes repeats it with `X-Abi-Confirm`. The token never enters the model's context, the transcript or
  the logs. Shown live: `approve_edit` with ABI's "target: '5739000' → '6312900'" preview; `delete_report` with
  "Destructive: …". Access changes (`PERSON_CLICK`) stay in ABI's app.
- **Grounding.** `scripts/ground.py` is ABI's `grounding.py` rules copied verbatim (numbers only from rows, names from
  results, the person's own words, today's date). One retry, then masking, then withholding. It catches the model's own
  arithmetic: in "why did views drop" the model's computed shares (63.1%, 18.2% …) and "India" (rows say "IN") were
  flagged; after the retry hint most answers come back clean, the rest with one masked figure.
- **Agent rules hold** because tool calls arrive as `via="mcp"`: write-back from the agent became a proposal
  (`propose_edit`), production stays view-only, secrets actions aren't offered.
- **Views.** Per project: `index.html` (sources, tables and steps, model diagram, measures with MEL, glossary,
  reports, ML, flows, agents, tools, security, environments, Git history; every card names the JSON file to edit) and
  one self-contained interactive page per report (KPIs, bar/line charts, hover values, sort, table toggle, CSV
  download, "how" with the MEL). Numbers come from ABI's `visual_data` rows; no CDN, opens from disk.
- **Hand edits** go back through ABI's import checks: `scripts/abi.py cli diff|apply|watch <project>` runs ABI's own CLI.

## Benchmark and what it changed (`bench/RESULTS.md`)

| task | first baseline | after optimisation |
|---|---|---|
| ask (top 3 countries) | 14.8 s, 3 turns | 10-12 s, 2 turns |
| report (KPIs + line, sync) | 32 s, 6 turns | 30-34 s, 5 turns |
| why (diagnosis) | 70 s, 6 turns, 2/2 | 74 s, 6-7 turns, grounded after retry (≤ 1 masked figure) |
| load (scenario 1) | — | ~110 s end to end, 1/1 |

Optimisations kept: a **model digest** (measure IDs, dimensions, relationships) in the prompt, so a question needs no
`semantic_model` turn; **ABI's keyword routing** (D-71) preloads the tool groups the person's words point to, with
their notes and playbooks, so a build needs no `read skill` + `abi_tools` turns; "batch independent queries";
deterministic `abi_load` for the slow, error-prone loading steps. Tried and dropped: thinking `low` (no faster,
slightly worse on the diagnosis task).

Bugs found and fixed by the benchmark and the tests:
1. OpenAI rejects tool schemas with regex lookarounds; ABI's `edit_total` has two, so activating the write-back group
   killed the request (silently in print mode). The extension strips lookaround `pattern`s in
   `before_provider_request` (ABI validates inputs anyway).
2. DuckDB's CSV guess split quoted multi-line fields in the YouTube files (8 % broken rows, dates unparseable); loads
   now use explicit quoting first, as the demo build does.
3. Resumed sessions lost the grounding sources (in memory) → false masks; rebuilt from the session branch now.
4. The model answered a question by creating measures in `yt`; questions are read-only now (rule 7, SYSTEM.md).

## What didn't, or only partly

- **Scenario 3 as phrased can't be a proposal on `yt`:** targets are kept per channel, country and month (SAMPLE rows
  for four channels, none in Gaming, none in July). The agent says so and offers valid rows; DEMO.md shows the
  proposal and the approval preview on a valid row.
- **Subagents can't ask the person:** they run as `pi -p` children without a UI, so a confirmation inside one is
  declined and reported back; the main agent then repeats the step. Subagents are slower (a fresh process and model
  context each), so the main agent uses them for long, separable work only.
- **Pi's `tool_search`** is switched on automatically for the deferred MCP server; the model sometimes uses it instead
  of `abi_tools` (it then misses the group notes). Harmless, enforced by the allow-list in subagents.
- Grounding misses ABI's server-side parts (`filtered`, dimension `members`): a member name that *is* in the data but
  not in this chat's rows is flagged; a lower-case member is not checked.
- `yt` has uncommitted changes from the test runs (scenario 2's report `country_category`, scenario 4's
  `gained_category_fc` model/dataset, and four measures: `target_uplift_10pct`, `gained_change`, `gained_per_day`,
  `gained_per_day_mom`) and one pending proposal (+10 % on 12 May 2018 target rows). Review with `changes` /
  `edit_proposals` in ABI and undo/reject what you don't want; the harness never undoes on its own.
- Test projects left in ABI: `pi_smoke`, `pi_test_layout`, `kr_drop`, `kr_full`, `s1_*`, `b_*` (delete in ABI's app).

## Which harness part needed which Pi mechanism

| Part | Pi mechanism |
|---|---|
| System prompt | `.pi/SYSTEM.md` + `systemPromptOptions.sections.abi_context` (stable prefix first, date and time zone last) |
| Rules | `AGENTS.md` context file + `tool_call`/`tool_result`/`message_end`/`agent_before_settle` hooks |
| Memory | ABI's memory over MCP, injected per turn; `abi_prefs` tool (file outside the repo) |
| Tool notes | MCP `exposure: deferred` + `toolExposure` (core direct), `pi.setActiveTools`, `abi_tools` tool, keyword preload |
| Skills | `.pi/skills/*/SKILL.md` (Agent Skills) + playbooks attached to group notes; `.pi/prompts/{load,brief,views}.md` |
| Scripts | custom tools (`pi.registerTool`) over `scripts/abi.py`; plain CLIs for a person |
| Subagents | Pi's official `subagent` example extension (patched 3 lines) + `.pi/agents/*.md` |
| Plugins | built-in MCP client; `pi.registerProvider` (CLIProxyAPI); `before_provider_request`; `ctx.ui.confirm`/`setStatus` |

## Workspace mapping

One ABI instance (:8770) is one ABI workspace. `workspaces/<workspace>/` is only a grouping on disk (a new workspace is
an empty folder); `workspaces/<workspace>/<project>/` is a `git clone` of `~/abi-preview/projects/<project>` (read-only:
`git pull --rebase`, never a write there) plus `.gitkeep` files for the empty layout folders, committed locally.
`workspaces/<workspace>/_views/<project>/` holds the rendered HTML (outside the clone: ABI's import refuses unknown
files). Tenants stay ABI's client isolation; the harness doesn't map them.

## ABI gaps the harness exposed (input for phase 11 AGENT-HARNESS)

1. `create_project` writes only `project.json`; folders appear with their first file. The harness makes the full
   skeleton; ABI should (and keep empty folders).
2. ABI's import refuses any file outside the layout, `.gitkeep` included, so `abi watch <folder>` fails on a skeleton
   folder; the harness sends a bundle without them. Accept (ignore) `.gitkeep`.
3. A table loaded as-is has no `tables/<id>.json` until it gets steps, so the folder doesn't show every model table.
4. MCP has no way to complete a confirmation, and REST runs as `via="rest"` (no agent rules). A harness needs a
   "confirm this MCP call" path: e.g. MCP returns the preview plus an opaque handle that only a person-facing
   endpoint can redeem, so the confirmed step stays an agent call.
5. MCP/REST answers are not grounded; every outside harness has to re-implement AG-2 without `filtered`/`scope`.
   A `grounding_check` action (answer + the call IDs it used) would make outside agents as safe as the chat.
6. `edit_total`'s schema uses regex lookarounds that OpenAI's strict schemas reject; publish lookaround-free patterns
   in `tools()` (MCP) or mark them.
7. There is no bundle download route (`/projects/{p}/export` exports data); the harness depends on reading ABI's Git
   folder on the same machine. A read-only `bundle` action or a Git remote per project would let a harness run
   anywhere.
8. DuckDB's CSV auto-detect silently splits quoted multi-line rows; `add_data` could default to `quote='"',
   escape='"'` or warn when a VARCHAR column's values look shifted.

## Tests

`.venv/bin/python -m pytest tests/` runs the offline tests (grounding rules, layout and schema validation, secret scan);
`ABI_PI_LIVE=1` adds the live scenario and guardrail tests (GPT-6.1 Sol). Last full run (2026-10-29): **16 passed in 4 min 35 s** (4 grounding, 3 layout, 4 guardrails incl. the secret scan over sessions, logs, workspaces and git history, 5 scenarios).
