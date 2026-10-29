---
name: flows-alerts
description: Automations - scheduled emails and summaries, alerts on data conditions, data-quality alerts, inbox posts. Use for "every Monday", "email me", "alert when".
---
# Flows and alerts
1. `abi_tools ["flows"]`; `delivery_settings` to see if the project can email (else post to the inbox and say so).
2. Recipients: the person's preference `recipients` (abi_prefs) or ask. Time zone: the project's.
3. `save_flow {flow: {id, name, enabled: true, trigger: {type: "schedule", cron: "0 8 * * 1", timezone}, steps: [{id,
   type: "action", action: "send_email", input: {to, subject, body, data: {measures: [...]}, report: {report, page}}}]}}`.
   Numbers ride in `data` (ABI attaches them), never in the text.
4. Enabling and the first send to new recipients need the person's yes: the harness shows ABI's message; wait.
5. `run_flow` once only if the person wants a test send; `flow_runs` for status.
