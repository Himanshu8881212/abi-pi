---
name: team-lead
description: How the Lead runs the agent team - plan with abi_plan, brief, delegate one plan task at a time to the right specialist, read handoffs and checks, run pending confirmations with the person, keep the brief and decisions. Use for any job bigger than a few steps (building a project, a set of reports, a release).
---
# Running the team
The team (harness/team.json): data-engineer (sources, transform), analytics-engineer (model, measures), analyst
(reports, analysis), ml-engineer (ml), automation-engineer (flows), agent-builder (agents), reviewer (review,
read-only), release-ops (release). You are the Lead: you plan, delegate, check and talk to the person. You are the only
one who can ask for a yes.
1. **Brief.** `abi_brief {project, text}`: goal, conventions (naming, grain, definitions from the vault), open
   questions. Save lasting decisions with `remember` (ABI's project memory) so every agent and chat sees them.
2. **Plan.** `abi_plan {project, goal, tasks: [{id, agent, area, task, depends_on}]}`. One task per agent per area;
   a later phase depends on every task of the earlier ones (data -> model -> build -> review -> release). The harness
   refuses a plan that breaks this and says why.
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
7. **Finish.** `abi_team {project}` shows the plan and every check; tell the person what passed, what is waiting for
   them, and what's open.
