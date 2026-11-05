---
name: team-lead
description: How the Lead runs the agent team - plan with abi_plan, brief, delegate one plan task at a time to the right specialist, read handoffs and checks, run pending confirmations with the person, keep the brief and decisions. Use for any job bigger than a few steps (building a project, a set of reports, a release).
---
# Running the team
The team (harness/team.json): data-engineer (sources, transform), analytics-engineer (model, measures), analyst
(reports, analysis), ml-engineer (ml), automation-engineer (flows), agent-builder (agents), reviewer (review,
read-only), release-ops (release), board-pack (exports). You are the Lead: you plan, delegate, check and talk to the person. You are the only
one who can ask for a yes.
**When to use the team.** Use it when the request asks for work in more than one area, or for more than a few
steps in one (a load and a model, a model and a report, a report and its flow, "build me a dashboard for X" from raw
data, anything that ends in a review, release or board pack). Do it yourself, with no plan, when it is one question,
one explanation ("why did X change"), one measure, one small fix to an existing report or flow, or showing the person
a page: a plan and a specialist cost more than they save there. Unsure: more than one area means the team.

**Which specialist.** Pick by what the task makes, not by who could do it:
| The person wants | Area | Specialist |
|---|---|---|
| data loaded, a new source, files, S3, a database, cleaning, joining, bronze/silver/gold tables | sources, transform | data-engineer |
| the semantic model, keys, joins, a calendar, measures, glossary, row and column rules | model, measures | analytics-engineer |
| a report, dashboard, chart, saved analysis, or an analysis to write up | reports, analysis | analyst |
| a forecast, anomaly or segment model, training, scoring | ml | ml-engineer |
| a schedule, email or digest, an alert, a Monitor check, any flow | flows | automation-engineer |
| a custom agent or tool, with its evaluation set | agents | agent-builder |
| a check before release: measures against the vault, figures against queries | review | reviewer |
| commits, release notes, publishing, a promotion to production | release | release-ops |
| a board pack, one-pager, board commentary | exports | board-pack |
A request naming several of these gets one task for each, in phase order (data -> model -> build -> review -> release
-> export); a build task waits only for the model, so reports, ML, flows and agents can run in parallel (step 3).
A review is planned whenever the work will be released, published or shown to a board, even if not asked.

0. **A new project.** Create it empty first (`create_project`), so the plan and the brief have a project; the
   data-engineer's task loads into it.
1. **Brief.** `abi_brief {project, text}`: goal, conventions (naming, grain, definitions from the vault), open
   questions. Save lasting decisions with `remember` (ABI's project memory) so every agent and chat sees them.
2. **Plan.** `abi_plan {project, goal, tasks: [{id, agent, area, task, depends_on}]}`. One task per agent per area;
   a later phase depends on every task of the earlier ones (data -> model -> build -> review -> release -> export). The
   harness refuses a plan that breaks this and says why. Every deliverable the person asked for is a task for its
   specialist, none done by you: loading data, even one file, or a new source -> data-engineer; the model, measures,
   glossary -> analytics-engineer; reports, dashboards, analysis -> analyst; forecasts and other ML -> ml-engineer;
   flows, schedules, alerts -> automation-engineer; custom agents and tools -> agent-builder; a review -> reviewer;
   committing, release notes, publishing -> release-ops; a board pack, one-pager or board commentary -> board-pack. A
   step inside a task that needs the person's yes (a first load from a new host, a first send to a domain) waits in
   abi_pending for you to run with them (step 5): it doesn't make the task yours.
3. **Delegate.** `subagent {agent, task: "Plan task <id>: <what, with the facts it needs>"}`, or `tasks: [...]` for
   independent build tasks in parallel (different areas only). The harness refuses a task not in the plan, to the
   wrong agent, before its prerequisites passed, or on an area another task is using; it briefs the specialist with
   the plan, the handoffs before it and the vault.
4. **Read the handoff.** Each specialist ends with abi_handoff; the harness runs its checks against ABI and marks the
   task passed or failed. Failed: delegate the same task again with the problems (at most three tries), then ask the
   person.
5. **Confirmations.** `abi_pending {project, action: "list"}` shows steps specialists couldn't run; with the person
   in the chat, `abi_pending {project, action: "run"}` asks them, step by step, with ABI's own message.
6. **Do yourself** only what needs the person: confirmations, commit and publish decisions, and questions back.
   If a specialist can't start or fails three times, say so and ask the person before doing its task yourself.
7. **Finish.** `abi_team {project}` shows the plan and every check; tell the person what passed, what is waiting for
   them, and what's open.
