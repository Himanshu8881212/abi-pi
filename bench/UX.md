# The 1.2.5 UX bench

One brief for every agent of the 1.2.5 UI/UX overhaul: how to get an install of your branch, fill it, take
screenshots, and how pages and journeys are scored. Everything runs on pve1 (LXC 100); the Mac only runs these
scripts, `git`, and static checks (`python3 -m py_compile`, `sh -n`). No pytest, docker, engine or browser on the Mac.

**Keep the theme.** Palette: paper #f7f7f5, sheet #fff, ink #0b0b0b, mute #62625e, accent #ff4f1f, and the light and
dark themes. This is a refinement, not a rebrand: more modern and premium as a tool.

## The installs

| install | ref | port (on the LXC) | what for |
|---|---|---|---|
| `abi-ux-base` | v1.2.0 | 8820 | the baseline everyone scores first; leave it running |
| `abi-ux-<you>` | your branch | your own (8821, 8822, 8823) | your work |

Each install is container `abi-ux-NAME`, image `abi-ux:NAME`, volume `abi-ux-NAME-data`, files in `/opt/abi-ux-NAME`.
Its secrets are root-only files on the LXC: `/root/abi-ux-NAME.env` (the owner's random password) and
`/root/abi-ux-NAME-accounts` (the seed's accounts). Never print, copy or commit them. Never touch the live `abi` and
`lake` containers, their volumes, `/opt/abi-pi` or `/opt/abi-fresh`.

```sh
bench/ux_install.sh release-1.2.5-a a 8821          # new install of a branch: build, start, seed, model
bench/ux_install.sh --redeploy release-1.2.5-a a    # your new commits: rebuild and restart, the volume (data) stays
bench/ux_shots.sh a OUT --tour                      # every page and dialog, owner and viewer, 1440 and 390 px
bench/ux_shots.sh a OUT steps.json --role viewer --width 390   # your own steps
```

`ux_install.sh` archives the ref (committed work only: commit before you redeploy), builds the image on the LXC,
starts the container on `127.0.0.1:PORT`, runs the seed and copies the Mac's model provider (`proxy`, default
`proxy/claude-sonnet-5-5`, fallback `proxy/claude-opus-5-5`) so the chat answers. The harness scripts
(`bench/ux_seed.py`, `bench/ux_shot.py`) are always the Mac checkout's, copied into the container. OUT: use the
scratchpad, e.g. `/private/tmp/claude-501/-Users-himanshu-Work-Working-abi-pi/9e38f0b0-492b-4261-87b3-034a57be3841/scratchpad/ux/<you>/`.

## The seed (`bench/ux_seed.py`)

Run by `ux_install.sh` inside the container as the owner; idempotent (a redeploy runs it again and keeps what exists).
Project `brewline` (Brewline Coffee, `bench/brewline.py` at scale 0.01: 120 cafés, 12 products, about 74k orders
2023-2025, the North region's orders halve in December 2025), loaded as `bench/perf.py project` does:

- tables cafes, customers, products, orders, the model, starter measures (revenue, orders_rows, quantity, ... and the
  `_prev`/`_change` ones) and the **Overview** report;
- report **Showcase**: page 1 one visual of every type the app draws (text, kpi, slicer, bar, stacked bar by region and
  category, line by year and month, combo, pie, scatter, map by customers' home country, waterfall, funnel, treemap,
  table, matrix), page 2 heatmap and Sankey (the app shows these as tables; the PDF draws them);
- glossary terms Revenue, Order, Region; dataset Revenue by month; ML **Revenue forecast** (trained);
- flow **Monday refresh and PDF** (a schedule, saved switched off);
- Monitor checks "Revenue is flowing" (holds: its alert flow posts to the **Inbox** within a minute) and "Monthly
  revenue falls 20%"; a saved Explore analysis "Revenue by region"; vault document `brewline-kpis.md`;
- accounts `owner` (workspace owner), `editor1` (Editor), `viewer1` (Viewer, row-level rule `viewer_region`: the
  South region only); one commit, then **Publish** (Viewer reads the published copy);
- Home widgets (pins) for the owner and viewer1: Revenue, Orders, Revenue per month, Revenue by region and category.

## Screenshots (`bench/ux_shot.py`)

Signs in through `/signin` as the role and drives the engine's headless Chromium. Names are
`<width>-<role>-<page>[-<state>].png`; `steps.jsonl` has one line per step: ok or its error, the console and page
errors it caused, its milliseconds. A steps file is a JSON list:

```json
[{"goto": "rep:showcase"}, {"click": "button.fadd"}, {"text": "Region"}, {"wait": 800},
 {"type": "input.f", "value": "South"}, {"key": "Escape"}, {"shot": "showcase-filtered"}, {"close": true}]
```

`upload` sets a file input to a file in the container (`{"upload": "input[type=file]", "file": "/tmp/bw/brewline.csv"}`;
make one with `docker exec abi-ux-NAME python3 bench/brewline.py /tmp/bw --csv` on the LXC). `goto` takes a page key (below) or a `/path`; `shot` is full page unless `"viewport": true`. At 390 px the context
is a phone (touch, 2x); the app keeps a phone in Viewer mode.

**The tour** (`--tour`) opens every page the role's sidebar has, then the reports, models and flows, then the dialogs:

- Start: `home`, `chat`
- Data: `sources`, `staged`, `tables`, `edits`
- Data Model: `model`, `measures`, `glossary`, `datasets` (Queries)
- Visualizations: `reports`, `monitor`, `inbox`, `explore`
- Export: `boardpack`, `documents`, `onepager`, `dataexport`, `video`, `transfer` (Project)
- Automate: `flows`, `ml`, `tools`, `agents`
- Knowledge: `vault`, `docs`
- Govern: `security`, `environments`, `history`, `people` (Users & access), `settings` (owners)
- and `rep:overview`, `rep:showcase`, `mdl:all`, `flw:<each flow>`
- dialogs: `home-account` (your name: password), `home-tokens` (personal tokens), `home-two-step`,
  `home-switcher` (workspace/project), `home-chat-drawer`, `home-changes` (commit; not for viewer),
  `rep-overview-filter-picker`, `sources-add-source`, `people-add-person`, `home-menu` (390 px only)

A Viewer sees fewer pages (no Govern owner pages, the published copy). Tools and Agents are empty by design of the
seed: they show the empty state a new project shows, which is part of what you score.

## Scoring

Score every page and dialog at both widths, as owner and as viewer, 1-10 in each category, then an Overall. The
target is **at least 9.5** everywhere.

1. **Typography**: type scale, weights, line length, numerals in tables and KPIs.
2. **Hierarchy**: what matters first; one primary action per view.
3. **Spacing & density**: the 8-pt rhythm, alignment, density right for a data tool.
4. **Color & contrast**: the palette above, contrast (WCAG AA), the accent only for what is active or live.
5. **Iconography**: consistent, meaningful, aligned; none where a word is clearer.
6. **Consistency & polish**: the same component looks and behaves the same everywhere; motion counts here.
7. **Clarity (incl. microcopy)**: labels, empty states, errors and help say what to do next, in plain words.
8. **Data visualization**: chart choice, axes, labels, legends, tooltips, number formats, light and dark.
9. **Interaction & feedback (incl. perceived speed)**: hover, focus, pressed, loading, success, failure, latency.
10. **Navigation & wayfinding**: where am I, how do I get there and back.
11. **Responsiveness**: desktop down to 390 px, no overflow, no lost actions.
12. **Accessibility**: keyboard, visible focus, labels and names, tap targets (44 px on a phone).

**The journeys.** For each: the clicks or steps, the dead ends, the errors, and a score (target 9.5). Script each one
as a steps file so the result can be shot again after your change.

1. First run: sign in, connect data, first report.
2. Upload a CSV until it is Ready.
3. Build a model, a measure, then a report with filters.
4. Ask "why did revenue change" as a Viewer and get to the chart.
5. Open a report on a phone, filter it, export a PDF.
6. Set an alert and see it in the Inbox.
7. Invite a person, give a role, restrict rows, turn on two-step sign-in.
8. Schedule a report email or a flow.
9. Publish and share a report.
10. Recover from mistakes: a wrong upload, undo, an error message.

**Rules for changes.** Visual changes and small flow fixes go in directly. A change that alters what a feature does
goes on a list for the owner's OK. CI stays green; the security rules hold (the chat's markdown stays escaped, no
secret in a page, a log or a commit).

## What the baseline tour found (v1.2.0, `abi-ux-base`, 155 shots)

Every page draws real Brewline content as the owner at 1440 px. steps.jsonl flags these:

- **Viewer, both widths:** Home, Sources and Monitor throw while drawing (`(r[ix(...)] || []).map is not a function`,
  `p.join is not a function`). Viewer's Home shows its widgets' titles but no numbers or charts.
- **Owner at 390 px:** a phone is held in Viewer mode, so Users & access and Settings answer 403, the flow pages fall
  back to Home, and the switcher, Changes, Add source and Add a person are not reachable.
- Seen in the shots: years printed as `2,023` in tables, the Overview's starter KPIs (a summed Unit Price, "Orders
  rows"), a daily line too dense to read, and a full-page shot's sidebar stops at the first screen height.
