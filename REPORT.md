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
- **One app per project.** `workspaces/<ws>/<project>/app.html` (written on every sync, git-excluded) holds everything
  behind a sidebar (Overview · Analyze · Model · Data · Automate · Govern). `scripts/abi.py app <project>` serves it on
  127.0.0.1 and it works live as the person: report filters and click-to-cross-filter, measures and glossary edited
  through ABI's checks, flows run with their trace, ML train/score, tools run, agents asked, sources reloaded. ABI's
  confirmations show in a dialog; the confirm token stays in the local server (the page gets an opaque one-time ID).
  An allow-list, a per-start key header and a Host check keep other sites and other actions out; roles, users and
  secrets stay in ABI's app. From disk the file is a snapshot.
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
5. RLS was refused as an "access change" (AGENTS.md wording); ABI lets agents set RLS rules (with a confirmation),
   only roles/users/tenants are a person's click. Rule 4 and the model notes now say so.
6. `sync` crashed after a hand edit (`cli apply`): the edited file blocked the pull of ABI's commit of the same file.
   `sync` now drops a local edit ABI has committed with the same content (JSON compared parsed).

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
- Test projects left in ABI: `pi_smoke`, `pi_test_layout`, `pi_app_test`, `kr_drop`, `kr_full`, `s1_*`, `b_*`, `sec_*`
  (delete in ABI's app).
- Custom agents can't answer on this ABI: no LLM provider is set up in ABI itself ("No model is available"); the app
  shows ABI's message. Tools, flows and ML run.
- Environment files have no action (ABI gap 9): they arrive only through import, i.e. the hand-edit path.

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
9. No action writes `environments/<branch>.json` (`set_environment` only binds a branch); it arrives only through an
   import, so an agent can't add one. An `set_environment_file` action (or a `sources` argument) would close it.
10. With auto-commit on, `abi apply` says "Applied as one uncommitted change" although ABI committed it.

## Tests

`.venv/bin/python -m pytest tests/` runs the offline tests (grounding rules, layout and schema validation, secret scan);
`ABI_PI_LIVE=1` adds the live scenario and guardrail tests (GPT-6.1 Sol). Last full run (2026-10-29): **34 tests**: 17 offline (grounding, layout, secret scan, `test_app.py`: the app's
build and every server guard) and 17 live (5 scenarios, 3 guardrails, `test_sections.py`: the agent builds every
explorer tab in a throwaway project: glossary, dataset, ML, flow, tool + agent, RLS (role change refused), environment
and tracked-file hand edits, every tab renders). All pass; two live tests were fixed on the way (an invented-number
check whose prompt contained the number, which ABI's D-75 rightly grounds; a role check that asserted "not tried"
instead of "not changed").

## Release loop

Each round attacks one area like a reviewer and a pentester, fixes the root cause with a test, and is committed.

| # | Area | Finding | Fix |
|---|---|---|---|
| 1 | Sessions / agent | The chat agent named its browser by the raw id while hubs are keyed `person/browser`: the lookup missed and fell back to every Developer page, so one person's agent could steer others' pages, and the Viewer guard could read another browser's Developer mode (an editor in Viewer could get changes made). | The agent gets its hub's own id (`Hub.hid`); an id the server doesn't know answers `viewer` (fail closed). Test: `test_an_agent_only_reaches_its_own_browser`. |
| 2 | Sessions | Sign-out and the 12 h expiry only forgot the session in the app; the ABI session (token) stayed valid. A session ABI revoked kept the person in a half-working app. | Sign-out and expiry call ABI's `/api/logout`; a 401 from ABI ends the app session (back to sign-in). Test: sign-out makes the token 401. |
| 3 | POST routes | A body that isn't JSON, or a field of the wrong type (`offset: "abc"`), raised inside the handler: the connection dropped with no answer. | Every request runs guarded: malformed input answers 400, anything else 500 without details. Test: `test_bad_input_gets_an_answer_not_a_dropped_connection`. |
| 4 | Confirmations | A pending ABI confirmation could be redeemed from any browser that knew its id (random, so defence in depth). | A confirmation belongs to the hub that asked; another browser gets 410. |
| 5 | XSS | Three chart templates put a measure's name, a visual's title and ABI's error text into `innerHTML` unescaped: anyone with Editor could plant script that runs for every viewer. The signed-in person's JSON in the page didn't escape `</`. | `E()` escapes text for HTML; the person's JSON escapes `</` like the project data. Static test: no `innerHTML` template interpolates without `E()`. |
| 6 | Data leak | Chats lived in one folder per project: every signed-in person saw and could open everyone's chats, whose answers were made under the asker's row-level security. | Chats are per person (`_views/<project>/chats/<name>/`). Test: `test_chats_are_each_persons_own`. |
| 7 | Data leak | Board packs are rendered under their maker's row-level security but were listed and downloadable for anyone with the Export section. An agent chart kept for everyone was named with the agent's free text (could carry the asker's figures). | A pack records who made it; only they, project owners and workspace admins see or download it. Shared agent charts are named by their measures only. |
| 8 | Clickjacking | No anti-framing headers: another site could frame the app and trick a signed-in person into Approve or Publish; the event stream's key could travel in a Referer. | Every response sends `X-Frame-Options: DENY`, `frame-ancestors 'none'`, `nosniff` and `Referrer-Policy: no-referrer`. CSRF was already covered (the page key in a custom header) and DNS rebinding too (Host check alone, the session cookie when signed in). Test: `test_pages_cannot_be_framed_or_sniffed`. |
| 9 | Agent | Vault passages reached the model as plain text, so an instruction planted in an uploaded document read like any other text. (Code already blocked the harm: Viewer refuses every change, destructive steps need the person's click in ABI's dialog, other changes stay uncommitted.) | Vault results are wrapped as `<vault-data>`, marked as data to cite, never instructions. |
| 10 | Import zip | The import read every member whole: a small zip that unpacks to gigabytes (zip bomb) could exhaust memory. (Zip slip was already covered: flat names only, ABI validates the bundle.) | Refused from the zip's listing before any read: at most 5000 files and 256 MB unpacked. Test: `test_a_zip_bomb_is_refused_before_it_is_read`. |
| 11 | Board packs | A pack's file names came from the report, page and view IDs the agent passes, unchecked: `../../x` would write outside the pack folder. (Rendering itself has no shell: arguments are a list.) | IDs in file names must be letters, digits, `_` and `-`; anything else is listed as a failed item. Test: `test_pack_file_names_stay_in_the_pack`. |
| 12 | Edits (correctness) | Edits opened on `calendar`, the date table ABI generates (no rows to edit): an error on first open. | ABI's date table is left out of the editable tables. Checked in the browser: Edits opens on a real table with its rows. |
| 13 | Every page (correctness) | After the app restarted (new page key) or a session ended, an open tab showed "forbidden" in every panel with no hint. | The page notices the refused key or stream once and shows "The app restarted or your session ended" with Reload. Checked in the browser. |

Correctness sweep (round 8): every one of the 32 pages opened in Developer and in Viewer with no script error and no error box other than the two above; phone width had been fixed in the earlier test pass.
| 14 | Section guards | The admin's sections guarded only the app's own routes: ABI actions through `/api/` (e.g. `set_measure` with Data Model closed) and the team steps on Home still ran. | Every change action belongs to a section (`ACTION_SECTION`; `set_table` to Data or Data Model); a closed section refuses it, and a team step is checked in its action's section. Test: every action in `WRITES` is mapped. |
| 15 | Import zip | An import restored the team's `pending.json`: a planted zip could put any step ("delete this source") on Home with Approve, looking like the team's own (ABI's dialog would still ask). | Steps waiting for a yes belong to a live team job; an import never restores them. Test: `test_an_import_never_brings_steps_waiting_for_a_yes`. Logs were checked too: the harness log keeps counts and timings only, no figures or names. |
| 16 | Framing | `DENY` also refused the app framing itself (needed for the phone-width check). | `SAMEORIGIN` / `frame-ancestors 'self'`: other sites still can't frame it. Phone width (396 px) re-checked for the newer pages (Edits, Monitor, Explore, the six Export pages, Users & access, History): nothing scrolls sideways. |
| 17 | Hosting | `--single` (no sign-in, acts as this machine's ABI account) together with `--open` or `--tailnet` gave anyone who could reach the machine owner rights. | That combination is refused at start; other devices always go through sign-in. Test: `test_one_person_mode_is_never_open_to_the_network`. |
| 18 | Data leak | Home loads the team's status, whose journal and brief quote figures from every report: a person the admin limited to some reports still received the others' figures. | A restricted person gets the plan and what waits, not the journal or brief (`team_view`). Test included. |
| 19 | Sessions | Behind an HTTPS reverse proxy (the likely hosting) the session cookie had no `Secure` flag. | `Secure` is added when the proxy says `X-Forwarded-Proto: https`. |
| 20 | Resources | Every browser's chat started an agent process that never stopped (memory and an open ABI session each); a sign-in older than 12 h ended only if its browser came back, so its ABI token stayed valid. | The watch loop (every 30 s) stops chat agents idle for 30 min (the next message restarts them from their session file) and ends expired sign-ins, in ABI too. Test: `test_idle_chat_agents_and_old_sessions_are_swept`. |

### Release checklist

Before hosting on the local infrastructure:

- [x] Everyone signs in with their own ABI account; the app acts as them (roles, row-level security, reports). `--single` refuses the network.
- [x] Admins (ABI workspace owners) manage people in Govern › Users & access; sections and reports per person are enforced by the server, not just hidden.
- [x] Sessions: HttpOnly, SameSite=Strict, `Secure` behind HTTPS; sign-out and the 12 h expiry end the ABI session too.
- [x] Every request answers (400 for bad input, 500 without details); security headers on every response; no figures in logs.
- [x] Chats, board packs and the team journal are per person or limited to who may see them.
- [x] Import refuses zip bombs and never brings waiting steps; pack file names are safe IDs.
- [x] Tests pass (`.venv/bin/python -m pytest -q tests`).
- [ ] Put the app behind an HTTPS reverse proxy (it speaks HTTP; the proxy should send `X-Forwarded-Proto: https`) and run it with `--open` only behind that proxy.
- [ ] Configure ABI's email delivery so Users & access invitations and flow emails reach people.
- [ ] Update the ABI preview to a build with MULTI-EXPORT (PPTX and several pages in one file) and the line-chart clipping fix in its PDF renderer.
- [x] Remotion's licence (video export): the owner confirmed on 29 Oct 2026 that they qualify for Remotion's free licence (individuals and companies up to 3 people). A larger company needs Remotion's company licence.
- [ ] Lower ABI's snapshot retention (`ABI_SNAPSHOT_RETENTION_DAYS`) or watch the disk: each refresh keeps DuckLake snapshots for 30 days.

End-to-end, stress and edge cases (from round 14): `tests/test_e2e.py` drives a real app server on the throwaway project `demo/pi_app_test` through the page's own routes: measures, glossary, queries and reports created, queried and removed; exports (PDF, CSV, project zip) and the import check; commit and undo; odd input (unicode, a 5000-character name, nulls, a bad expression, a bad ID); 40 parallel requests from 7 browsers; rapid mode flips; an unknown project in the header; a 65 MB body. Steps ABI confirms are left for a person.

| # | Area | Finding | Fix |
|---|---|---|---|
| 21 | Errors (E2E) | When ABI refused a request's shape (422) the page said "Invalid input. None": the field ABI named was lost. | ABI's field errors are shown as `field: reason` (e.g. `report: Field required`). Test: `test_abis_refusal_names_the_field`. |
| 22 | Import (E2E) | Checking a zip compares it with the project as it is now: uncommitted work showed as changes, and Apply would replace it without saying so. | The check says how many uncommitted changes Apply replaces, and to commit first to keep them. |
| — | Invite | Inviting the owner's email was refused by ABI: account email is off in the preview (needs `ABI_PUBLIC_URL` and `set_account_mail`). Nothing was sent. | Release checklist item: set up ABI's account mail. |
