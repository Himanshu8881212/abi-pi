import type { Condition, FlowSpec, Trigger } from "./api";

// AT-1/AT-3: an agent task is a flow with the task shape (work/validation/T-04.md): an optional leading refresh, one
// agent step, an optional AT-2 "only send if" condition step, one send whose text is that agent's answer. The form
// edits a Task; Save writes build(task) via save_flow.
type Delivery = { kind: "inbox" } | { kind: "email"; to: string; subject: string } | { kind: "channel"; channel: string };
export type Attach = { report: string; page: string; format: "png" | "pdf" };
type Only = { kind: "always" } | { kind: "changed" } | ({ kind: "holds" } & Condition);
export type Task = {
  id: string; name: string; description?: string; enabled: boolean; trigger: Trigger; refresh: string; // "" = no refresh step
  agent: string; prompt: string; only: Only; delivery: Delivery; attach: Attach | null;
  ids: [string, string, string, string]; // step IDs: refresh, agent, send, condition
};
export const NEW_TASK: Task = { id: "", name: "", enabled: false, trigger: { type: "manual" }, refresh: "", agent: "", prompt: "",
  only: { kind: "always" }, delivery: { kind: "inbox" }, attach: null, ids: ["refresh", "ask", "send", "only_if"] };

export function build(t: Task): Omit<FlowSpec, "owner"> {
  const [r, a, s, c] = t.ids;
  const answer = `{{ data.${a}.answer }}`;
  const report = t.attach ? { report: t.attach } : {};
  const d = t.delivery;
  const send = d.kind === "email"
    ? { action: "send_email", input: { to: d.to.split(",").map((x) => x.trim()).filter(Boolean), subject: d.subject, body: answer, ...report } }
    : { action: "post_message", input: { channel: d.kind === "inbox" ? "inbox" : d.channel, text: answer, ...report } };
  const { kind, ...cond } = t.only;
  return {
    id: t.id, name: t.name, ...(t.description ? { description: t.description } : {}), enabled: t.enabled, trigger: t.trigger,
    steps: [...(t.refresh ? [{ id: r, type: "refresh", table: t.refresh }] : []), { id: a, type: "agent", agent: t.agent, prompt: t.prompt },
      ...(kind === "always" ? [] : [{ id: c, type: "condition", ...(kind === "changed" ? { changed: a } : cond) }]),
      { id: s, type: "action", ...send }],
  };
}

const only = (o: object, keys: string[]) => Object.keys(o).every((k) => keys.includes(k));

// A flow as a Task, or null when the form can't show it without losing something (it then opens in the flow editor).
export function parse(f: FlowSpec): Task | null {
  const steps = f.steps as Record<string, any>[];
  const r = steps[0]?.type === "refresh" ? steps[0] : null;
  const rest = steps.slice(r ? 1 : 0);
  const [a, c, s] = rest.length === 3 ? rest : [rest[0], null, rest[1]];
  if (rest.length < 2 || rest.length > 3 || a?.type !== "agent" || s?.type !== "action" || (c && c.type !== "condition")) return null;
  let cond: Only = { kind: "always" };
  if (c && "changed" in c) {
    if (c.changed !== a.id) return null; // "answer changed" of this task's agent only
    cond = { kind: "changed" };
  } else if (c) cond = { kind: "holds", query: c.query, measure: c.measure, op: c.op, value: c.value };
  const answer = `{{ data.${a.id}.answer }}`;
  const i = s.input ?? {};
  const rep = i.report;
  if (rep && !only(rep, ["report", "page", "format"])) return null;
  let delivery: Delivery;
  if (s.action === "send_email" && only(i, ["to", "subject", "body", "report"]) && i.body === answer && Array.isArray(i.to))
    delivery = { kind: "email", to: i.to.join(", "), subject: i.subject ?? "" };
  else if (s.action === "post_message" && only(i, ["channel", "text", "report"]) && i.text === answer && typeof i.channel === "string")
    delivery = i.channel === "inbox" ? { kind: "inbox" } : { kind: "channel", channel: i.channel };
  else return null;
  return {
    id: f.id, name: f.name, ...("description" in f ? { description: f.description as string } : {}), enabled: f.enabled, trigger: f.trigger,
    refresh: r?.table ?? "", agent: a.agent, prompt: a.prompt, only: cond, delivery,
    attach: rep ? { report: rep.report, page: rep.page, format: rep.format ?? "png" } : null,
    ids: [r?.id ?? "refresh", a.id, s.id, c?.id ?? "only_if"],
  };
}
