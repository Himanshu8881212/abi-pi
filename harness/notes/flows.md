# flows: schedules, alerts, sends
- `save_flow {flow: {id, name, enabled, trigger, steps}}`. Triggers: `{"type":"schedule","cron":"0 8 * * 1","timezone":
  <project zone>}`, data_condition, data_quality, manual. Steps: `{"id","type":"action","action":"send_email"|"post_message",
  "input":{...}}` or `{"type":"refresh","table":...}`.
- Numbers in messages come attached from a query (`"data": {"measures": [...]}`), never typed into the text.
- `send_email` input: to, subject, body, data, optional report {report, page}. The project needs delivery settings
  (`delivery_settings`, `set_delivery`); without them post to the inbox (`post_message channel: inbox`).
- Enabling a trigger, the first send to new recipients and approvals ask the person's confirmation.
- A webhook trigger's token (`rotate_webhook_token`, after the person's yes) is shown to the person once, in a dialog
  only they read: you never see it; if they didn't copy it, they make a new one. Say plainly where the hook listens:
  the path /api/hooks/<project>/<flow> on the engine's own address (port 8780 on the server), which the address
  people open ABI at does not reach unless their administrator exposes it.
- An alert per object (one per café): add `"per": <object type id>` to a data_condition trigger. Each object whose row
  starts to hold gets its own run with `{{ data.trigger.title }}` and `{{ data.trigger.object.<column> }}`; its inbox
  post and emails reach only project members who can see that object. `"recipients": {"property": <an email or person
  property>, "link"?: <link id>}` sends to the people the object names; use `"to": "{{ data.trigger.recipients }}"` with
  `"when": {"value": "{{ data.trigger.recipients }}", "op": "!=", "to": []}` on that step (without it, a run whose
  people have no email stops there). It runs only steps refresh, agent, snapshot, condition (on a measure) and the
  actions send_email, post_message (channel inbox), refresh, run_agent, report_snapshot, semantic_query, get_object,
  list_objects, visual_data; inside it every API tool and propose_edit are refused, an agent step's calls too.
- `run_flow` runs now; `flow_runs` shows status. `subscribe` sets a person's own alert.
