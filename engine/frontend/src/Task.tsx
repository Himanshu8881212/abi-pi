import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { agentsQuery, canDesign, confirmable, flowsQuery, measuresQuery, meQuery, post, reportsQuery, tablesQuery, type Condition, type FlowSpec, type Op, type Tokens } from "./api";
import { ZONES } from "./Locale";
import { ReadOnly } from "./Production";
import { build, NEW_TASK, parse, type Attach, type Task } from "./taskFlow";
import { Runs } from "./Flow";
import { actionErrors, Confirm, Errors, type ActionError } from "./mel";
import { t } from "./i18n";
import { focusSoon, useServerChange, useUi } from "./store";

// The task tab ("project/#task/<id>"; an empty id is a new task).
export default function TaskTab({ project, id }: { project: string; id: string }) {
  const flows = useQuery(flowsQuery(project));
  const agents = useQuery(agentsQuery(project));
  const reports = useQuery(reportsQuery(project));
  const tables = useQuery(tablesQuery);
  const channels = useQuery({ queryKey: ["delivery_channels", project], queryFn: () => post<{ channels: string[] }>("delivery_channels", { project }) });
  const me = useQuery(meQuery(project));
  const measures = useQuery(measuresQuery(project));
  const err = flows.error ?? agents.error ?? reports.error ?? tables.error ?? channels.error ?? me.error ?? measures.error;
  if (err) return <p role="alert">{t("task.loadFailed", { project, error: err.message })}</p>;
  if (!flows.data || !agents.data || !reports.data || !tables.data || !channels.data || !me.data || !measures.data)
    return <p role="status">{t("task.loading", { id })}</p>;
  const f = id ? flows.data.flows.find((x) => x.id === id) : undefined;
  if (id && !f) return <p role="alert">{t("task.none", { id, project })}</p>;
  const task = f ? parse(f) : NEW_TASK;
  if (!task) return <p role="alert">{t("task.notSimple", { id })}</p>;
  return <Form project={project} f={f} t={task} tokens={flows.data.tokens[id]} readOnly={!canDesign(me.data)}
    agents={agents.data.filter((a) => a.status === "published").map((a) => [a.id, a.name])} reports={reports.data}
    tables={tables.data.filter((x) => x.startsWith(`${project}/`)).map((x) => x.slice(project.length + 1))} channels={channels.data.channels} measures={measures.data.map((m) => [m.id, m.name])} />;
}

type Props = { project: string; f?: FlowSpec; t: Task; tokens?: Tokens; readOnly: boolean; agents: [string, string][];
  reports: { id: string; name: string; pages: { id: string; name: string }[] }[]; tables: string[]; channels: string[]; measures: [string, string][] };

const OPS: [Op, string][] = [[">", "gt"], [">=", "ge"], ["<", "lt"], ["<=", "le"], ["=", "eq"], ["!=", "ne"]]; // names: task.op.<name>
const newCondition = ([[m] = ["", ""]]: [string, string][]): Condition => ({ query: { measures: [m] }, measure: m, op: ">", value: 0 });
const drop = <T extends object>(o: T, k: string, v: unknown) => { const n: Record<string, unknown> = { ...o, [k]: v }; if (v === undefined) delete n[k]; return n as T; };

