---
name: flows-alerts
description: Automations - scheduled summaries, alerts on data conditions, inbox posts and emails. Use for "every Monday", "email me", "alert when", "notify".
---
# Flows and alerts
1. `abi_tools ["flows"]`; `delivery_settings` shows whether the project can email (else post to the inbox, say so).
2. Recipients: the person's preference `recipients` (abi_prefs) or ask. Time zone: the project's.
3. A weekly summary to the inbox:
   ```json
   {"id": "weekly_summary", "name": "Weekly summary", "enabled": false,
    "trigger": {"type": "schedule", "cron": "0 8 * * 1", "timezone": "UTC"},
    "steps": [{"id": "post", "type": "action", "action": "post_message",
      "input": {"channel": "inbox", "text": "Weekly summary: partner revenue, views and RPM.",
        "data": {"measures": ["partner_revenue", "views", "rpm"], "dimensions": [], "filters": []}}}]}
   ```
   Email instead: `action: "send_email"`, `input: {to, subject, body, data}`. Numbers ride in `data` (ABI attaches
   them), never in the text.
4. An alert: `trigger: {type: "data_condition", query: {measures: [m], dimensions: [...], order_by, limit},
   measure: m, op: "<", value: 50, cooldown_minutes: 1440}`; it runs when the condition starts to hold. A
   month-over-month drop: a change measure (`DIVIDE([m] - PREVIOUS_PERIOD([m]), PREVIOUS_PERIOD([m]))`) queried by
   calendar.month and the dimension, with `op: "<"` and `value: -0.5`.
5. Save with `enabled: false` unless asked; enabling and the first send to new recipients need the person's yes (the
   harness shows ABI's message).
6. `run_flow` only for a test the person wants; `flow_runs` for status.
