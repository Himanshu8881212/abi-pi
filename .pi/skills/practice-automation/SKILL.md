---
name: practice-automation
description: How an automation engineer works in ABI - flows off by default, clear triggers, cooldowns, ownership, a test run, failure handling, recipients. Use with flows-alerts and for any schedule, alert, send or webhook.
---
# Automation practice
- **One job per flow**, named for what it does ("weekly_revenue_summary"), with a description saying who gets what and
  when.
- **Off by default.** Save `enabled: false`; the person turns it on (ABI asks them). Never enable or send to new
  recipients on your own.
- **Triggers.** Schedules in the project's time zone with a cron the person can read back ("Mondays 08:00").
  Alerts on a measure the business defines (vault), with a `cooldown_minutes` so one bad day doesn't post every
  minute, and a threshold that matches the business rule.
- **Numbers in data, not text.** The message names the measures; ABI attaches the figures.
- **Test once.** After saving, `run_flow` once only when the person agrees, then `flow_runs` to read each step's
  status; fix and say what happened.
- **Ownership.** You run as the person and they become the owner; say so for anything that sends outside ABI.
- **Failure path.** A data-quality or failed-refresh alert is part of any production flow; suggest one.
- **Idempotent.** Re-saving a flow replaces it; check `list_flows` before creating a second copy.