// FL-2 data condition and AT-2 "a condition holds": a semantic query, a measure in it, a comparison and a number. It holds
// when any result row satisfies it (the server checks; principle 7). Number, list and JSON inputs are uncontrolled so
// partial typing isn't rewritten; keys the form doesn't show (totals, order_by, limit, more measures) are kept as they are.
function ConditionFields<C extends Condition>({ c, set, measures }: { c: C; set: (c: C) => void; measures: [string, string][] }) {
  const q = c.query;
  return <>
    <label>{t("task.measure")} <select value={c.measure} onChange={(e) => set({ ...c, measure: e.target.value, query: { ...q, measures: [e.target.value] } })} required>
      {[...new Map([[c.measure, c.measure], ...measures])].map(([v, n]) => <option key={v} value={v}>{n}</option>)}</select></label>
    <label>{t("task.comparison")} <select value={c.op} onChange={(e) => set({ ...c, op: e.target.value as Op })}>
      {OPS.map(([v, n]) => <option key={v} value={v}>{t("task.opOption", { name: t(`task.op.${n}`), op: v })}</option>)}</select></label>
    <label>{t("task.value")} <input type="number" step="any" defaultValue={c.value} required
      onChange={(e) => e.target.value !== "" && set({ ...c, value: Number(e.target.value) })} /></label>
    <label>{t("task.groupBy")} <input defaultValue={q.dimensions?.join(", ") ?? ""}
      onChange={(e) => { const d = e.target.value.split(",").map((x) => x.trim()).filter(Boolean); set({ ...c, query: drop(q, "dimensions", d.length ? d : undefined) }); }} /></label>
    <label>{t("task.filters")} <textarea rows={2} defaultValue={q.filters ? JSON.stringify(q.filters) : ""}
      onChange={(e) => {
        const el = e.currentTarget;
        try {
          const f = el.value.trim() ? JSON.parse(el.value) : [];
          if (!Array.isArray(f)) throw new Error();
          el.setCustomValidity("");
          set({ ...c, query: drop(q, "filters", f.length ? f : undefined) });
        } catch {
          el.setCustomValidity(t("task.filtersInvalid"));
        }
      }} /></label>
  </>;
}

