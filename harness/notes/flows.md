# flows: schedules, alerts, sends
- `save_flow {flow: {id, name, enabled, trigger, steps}}`. Triggers: `{"type":"schedule","cron":"0 8 * * 1","timezone":
  <project zone>}`, data_condition, data_quality, manual. Steps: `{"id","type":"action","action":"send_email"|"post_message",
  "input":{...}}` or `{"type":"refresh","table":...}`.
- Numbers in messages come attached from a query (`"data": {"measures": [...]}`), never typed into the text.
- `send_email` input: to, subject, body, data, optional report {report, page}. The project needs delivery settings
  (`delivery_settings`, `set_delivery`); without them post to the inbox (`post_message channel: inbox`).
- Enabling a trigger, the first send to new recipients and approvals ask the person's confirmation.
- `run_flow` runs now; `flow_runs` shows status. `subscribe` sets a person's own alert.
