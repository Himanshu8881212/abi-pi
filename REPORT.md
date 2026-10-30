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

- [x] ABI-PI is independent: its own engine (`engine/`, a copy of ABI's backend), Postgres and data; one container (3.99 GB, tini as PID 1, user `abi`, health check) holds all of it, the data in one volume. No other ABI is needed.
- [x] Everyone signs in with their own ABI account; the app acts as them (roles, row-level security, reports). `--single` refuses the network.
- [x] Admins (ABI workspace owners) manage people in Govern › Users & access; sections and reports per person are enforced by the server, not just hidden.
- [x] Sessions: HttpOnly, SameSite=Strict, `Secure` behind HTTPS; sign-out and the 12 h expiry end the ABI session too.
- [x] Every request answers (400 for bad input, 500 without details); security headers on every response; no figures in logs.
- [x] Chats, board packs and the team journal are per person or limited to who may see them.
- [x] Import refuses zip bombs and never brings waiting steps; pack file names are safe IDs.
- [x] Backup and restore: `scripts/backup.sh backup|keys|restore` in a running container; a restore into a new volume answers the same rows.
- [x] Tests pass (30 Oct 2026, from a clean engine; the tests remove their own projects): offline 66 (`.venv/bin/python -m pytest -q tests`), live agent 18 of 18, `tests/container_check.sh` 13 of 13, `tests/container_fuzz.py` 17 of 17; all 30 pages draw in Chrome with no script error.
- [ ] Later (owner, 30 Oct 2026): put the app behind an HTTPS reverse proxy (it speaks HTTP; the proxy should send `X-Forwarded-Proto: https`) and run it with `--open` only behind that proxy.
- [ ] Later (owner, 30 Oct 2026): configure the engine's email delivery (`ABI_PUBLIC_URL` and the account mail server) so Users & access invitations and flow emails reach people.
- [ ] Bring yt_cms onto the own engine: its sources (RustFS S3) and about 100M rows need the data loaded again (the old ABI's copy stays in `workspaces.old-abi/`).
- [x] Deeper trim checked (30 Oct 2026), nothing more cut: the engine's UI is where ABI's emailed invite and password-reset links land and what PDF/PPTX render through, and its home is the chat; the chat code is shared with the MCP server Pi uses, ABI's shared agents and flows. The image's weight is all in use (DuckDB, Arrow, ML, Playwright, Chromium).
- [x] Clean start (30 Oct 2026): every engine project deleted and purged; one small project `demo/trending` (real YouTube trending data, 4 files, 560,271 rows: trending, videos, categories, countries) with its 4 relationships found by ABI.
- [x] ABI preview updated on 29 Oct 2026 (owner's approval) from 87018dff to fcc3aa35 with ABI's own `preview.sh` (backup first: Postgres dump and the old UI in `~/abi-preview-backups/`): PPTX (16:9, a slide per page, speaker notes say how each number is calculated) and several pages in one PDF work, also from the app's Export.
- [x] PDF charts no longer cut off: the own engine prints landscape A4 at 0.8 (Release loop #39).
- [x] Remotion's licence (video export): the owner confirmed on 29 Oct 2026 that they qualify for Remotion's free licence (individuals and companies up to 3 people). A larger company needs Remotion's company licence.
- [x] ABI's worker runs with `ABI_SNAPSHOT_RETENTION_DAYS=7` (was 30): the nightly purge (03:17) keeps a week. A restart of the preview by `preview.sh` needs the variable again. (The old ABI; ABI-PI's own engine sets 7 days itself.)

End-to-end, stress and edge cases (from round 14): `tests/test_e2e.py` drives a real app server on the throwaway project `demo/pi_app_test` through the page's own routes: measures, glossary, queries and reports created, queried and removed; exports (PDF, CSV, project zip) and the import check; commit and undo; odd input (unicode, a 5000-character name, nulls, a bad expression, a bad ID); 40 parallel requests from 7 browsers; rapid mode flips; an unknown project in the header; a 65 MB body. Steps ABI confirms are left for a person.

| # | Area | Finding | Fix |
|---|---|---|---|
| 21 | Errors (E2E) | When ABI refused a request's shape (422) the page said "Invalid input. None": the field ABI named was lost. | ABI's field errors are shown as `field: reason` (e.g. `report: Field required`). Test: `test_abis_refusal_names_the_field`. |
| 22 | Import (E2E) | Checking a zip compares it with the project as it is now: uncommitted work showed as changes, and Apply would replace it without saying so. | The check says how many uncommitted changes Apply replaces, and to commit first to keep them. |
| — | Invite | Inviting the owner's email was refused by ABI: account email is off in the preview (needs `ABI_PUBLIC_URL` and `set_account_mail`). Nothing was sent. | Release checklist item: set up ABI's account mail. |

The release is now the ABI-PI container (self-hosted like n8n). Container checks (round 15): missing project, unreachable ABI, no projects folder and no service account each stop with one clear line; a fresh volume starts healthy as user `abi`; a restart keeps the data without attaching again; no secret in the image history.

| # | Area | Finding | Fix |
|---|---|---|---|
| 23 | Settings (SSRF) | A provider's base URL could be a cloud metadata or link-local address: Test connection would send the API key there and read internal answers. | Metadata names, link-local, unspecified and multicast addresses are refused when saving and again when testing; local endpoints (Ollama, LM Studio) stay allowed. Test: `test_a_provider_url_cannot_point_at_metadata_or_nowhere`. |
| 24 | Container | Everything ran as root. | A `abi` user (uid 10001) owns only `/app` and `/data` and runs the app. |
| 25 | Container | No health check: Docker and orchestrators couldn't tell a stuck container. | `HEALTHCHECK` on the app's page (start period 120 s). |
| 26 | Container | An unreachable ABI, an unmounted projects folder or a missing service account failed later with a traceback. | The entrypoint checks each first and says what to set. |
| 27 | Agent team (root cause of every specialist hang) | A specialist that called `abi_show` started an app server and kept listening on its output: that open listener kept the specialist's process alive for ever, so the Lead's subagent call never returned (the 16:14 reviewer waited 29 min; the 12:55 and 13:09 hangs were the same). The 90 s safety timer didn't fire either: its pattern matched only one escaping of `"result": "passed"`. Stray `app.py … 0` servers were the same servers. | Specialists never start an app; a started server is released (listener removed, pipe closed, `unref`, ended with the agent); the timer matches any escaping; the subagent call also returns 5 s after its child exits even if a pipe stays open. The stuck reviewer was ended and the Lead went on to the release step. |
| 28 | Container (agent) | The image's Node.js was Debian's 20; Pi 1.3.0 needs 22 (`enableCompileCache`): the agent crashed at start, so the chat never answered. | Node 22.12 from the official image. Checked: a real chat in the container opens Data Model › Measures and answers in 8 s. |
| 29 | Container (agent) | On a fresh machine Pi loads a project's own extensions only from a trusted folder; nothing was trusted in the image, so the ABI extension and its model provider never loaded ("No API key found for the selected model"). | The image trusts its own app folder (`/home/abi/.pi/agent/trust.json`). |
| 30 | Chat | When the agent couldn't start or crashed, the chat waited for ever: its error output was discarded and the end of its process went unnoticed. | Pi's errors go to `logs/pi-stderr.log`; an agent that ends without an answer gives one ("The agent stopped… check the model settings"). Test: `test_an_agent_that_dies_gives_an_answer_not_silence`. |
| 31 | Chat (Settings) | With an unreachable model picked in Settings, Pi kept retrying and the chat waited 4+ minutes with no sign. (Checked in the container: Settings' model reaches the next chat; restoring it, the chat answered "ok" in 6 s.) | After 2 minutes without a sign from the agent the person gets a notice: press Stop and check Govern › Settings, or keep waiting. Test: `test_a_silent_model_is_reported_after_two_minutes`. |
| 32 | Container (exports) | The first video in a fresh container downloaded Remotion's headless Chrome: on infrastructure without internet the video export failed. (Checked in the container: video 20 s, PPTX deck, both through the app.) | The image fetches it at build (`npx remotion browser ensure`). |
| 33 | ABI down | While ABI was unreachable, every request was dropped without an answer: a connection error is an `OSError`, which the guard treated as "the browser went away". Starting the app with ABI down ended in a traceback. | ABI not answering gives 503 "ABI isn't answering right now"; starting without ABI says "Can't reach ABI at …". Test: `test_abi_down_answers_503_not_a_hung_request`. |
| 34 | Agent (data leak) | The agent's file tools could read `workspaces/_settings/` (the encrypted API keys and the key file that opens them) and `workspaces/*/_views/` (everyone's chats): one person's agent could read another's conversations, and both key files would reach the model provider. | The file-tool guard refuses `_settings`, every `_views` (chats), `_team` (the journal quotes every report) and `_app/…/packs` (rendered under their maker's row-level security) folder and any `secret.key`; the team reads its files through its own tools. Test: `test_the_agents_file_tools_refuse_keys_and_chats`. |
| 35 | Independence | ABI-PI died with ABI: it had no engine of its own, so ABI's database going down (Docker) took sign-in, data and the phone app with it. | `engine/` is ABI-PI's own copy of ABI's backend, schemas and UI (fcc3aa35, committed files only); `scripts/engine.sh` runs it with its own Postgres (socket only) and `data/`; the container holds Postgres, engine and app. |
| 36 | Fuzz | `limit: 1e309` on `/edits/rows` answered 500 (OverflowError); a missing vault document dropped the connection (FileNotFoundError taken for a closed browser). | 400 and 404 with a message. Test: `test_odd_numbers_and_missing_vault_documents_get_answers`. |
| 37 | Container (first start) | Loading data into the attached project failed inside the container: git had no author, so the copy's rebase stopped halfway. Pids from before a restart could keep the engine from starting. | The image sets a git identity for the copies' local commits; the entrypoint clears old pids. |
| 38 | Agent (data leak) | With the engine inside the repo, the agent's file tools and uploads could reach `data/` (Postgres files, the lake, `accounts.txt`) and `engine/`. | Both are refused by the file-tool guard and by uploads. Test: `test_the_agents_file_tools_and_uploads_refuse_the_engines_data`. |
| 39 | Exports (PDF) | Every PDF export cut off the right side of its charts (bar and line alike): the engine draws them for a 1280 px viewport and printed on a portrait Letter sheet (816 px). An ABI gap until now; the engine is ABI-PI's own copy, so fixed there. | `report_snapshot` prints landscape A4 at scale 0.8 (laid out 1309 px wide): one-pagers and board packs show whole charts. Test: `test_report_pdf_is_landscape_a4_so_charts_are_not_cut_off`. |
| 40 | Container (size) | The image carried two headless Chromes (Remotion's and the engine's), two Pythons (pip copies of DuckDB, jsonschema, cryptography next to the engine's venv), the uv binary and the repo's tests, benchmark and demo converters: 4.5 GB. | One Chrome (Remotion renders with the engine's, `REMOTION_BROWSER`), one Python (the engine's venv first on PATH), uv only at build time, dev files ignored: 3.98 GB. Checked in a fresh container: PDF, PPTX and a video render. |
| 41 | Container (agent) | The agent's MCP sign-in header ran `/Users/himanshu/…/scripts/creds.py`: in the container (or any other machine) the path doesn't exist, so the agent couldn't reach the engine's tools. | `.pi/mcp.json` runs `scripts/creds.py` relative to the repo, where every Pi starts. |
| 42 | Agent (end to end) | 13 of 18 live tests failed on the own engine. Root cause: `abi_show` from a terminal Pi started its own app server, and the app switches its project to manual commits on open, so every later change stayed uncommitted in the engine and never reached the folder (the agent had done the work: tools, agents, ML models were in ABI). | `abi_show` only steers the app that started the agent (no second server). Stale tests fixed (the chat test's measure, the Queries tab, specialists' calls checked in ABI); scenarios 2-5 build their own `yt` project from the trending CSV with a keyed targets table. 18 of 18 live tests pass. |
| 43 | Grounding | "every Monday at 8" came back as "at 08:[not verified]": the minutes of a clock time were checked as a figure. | The minutes of an `HH:MM` time are skipped; the hour is still checked. Test: `test_clock_minutes_are_no_figure`. |
| 44 | Container (backup) | A backup in the container never finished: the app is PID 1 and never reaps children, so the stopped worker stayed a zombie that `kill -0` still saw. | `tini` is PID 1 (`ENTRYPOINT ["tini", "--", …]`). |
| 45 | Operations | ABI-PI had no backup: a lost disk lost everything. | `scripts/backup.sh backup | keys | restore`: the engine's database as a dump plus the data folder and workspaces, checksummed; the two key files apart; restore only into an empty volume at the same path (`/data`). |
| 46 | Config as code | The app's own JSON (sections and reports per person in `people.json`, Explore's `analyses.json`) was written in place: JSON, and in the project zip, but with no history, review or undo, unlike the project's files. | Every save commits in `workspaces/<ws>/_app/<project>/`'s own Git (`app_save`). Test: `test_the_apps_own_settings_have_a_git_history`. |
| 47 | Code (ponytail) | About 700 lines that no self-hosted user runs: the passphrase (`ABI_APP_PASS`) mode, the specialists' terminal panel (Pi's interactive TUI; the app runs `--mode rpc`), the subagent's stale README, `bench.py`, "needs a newer ABI" fallbacks, `Hub.refresh`/`cid`, `ground.RETRY`, `abi.py app`, `ABI_BACKEND`/`ABI_SCHEMAS`/`ABI_PI_PYTHON`, the hand-kept `ALLOWED` list (now `READS | WRITES | READS_ONLY`); `duckdb` and `pytest` moved to dev. Two risks closed on the way: a specialist could be swapped for a same-named agent in `~/.pi/agent/agents` (scope "user"), and the model could start a specialist in another folder (outside the file guard and its MCP sign-in). | Specialists are always this project's team, always in the harness folder. Live agent tests rechecked after the cuts. |
| 48 | Container (stress) | 20 requests at once from one page dropped 4 connections in the container (none on the Mac): Python's HTTP server listens with a queue of 5, and Linux resets connections beyond it; nothing was logged. | The app's server listens with a queue of 128. Checked in the container: 200 requests at 20 and at 50 at once, all answered. |
| 49 | Container (size limits) | A 70 MB body sometimes got no answer: the app read 64 MB, answered and closed, and the unread rest reset the line before the answer arrived. | A body over 64 MB is read away (up to 256 MB) and answered 413 "too large" every time. Tests: `test_a_huge_body_is_refused_cleanly` (three times), `tests/container_fuzz.py`. |
| 50 | Chat (model down) | When the model provider refused (429, usage limit; found when the proxy's quota ran out mid-loop), Pi retried and settled with no answer: the chat simply stopped, with nothing said. | The chat answers "The model didn't answer (429: usage limit or too many requests). Try again later, or pick another model or provider in Govern › Settings." (the provider's own text stays in Pi's log). Test: `test_a_model_that_refuses_is_told_not_silent`; checked in Chrome. |
| 51 | Page (deep link) | Opening the app at `…/#chat` (a bookmark, a shared link) threw "Cannot access 'chatBox' before initialization" and drew an empty page: the first page was drawn before the chat's parts further down the script were declared. Every walk-through had started at Home. | The first page is drawn once the whole script has run. Test: `test_a_link_straight_to_a_page_draws_after_the_script`; checked in Chrome at #chat. |
| 52 | Engine (purge) | A deleted project's data was never freed: the app and the worker keep its lake cached, the catalog connection idle in transaction, and the purge's `DROP SCHEMA` waited on that lock for ever (found deleting the 24 test projects; the nightly purge would hang the same way). | `purge_deleted` ends the sessions that hold locks in a lake deleted over 30 days ago (only a stale cache can) and waits at most 30 s. Test: `test_purge_of_deleted_lakes_is_not_blocked_by_a_stale_cached_connection`. |
| 53 | Tests (leftovers) | Every live run left its projects (`sec_<time>`, `s1_<time>`, `yt`, `pi_app_test`) in the engine and under `workspaces/`: 24 had piled up. | A session fixture deletes the projects the run created (test names only) through `delete_project`, and their copies. |
| 54 | Loading (dates) | YouTube's `trending_date` (`yy.dd.mm`, 18.07.02) loaded as the years 2 to 6 when the 1,000 sampled rows all had days up to 12: `%d.%m.%Y` comes first in the formats tried, and DuckDB takes "02" for `%Y`. The agent's forecasts then ran on nonsense dates (it said so). | A format counts only when its dates fall in 1900–2100. Test: `test_a_two_digit_year_date_is_never_read_as_year_2`. |
| 55 | Tests (hidden state) | With #53 the live tests start from fresh projects, and three failed: they had passed only on state earlier runs left behind (an amount measure in `pi_app_test`, a target measure in `yt`). On a fresh project the agent rightly asked to add the missing measure first. | The fixtures set up what a person would have: an order ID (so `amount` gets its SUM, not the key) and a `target` measure on the targets table. |
| 56 | Sessions (layout) | A browser marked as a phone stayed one until the app restarted: a window made narrow once (or a phone and a laptop sharing a profile) kept the phone layout and Viewer on the desktop. | The phone flag follows each request's browser; when a narrow window turns the browser's hub to Viewer, every window of it is told, so the page never shows Developer while the server enforces Viewer. Pages keep their own layout. Test: in `test_modes_are_per_browser_and_a_phone_stays_viewer`. |
| 57 | Agent (new project) | A project the agent built in the chat never opened: the extension looked up the app by project name (only the starting project was known) and the app steered only browsers already on that project, so the agent told the person to run a terminal command. | `abi_show` uses the app that started the agent; for another project the app tells that browser to open it (`/?p=…`, access checked on load); a terminal agent never moves people's pages. Test: `test_the_chat_agent_opens_a_project_it_just_made`. |
| 58 | One-person mode | `--single` served `<project>/app.html` from disk, written at the last sync: changes since (here a new model's tables) stayed invisible until the next sync. | It builds the page from the live, change-invalidated snapshot like sign-in does. Test: `test_one_person_mode_serves_the_live_page_not_a_stale_file`. |
| 59 | Chat | The agent answers with markdown tables; the chat showed raw pipes. | Tables render (cells escaped first, numbers right-aligned). Test: `test_chat_answers_draw_markdown_tables_safely` (runs the page's own function in node). |
| 60 | Agent (medallion) | Layers were optional: the load skill said "medallion or profile-clean", and the quick loader puts files straight into the model, so cleaning could happen outside ABI with no lineage (the hand-built `trending` had no staged tables). | Medallion is the default for every project load (bronze hidden, silver staged, gold model); the quick loader is for a throwaway look. Rebuilt by the agent from one plain chat message: 20 bronze sources, 22 staged tables, a gold fact and three dimensions, 32 measures, a reviewed dashboard. |
| 61 | Pages (regression) | Rewriting the canvas dropped the page script's `KIND` and `SERVER_KEYS`: Agents, Tools, Flows and ML drew blank (no create button) while every test passed, since none ran the page. | Restored; `test_every_page_draws_in_a_real_browser` opens every sidebar page in the engine's headless Chromium and fails on any script error or blank page (it also caught a form-order error and a blank Environments page). |
| 62 | Publish | Publish failed ("Something went wrong"): a commit starts a background sync of the project's copy while Publish syncs it too, and two `git fetch` at once fail ("cannot lock ref"); the live tests' exit-128 sync failures were the same race. | `abi.sync` takes a file lock per copy (across processes). Test: `test_commit_publish_and_viewer_reads_only_the_published_copy` (commit, History, Publish, Viewer reads the copy with the same numbers, never uncommitted work). |
| 63 | Operations | An unexpected error answered "Something went wrong" and left no trace. | The app logs its kind and line (never its message, which may carry values). |
| 64 | Access | A person with Viewer access only saw a one-button mode switch. | The switch is hidden for them. |
| 65 | Chat | A file the agent made (a PDF) was linked as `/api/artifacts/…`, which the app didn't serve; its label ("Download …") was masked as an unverified name. | The app serves ABI's rendered files to the signed-in person (their access checked); a link label opens like a sentence for the grounding check. Test: `test_a_link_label_opens_like_a_sentence`. |
| 66 | Settings | A provider's models were typed by hand. | Fetch models lists them from the provider's API (the typed key, else the stored one, else the built-in connection's key, only for its own address); the key never comes back. Test: `test_models_are_fetched_from_the_provider_with_a_key_that_never_comes_back`. |
| 67 | Mode switch (regression) | Clicking Developer or Viewer did nothing: a `//` comment added mid-line in the page script swallowed the buttons' wiring after it. | A block comment; the real-browser test now clicks both and checks the mode. |
| 68 | Viewer | In Viewer every live request from the page (Explore, Test, filters, monitors) was refused "not available here": the page holds the published copy's data and names `<project>_pub`, the server accepted only the project. | Either name is accepted; the server still answers from the browser's own current project. Test: in `test_commit_publish_and_viewer_reads_only_the_published_copy`. |
| 69 | Tests (lockout) | Each suite run tried a wrong password on the owner, the app's own service account: after ten runs ABI's per-account throttle locked the app out ("Can't reach ABI" at start, which was also the wrong message). | The wrong-password check uses a name nobody has; a refused sign-in at start says so. |
| 70 | Settings (proxy) | A provider saved for the built-in connection's address without a key got a 401 from the agent: Pi was told it had no key, and the card said "No API key". | Such a provider reports its key as the built-in one ("Uses the built-in connection's key"); Pi gets it through `settings.py key`. Checked live: Proxy · gpt-6.1-sol saved in Settings, the agent answered through it. |

Release loop on the independent ABI-PI (29 Oct 2026, findings 35–40): pass 1 found the PDF cut-off (#39) and the image weight (#40); passes 2 and 3 found nothing new on the own engine and the trimmed container (security fuzz without publish or send, Settings, 200 parallel queries over 10 sessions, a forged cookie, a hard kill with the data kept, zip export/import, PDF/PPTX/video inside the container, no secret in logs, files, layers or `docker inspect` beyond what was passed). Loop stopped.

Second release loop (29–30 Oct 2026, findings 48–51) over security, end to end, stress, edge cases, the container and the code: passes 1, 2 and 4 found #48–#51 (pass 4's live tests were blocked for 2.5 h by the model proxy's quota, which surfaced #50); passes 5 and 6 found nothing new with every test passing (offline, live, container, fuzz, Chrome). Loop stopped.

## Design loop (30 Oct 2026)

Every page at desktop (1440 px) and phone (390 px) width, critiqued with the design-critique skill, fixed, rechecked.

- **Theme: Swiss signal** (the owner's pick of three). White or near-black ground, hairline rules, an 8-pt grid, Inter Tight for words, IBM Plex Mono only for formulas, JSON and file paths, one red-orange signal colour for what is active or live (the active page's dot, the Ask button, focus rings). Light and dark, also on the sign-in page and the exported report views. Replaces the beige typewriter look.
- **No redundant headings:** the page name moved into the top bar's path (`demo / trending / Measures`, its explanation as the tooltip); the big title and paragraph on every page are gone, the page head keeps only its buttons (and goes when there are none). Card hints became tooltips; paths and IDs stay.
- **Layout:** cards fill their row with equal heights; a lone form keeps a readable width; Measures and Glossary are aligned rows under one header (Save shows once a row changes); Reports is a list; Home is three bands (ask, needs attention, six area tiles); Explore's controls sit in one row; a report's filter and actions share one bar; nested cards became sections; empty pages show one empty-state panel; chart tools show on hover; KPI numbers scale to their card.
- **Phone:** one-line top bar (menu, page name, Ask), a proper drawer, stacked rows with their labels, and a message instead of a dead end on Settings and Users. The "nothing published" notice shows once per session.

## Several models in one project (30 Oct 2026)

ABI keeps one model per project; ABI-PI's engine now adds **named models** (perspectives): `model/models.json` (schema `models.json`), each a subset of the model's tables and measures for one audience (e.g. Finance, Marketing). Joins and measures stay shared, so a number is the same in every model.

- Actions `save_model` and `delete_model` (destructive: waits for a yes; refused while a report is built in it); `semantic_model {model}` shows only that model and lists `models`; `semantic_query {model}` refuses measures or columns outside it, naming what it has.
- A report can name its `model`; import and save refuse an unknown model or a visual outside it, and `visual_data` checks it again.
- The app: a model switcher (All tables, each model, + New model) on Diagram and Measures; the agent passes `model` when the person names one (checked live: "using the Categories model, which category trended most?" answered inside it).
- Not done: conflicting joins between the same tables (two projects cover that), datasets naming a model.

## Building in the app (30 Oct 2026)

From a plain-user pass over every feature (the agent built, explained, refused what the data can't support, committed):

- **Data models** is a list like Reports (the whole model and each named model); Open draws its canvas: every column shown, tables in columns by join depth (no loops), zoom (−, +, Fit, Ctrl/⌘ + wheel) and Auto arrange.
- **Flows** have a canvas on the same kit: the trigger and each step in columns by what it waits for, conditions on the step, the last run's status per step.
- **Create everywhere:** Staged has New staged table (a source, steps picked from a menu with the source's columns, a preview, or the cleaning in words to the agent); Flows, ML, Tools and Agents open "describe it and the agent builds it" or a template; Monitor has New monitor (a measure crossing a value, a stale table, a failed flow; live status).
- **Home is the person's own:** pins can be moved (← →) and made wide or narrow; "Needs attention" is one summary line with details on demand. Pin on report charts, saved analyses, monitors, tables, flows, ML models, tools and agents; Home shows them live, Open goes to the detail. The Ask box left Home (the chat is in the sidebar).
- **Docs** download as a PDF (A4, printed by the engine's Chromium).
- **Layout on every page:** Layout (Developer, desktop) outlines each card: drag one to move it, drag its corner to resize it (12 columns, 20 px steps); Reset puts a page back. A report keeps it in ABI's report (x, y, w, h, 60 px rows like ABI's editor), so it goes through Commit and Publish; every other page keeps it in the app's `layouts.json` (with Git history): a draft Developer sees, which Publish copies to what Viewer sees (Publish is enabled for a layout-only change and then copies only the layouts). The phone stays one column. Every card on every page is a container: lists (Measures, Glossary, Reports, the model) and loose cards (Docs, Video, History, Vault) too; while a corner is held the card is not draggable, so the browser's own drag can't take the pointer over.
- **+ Add widget on Home:** one picker for everything that can be pinned: report charts, saved analyses, monitor checks and Monitor's own panels (data freshness, model readiness, ML, flows), tables, flows, ML models, tools, agents.
- **Monitors compare against what you choose:** a measure against a value, another measure (e.g. a target) or its previous period (% change on a calendar column); optionally only where a column equals a value, and optionally for each member of a dimension (alerts naming the members that match). Checks can be edited.
- **Content follows the card:** a chart draws itself again for its card's new width and height (text stays the same size; it no longer stretches like a picture). A redraw while resizing no longer snaps the card back to its saved size. Tables and long text scroll inside a card made smaller.
- **Sources are Parquet:** a source is an uploaded .parquet file or an S3 folder of Parquet files (`s3://…/*.parquet`); CSV, Excel, JSON, local paths and databases are refused at every door, with a message that says what to upload instead (it never announces a rule): `abi.call`/`abi.upload` (the app, `abi.py load`), the agent's actions (the extension) and the Sources form (Parquet file picker, no format menu). The load-data skill says so. Sources already loaded stay as they are.