export function Form({ project, f, t: initial, tokens, readOnly, agents, reports, tables, channels, measures }: Props) {
  const tab = `${project}/#task/${f?.id ?? ""}`;
  const [task, setT] = useState(initial);
  const [saved, setSaved] = useState(() => JSON.stringify(build(initial)));
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [status, setStatus] = useState("");
  const [confirm, setConfirm] = useState<{ message: string; token: string; action: string; body: object; from: string } | null>(null);
  const qc = useQueryClient();
  const flow = build(task);
  const dirty = JSON.stringify(flow) !== saved;
  useServerChange(initial, (now) => { if (!dirty) { setT(now); setSaved(JSON.stringify(build(now))); } });
  const set = (p: Partial<Task>) => setT((s) => ({ ...s, ...p }));
  const d = task.delivery;
  const tr = task.trigger;
  const pages = reports.find((r) => r.id === task.attach?.report)?.pages ?? [];

  // as in the flow tab: the action, its 409 preview (Confirm re-sends with the token), errors by JSON path, or the result
  const done = async (action: string, body: object, from: string, token?: string) => {
    setErrors([]);
    setStatus("");
    const r = await confirmable(action, { project, ...body }, token);
    if (r.confirm) return setConfirm({ ...r.confirm, action, body, from }); // Confirm takes focus
    if (confirm) focusSoon(from); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) return setErrors(actionErrors(r.status, r.json?.detail));
    qc.invalidateQueries({ queryKey: ["flows", project] });
    qc.invalidateQueries({ queryKey: ["flow_runs", project, task.id] });
    if (action === "test_task")
      return setStatus(t("task.testStarted", { owner: f?.owner ?? "" }));
    if (action === "enable_flow") {
      const enabled = (body as { enabled: boolean }).enabled;
      set({ enabled });
      setSaved(JSON.stringify({ ...flow, enabled }));
      return setStatus(enabled ? t("flow.nowEnabled") : t("flow.nowDisabled"));
    }
    setSaved(JSON.stringify(flow));
    setStatus(t("flow.saved", { id: r.json.id }));
    if (r.json.id !== f?.id) { // a new task (or a new id) gets its own tab
      const { close, open } = useUi.getState();
      close(tab);
      open(`${project}/#task/${r.json.id}`);
      focusSoon(`ws-${project}/#task/${r.json.id}`);
    }
  };

  const asFlow = () => { // AT-3: advanced users open the same file in the flow editor
    const { close, open } = useUi.getState();
    close(tab);
    open(`${project}/#flow/${f!.id}`);
    focusSoon(`ws-${project}/#flow/${f!.id}`);
  };

  return (
    // read-only, the disabled form holds no focusable control, so the scrolling pane itself takes focus (WCAG 2.1.1)
    <div className="measure tool flow" {...(readOnly ? { tabIndex: 0, role: "region", "aria-labelledby": "task-title" } : {})}>
      <form aria-labelledby="task-title" onSubmit={(e) => { e.preventDefault(); done("save_flow", { flow }, "task-save"); }}>
        <h2 id="task-title">{f ? t("task.title", { name: f.name }) : t("task.new")}</h2>
        {f && <p>{t("task.header", { owner: f.owner ?? "", state: task.enabled ? t("flow.enabled") : t("flow.disabled"),
          tokens: tokens ? t("task.tokens", { prompt: tokens.prompt_tokens, completion: tokens.completion_tokens }) : t("task.noTokens") })}</p>}
        {readOnly && <ReadOnly project={project} why={t("task.readOnly", { project })} />}
        <fieldset className="plain" disabled={readOnly}>
          <legend className="sr-only">{t("task.task")}</legend>
          {!f && <label>{t("agent.id")} <input id="task-id" value={task.id} onChange={(e) => set({ id: e.target.value })} required /></label>}
          <label>{t("agent.name")} <input value={task.name} onChange={(e) => set({ name: e.target.value })} required /></label>
          <label>{t("task.agent")} <select value={task.agent} onChange={(e) => set({ agent: e.target.value })} required>
            <option value="">{t("task.chooseAgent")}</option>
            {agents.map(([v, n]) => <option key={v} value={v}>{n}</option>)}</select></label>
          {agents.length === 0 && <p className="muted">{t("task.noAgents", { project })}</p>}
          <label>{t("task.prompt")} <textarea rows={3} value={task.prompt} onChange={(e) => set({ prompt: e.target.value })} required /></label>
          <fieldset>
            <legend>{t("task.trigger")}</legend>
            <label>{t("task.when")} <select value={tr.type} onChange={(e) => {
              const v = e.target.value;
              set({ trigger: v === "schedule" ? { type: "schedule", cron: "0 8 * * 1", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }
                : v === "after_refresh" || v === "writeback" ? { type: v, tables: [] }
                : v === "data_condition" ? { type: v, ...newCondition(measures) } : { type: "manual" } });
            }}>
              <option value="manual">{t("task.when.manual")}</option><option value="schedule">{t("task.when.schedule")}</option><option value="after_refresh">{t("task.when.afterRefresh")}</option>
              <option value="writeback">{t("task.when.writeback")}</option><option value="data_condition">{t("task.when.condition")}</option>
            </select></label>
            {tr.type === "schedule" && <>
              <label>{t("task.cron")} <input value={tr.cron} onChange={(e) => set({ trigger: { ...tr, cron: e.target.value } })} required /></label>
              <label>{t("task.zone")} <input list="task-zones" value={tr.timezone} onChange={(e) => set({ trigger: { ...tr, timezone: e.target.value } })} required /></label>
              <datalist id="task-zones">{ZONES.map((z) => <option key={z} value={z} />)}</datalist>
            </>}
            {tr.type === "data_condition" && <fieldset>
              <legend>{t("task.condition")}</legend>
              <ConditionFields c={tr} set={(c) => set({ trigger: c })} measures={measures} />
              <label>{t("task.cooldown")} <input type="number" min={60} step={1} defaultValue={tr.cooldown_minutes ?? ""}
                onChange={(e) => set({ trigger: drop(tr, "cooldown_minutes", e.target.value === "" ? undefined : Number(e.target.value)) })} /></label>
            </fieldset>}
            {(tr.type === "after_refresh" || tr.type === "writeback") && <fieldset>
              <legend>{tr.type === "writeback" ? t("task.editsOf") : t("task.refreshOf")}</legend>
              {tables.map((x) => <label key={x} className="check"><input type="checkbox" checked={tr.tables.includes(x)}
                onChange={(e) => set({ trigger: { ...tr, tables: e.target.checked ? [...tr.tables, x] : tr.tables.filter((y) => y !== x) } })} />{x}</label>)}
            </fieldset>}
          </fieldset>
          <label>{t("task.refreshFirst")} <select value={task.refresh} onChange={(e) => set({ refresh: e.target.value })}>
            <option value="">{t("task.noRefresh")}</option>{tables.map((x) => <option key={x}>{x}</option>)}</select></label>
          <fieldset>
            <legend>{t("task.onlyIf")}</legend>
            <label>{t("task.sendWhen")} <select value={task.only.kind} onChange={(e) => set({ only: e.target.value === "holds" ? { kind: "holds", ...newCondition(measures) }
              : { kind: e.target.value as "always" | "changed" } })}>
              <option value="always">{t("task.always")}</option><option value="changed">{t("task.changed")}</option>
              <option value="holds">{t("task.holds")}</option></select></label>
            {task.only.kind === "holds" && <ConditionFields c={task.only} set={(only) => set({ only })} measures={measures} />}
            {task.only.kind !== "always" && <p className="muted">{t("task.otherwise")}</p>}
          </fieldset>
          <fieldset>
            <legend>{t("task.delivery")}</legend>
            <label>{t("task.sendTo")} <select value={d.kind} onChange={(e) => set({ delivery: e.target.value === "email" ? { kind: "email", to: "", subject: task.name }
              : e.target.value === "channel" ? { kind: "channel", channel: channels[0] ?? "" } : { kind: "inbox" } })}>
              <option value="inbox">{t("task.inbox")}</option><option value="email">{t("task.email")}</option><option value="channel" disabled={!channels.length}>
                {channels.length ? t("task.channelOption") : t("task.noChannels")}</option>
            </select></label>
            {d.kind === "email" && <>
              <label>{t("task.to")} <input value={d.to} onChange={(e) => set({ delivery: { ...d, to: e.target.value } })} required /></label>
              <label>{t("task.subject")} <input value={d.subject} onChange={(e) => set({ delivery: { ...d, subject: e.target.value } })} required /></label>
            </>}
            {d.kind === "channel" && <label>{t("task.channel")} <select value={d.channel} onChange={(e) => set({ delivery: { kind: "channel", channel: e.target.value } })}>
              {[...new Set([d.channel, ...channels])].map((c) => <option key={c}>{c}</option>)}</select></label>}
            <p className="muted">{t("task.message")}</p>
          </fieldset>
          <fieldset>
            <legend>{t("task.attachment")}</legend>
            <label className="check"><input type="checkbox" checked={!!task.attach} disabled={!reports.length}
              onChange={(e) => set({ attach: e.target.checked ? { report: reports[0].id, page: reports[0].pages[0]?.id ?? "", format: "png" } : null })} />
              {reports.length ? t("task.attach") : t("task.attachNone")}</label>
            {task.attach && <>
              <label>{t("task.report")} <select value={task.attach.report} onChange={(e) => set({ attach: { ...task.attach!, report: e.target.value,
                page: reports.find((r) => r.id === e.target.value)?.pages[0]?.id ?? "" } })}>
                {reports.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}</select></label>
              <label>{t("task.page")} <select value={task.attach.page} onChange={(e) => set({ attach: { ...task.attach!, page: e.target.value } })}>
                {pages.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
              <label>{t("task.format")} <select value={task.attach.format} onChange={(e) => set({ attach: { ...task.attach!, format: e.target.value as Attach["format"] } })}>
                {["png", "pdf"].map((x) => <option key={x} value={x}>{x.toUpperCase()}</option>)}</select></label>
            </>}
          </fieldset>
        </fieldset>
        <Errors errors={errors} />
        {status && <p role="status">{status}</p>}
        {!readOnly && <>
          {dirty && f && <p className="muted">{t("task.unsaved")}</p>}
          <div className="row">
            <button type="submit" id="task-save" className="primary">{t("common.save")}</button>
            {f && <button type="button" id="task-test" disabled={dirty} onClick={() => done("test_task", { flow: f.id }, "task-test")}>{t("task.testRun")}</button>}
            {f && <button type="button" id="task-enable" disabled={dirty} onClick={() => done("enable_flow", { flow: f.id, enabled: !task.enabled }, "task-enable")}>
              {task.enabled ? t("flow.disable") : t("flow.enable")}</button>}
            {f && <button type="button" id="task-flow" disabled={dirty} onClick={asFlow}>{t("task.openAsFlow")}</button>}
          </div>
          {f && <p className="muted">{t("task.testHint")}</p>}
        </>}
        {confirm && (
          <Confirm label={confirm.action === "test_task" ? t("task.confirmTest") : confirm.action === "enable_flow" ? t("flow.confirmEnable") : t("flow.confirmSave")} message={confirm.message}
            onConfirm={() => done(confirm.action, confirm.body, confirm.from, confirm.token)} onCancel={() => setConfirm(null)} back={confirm.from} />
        )}
      </form>
      {f && <Runs project={project} flow={f.id} />}
    </div>
  );
}
