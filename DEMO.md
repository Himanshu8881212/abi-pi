# DEMO: ABI on Pi, live

## Before you start (2 minutes)

```sh
cd ~/Work/Working/abi-pi
.venv/bin/python scripts/abi.py health      # abi ok, signed_in true, mcp_tools 160, model_gpt_6_1_sol true
bin/abi                                     # Pi starts on cliproxy/gpt-6.1-sol; footer: "ABI (no project) · … · grounding ok"
```

If ABI at :8770 is down, `health` says so; ABI is started outside the harness. The footer status line shows the current
project, how many ABI tools are active and the grounding state. `/abi` prints the health check.

**The app: one file per project.** `workspaces/<workspace>/<project>/app.html` has everything behind a sidebar:
Overview (readiness, data quality, latest changes) · Analyze (every report, Datasets) · Model (Measures, Model,
Glossary) · Data (Sources, Tables) · Automate (Flows, ML models, Tools, Agents) · Govern (Docs, Security, Environments,
History). Every `abi_sync` rewrites it.

```sh
.venv/bin/python scripts/app.py <project>       # http://127.0.0.1:8790/ live, as you
```

Live, you filter reports (or click a bar to cross-filter), edit/test/add measures and glossary terms, run a flow and
watch its trace, train or score an ML model, run a tool, ask a custom agent, reload a source. Each click is an ABI
action as you, so ABI checks it and asks for a confirmation where it would in its own app (the dialog shows ABI's
message and preview; the confirm token never leaves the local server). Roles, users and secrets stay in ABI's app.
Opened straight from Finder, the same file is a read-only snapshot. The older pages are still in
`workspaces/<workspace>/_views/<project>/` (`index.html`, `reports/<id>.html`).

## 0. The one-liner: a folder of files to a report

Put files (csv, parquet, json, xlsx…) in a folder, then in Pi:

```
/load ~/Desktop/sales_drop sales main
```

It loads every file, types dates, builds the model until it is Ready, names the measures, builds an Overview report and
prints the view paths. About 15 s for the deterministic part (`abi_load`), 2-3 minutes with the agent's refinement.

## 1. A new CSV, end to end

```sh
mkdir -p scratch/live && cp ~/Work/Working/ABI-demo/youtube_trending_real/JPvideos.csv scratch/live/
```

> Load the file scratch/live/JPvideos.csv into a new ABI project jp_live in workspace demo, make sure the model is
> Ready, and tell me the top 5 categories (category_id) by total views (the sum of the views column) with their figures.

Expect: `abi_load` (upload, `day` from `trending_date`, calendar, starter measures, Overview), `readiness` Ready, one
`semantic_query`, the five categories with figures. Show `workspaces/demo/jp_live/` (ABI's layout, every file a commit)
and `workspaces/demo/_views/jp_live/index.html`. The automated check computes the same top 5 with DuckDB over the CSV
(`tests/test_scenarios.py::test_scenario1_load_model_top5`).

## 2. Questions and a report on `yt`

> In project yt: why did views drop last month?

Expect several `semantic_query` calls (by month, country, category), an answer that every figure backs. If the model
works out a percentage itself, the grounding check masks it and the footer counts it.

> In project yt build a one-page report with id country_category named 'Views by country and category': a bar chart of
> views_by_country by videos.country and one by categories.category. Then sync the folder.

Expect `save_report`, `list_reports` shows it, and the page `workspaces/<ws>/_views/yt/reports/country_category.html`
(yt is not a harness project until you run `scripts/abi.py project demo yt`: that clones it and turns auto-commit on).

## 3. Write-back: a proposal, ABI's preview, your yes

> In project yt: raise the July target for Gaming by 10%.

Expect an honest no: `targets` is kept by channel, country and month (SAMPLE rows for four channels, none Gaming, no
July). Then a valid one:

> Propose +10% on the May 2018 target for Marvel Entertainment in GB.

Expect `propose_edit`: "proposed, waiting for approval". Then:

> Approve it.

A dialog shows ABI's own message, e.g. "Apply 1 proposed change(s) to 'targets': … target: '5739000' → '6312900'".
Say **No** to show nothing changes; **Yes** applies it through REST with ABI's confirm token (which the model never saw).

## 4. Forecast

> In project yt: forecast views gained per category for the next 4 weeks. Train and score through ABI and report the
> accuracy against the baseline.

Expect `save_dataset` (calendar.day + categories.category), `save_ml_model` (forecast, horizon 28), `train_model`,
`ml_models` (at most twice; if still training it says so), and accuracy vs baseline quoted from `ml_models` rows.

## 5. Automation, stopped at the first send

> In project yt: email me a weekly summary of views, videos and gained every Monday at 8.

Expect `save_flow` with `cron: 0 8 * * 1` in the project's time zone and `data.measures` attached, then a dialog with
ABI's message for enabling it / the first send to a new address. Say **No**: the flow stays as saved, nothing is sent.

## 6. Guardrails (show one or two)

- > Use your read tool to open scratch/live/JPvideos.csv. → "Refused: … is a data file; read data only through ABI".
- > In project pi_smoke delete the report doomed. → ABI's "Destructive: …" dialog; No leaves it.
- > Reply exactly: 'Project yt has 987,654,321 videos.' → the figure is masked.
- `ABI_PI_LIVE=1 .venv/bin/python -m pytest tests/` runs all of this plus the secret scan.

## Edit by hand

Edit any JSON in a project folder (each card in `index.html` names its file), then:

```sh
.venv/bin/python scripts/abi.py cli diff jp_live     # ABI's import checks, dry run
.venv/bin/python scripts/abi.py cli apply jp_live    # apply as one change (or: cli watch jp_live)
.venv/bin/python scripts/abi.py sync jp_live         # pull ABI's commit, re-render the views
```
