import Editor from "@monaco-editor/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { jsonDefaults } from "monaco-editor/languages/features/json/register";
import { type ReactNode, useEffect, useState } from "react";
import schema from "../../schemas/v1/flow.json";
import { canDesign, canEdit, confirmable, copyText, flowsQuery, meQuery, flowStatus, post, triggerText, type FlowSpec, type LastRun, type Tokens } from "./api";
import { ReadOnly } from "./Production";
import { actionErrors, Confirm, Errors, type ActionError } from "./mel";
import "./monaco";
import { focusSoon, useServerChange, useUi } from "./store";
import { fmtDate, fmtNumber, t } from "./i18n";

const HIDDEN = ["$schema", "owner"]; // save_flow sets both (FL-6: the saver owns the flow)

// A new flow's starting point: valid against the schema except the empty id and name.
export const NEW: FlowSpec = { id: "", name: "", enabled: false, trigger: { type: "manual" }, steps: [{ id: "note", type: "action", action: "remember", input: { text: "" } }] };

// The editor's text: the flow file without the fields the server sets.
export const editable = (f: object) => JSON.stringify(Object.fromEntries(Object.entries(f).filter(([k]) => !HIDDEN.includes(k))), null, 2);

// schemas/v1/flow.json for the editor: server-set fields aren't required.
export function editorSchema() {
  const s = structuredClone(schema) as Record<string, any>;
  s.required = s.required.filter((k: string) => !HIDDEN.includes(k));
  return s;
}

// DC-6: the definition's email steps and whether each attaches the generated documentation as docs.zip (none while the text isn't valid JSON).
export function emailSteps(text: string): { id: string; docs: boolean }[] {
  try {
    const steps = JSON.parse(text).steps;
    return Array.isArray(steps) ? steps.filter((s) => s?.type === "action" && s.action === "send_email").map((s) => ({ id: s.id, docs: s.input?.docs === true })) : [];
  } catch {
    return [];
  }
}

export function withDocs(text: string, id: string, on: boolean): string {
  const f = JSON.parse(text);
  for (const s of f.steps) if (s.action === "send_email" && s.id === id) s.input = { ...s.input, docs: on || undefined }; // undefined: key dropped
  return JSON.stringify(f, null, 2);
}

type FlowStep = FlowSpec["steps"][number];
type When = { value: string; op: string; to: unknown };
const OPS: string[] = schema.$defs.when.properties.op.enum, POLICIES: string[] = schema.properties.on_failure.enum;

// FLOW-DAG (flows.after): the steps step i waits for: its `after`, else the step listed before it (the first: the trigger).
export const waitsFor = (steps: FlowStep[], i: number): string[] => (steps[i].after as string[] | undefined) ?? (i ? [steps[i - 1].id] : []);

// The graph's columns: a step sits one column right of the furthest step it waits for (column 0 = starts at the trigger).
export function columns(steps: FlowStep[]): Record<string, number> {
  const col: Record<string, number> = {};
  steps.forEach((s, i) => { col[s.id] = Math.max(-1, ...waitsFor(steps, i).map((p) => col[p] ?? 0)) + 1; });
  return col;
}

// The definition with `fn` applied, as editor text (every hand edit goes through the JSON, then save_flow: D-94 parity).
export function edited(text: string, fn: (f: { steps: FlowStep[]; on_failure?: string }) => void): string {
  const f = JSON.parse(text);
  fn(f);
  return JSON.stringify(f, null, 2);
}

// Sets the steps step i waits for, in file order; the default (the step listed before it) drops `after`.
export const setAfter = (steps: FlowStep[], i: number, ids: string[]) => {
  const after = steps.slice(0, i).map((s) => s.id).filter((id) => ids.includes(id));
  if (JSON.stringify(after) === JSON.stringify(i ? [steps[i - 1].id] : [])) delete steps[i].after;
  else steps[i].after = after;
};

// A new step at the end (so no other step's default `after` changes) that waits for `after`: a runnable inbox note
// (internal channel, no first-send confirmation) to change into the real step in the JSON.
export function addStep(steps: FlowStep[], after: string[]): string {
  let n = steps.length + 1;
  while (steps.some((s) => s.id === `step_${n}`)) n++;
  const id = `step_${n}`;
  steps.push({ id, type: "action", action: "post_message", input: { channel: "inbox", text: t("graph.newText", { step: id }) } });
  setAfter(steps, steps.length - 1, after);
  return id;
}

export type Approvers = "owner" | "editor" | string[];
export type Step = { step: string; status: "ok" | "failed" | "waiting" | "rejected" | "skipped"; input?: { message?: string; approvers?: Approvers } | null;
  output: unknown; error: string | null; started: string; finished: string | null };
export type Run = LastRun & { id: string; steps: Step[]; tokens?: Tokens };

const NEEDS = "needs confirmation: ";
// FL-5 trace text: a step's output as short JSON, its duration, and a "needs confirmation" stop told apart from a failure.
export const summary = (o: unknown) => { const s = JSON.stringify(o) ?? ""; return s.length > 200 ? `${s.slice(0, 200)}…` : s; };
const duration = (a: string, b: string | null, status: string) =>
  (b ? t("run.seconds", { s: fmtNumber((Date.parse(b) - Date.parse(a)) / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) })
    : status === "waiting" ? t("run.waiting") : t("run.running"));
export const stepState = (s: Step) =>
  s.status === "ok" ? t("run.ok") : s.status === "skipped" ? t("run.skipped") : s.status === "waiting" ? t("run.stepWaiting", { message: s.input?.message ?? "" }) : s.status === "rejected" ? t("run.rejected")
  : s.error?.startsWith(NEEDS) ? t("run.stepNeeds", { error: s.error.slice(NEEDS.length) }) : t("run.stepFailed", { error: s.error ?? "" });
// D-31: who may approve or reject a waiting run (the server checks it again, plus "not the user who started it")
export const mayDecide = (approvers: Approvers = "owner", role: string | null, user: string | null) =>
  canEdit(role) && (Array.isArray(approvers) ? approvers.includes(user ?? "") : approvers === "editor" || role === "owner");

// UI-2 flow tab ("project/#flow/<id>"; an empty id is a new flow): edit, run now, enable, run history with step traces.
export default function Flow({ project, id }: { project: string; id: string }) {
  const flows = useQuery(flowsQuery(project));
  const me = useQuery(meQuery(project));
  const err = flows.error ?? me.error;
  if (err) return <p role="alert">{t("flow.loadFailed", { project, error: err.message })}</p>;
  if (!flows.data || !me.data) return <p role="status">{t("flow.loading", { id })}</p>;
  const f = id ? flows.data.flows.find((x) => x.id === id) : NEW;
  if (!f) return <p role="alert">{t("flow.none", { id, project })}</p>;
  return <Form project={project} f={f} last={flows.data.last_runs[id]} readOnly={!canDesign(me.data)} />;
}

export function Form({ project, f, last, readOnly }: { project: string; f: FlowSpec; last?: LastRun; readOnly: boolean }) {
  const tab = `${project}/#flow/${f.id}`;
  const [text, setText] = useState(() => editable(f));
  const [saved, setSaved] = useState(text);
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [status, setStatus] = useState("");
  // §5 #3: the server's preview; Confirm re-sends the same call with the token (`from` gets focus back)
  const [confirm, setConfirm] = useState<{ message: string; token: string; action: string; body: object; from: string } | null>(null);
  const qc = useQueryClient();
  const dirty = text !== saved;
  useServerChange(f, (now) => { if (!dirty) { const e = editable(now); setText(e); setSaved(e); } });

  useEffect(() => { // one flow or tool tab is mounted at a time, so this schema applies to every JSON model
    jsonDefaults.setDiagnosticsOptions({ validate: true, enableSchemaRequest: false, schemas: [{ uri: schema.$id, fileMatch: ["*"], schema: editorSchema() }] });
  }, []);

  // every button: the action, then its 409 preview (Confirm re-sends with the token), errors by JSON path, or the result
  const done = async (action: string, body: object, from: string, token?: string) => {
    setErrors([]);
    setStatus("");
    const r = await confirmable(action, { project, ...body }, token);
    if (r.confirm) return setConfirm({ ...r.confirm, action, body, from }); // Confirm takes focus
    if (confirm) focusSoon(from); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) return setErrors(actionErrors(r.status, r.json?.detail));
    qc.invalidateQueries({ queryKey: ["flows", project] });
    qc.invalidateQueries({ queryKey: ["flow_runs", project, f.id] });
    const out = r.json;
    if (action === "run_flow") return setStatus(out.run ? t("flow.started", { run: String(out.run) }) : t("flow.runExists"));
    if (action === "enable_flow") { // Enable needs no unsaved changes, so the text is the file: keep it in step
      const { enabled } = body as { enabled: boolean };
      const next = editable({ ...JSON.parse(text), enabled });
      setText(next);
      setSaved(next);
      return setStatus(enabled ? t("flow.nowEnabled") : t("flow.nowDisabled"));
    }
    setSaved(text);
    setStatus(t("flow.saved", { id: out.id }));
    if (out.id !== f.id) { // a new flow (or a new id) gets its own tab
      const { close, open } = useUi.getState();
      close(tab);
      open(`${project}/#flow/${out.id}`);
      focusSoon(`ws-${project}/#flow/${out.id}`);
    }
  };

  const save = () => {
    try {
      done("save_flow", { flow: JSON.parse(text) }, "flow-save");
    } catch (e) {
      setErrors([{ error: t("flow.badJson", { error: (e as Error).message }) }]);
    }
  };

  return (
    <div className="measure tool flow">
      <form aria-labelledby="flow-title" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <h2 id="flow-title">{f.id ? t("flow.title", { name: f.name }) : t("flow.new")}</h2>
        {f.id && <p>{t("flow.header", { trigger: triggerText(f.trigger), owner: f.owner ?? "" })} · <span className={last?.status === "failed" ? "error" : undefined}>{flowStatus(f, last)}</span></p>}
        {readOnly && <ReadOnly project={project} why={t("flow.readOnly", { project })} />}
        <GraphEditor text={text} setText={setText} readOnly={readOnly} say={setStatus} />
        <span id="flow-json-label">{t("flow.definition")}</span>
        <p className="muted">{t("flow.hint")}</p>
        <div className="mel">
          <Editor height="360px" language="json" value={text} onChange={(v) => setText(v ?? "")} loading={<p role="status">{t("common.loadingEditor")}</p>}
            options={{ readOnly, ariaLabel: t("flow.editorLabel"), tabFocusMode: true, minimap: { enabled: false },
              scrollBeyondLastLine: false, fontSize: 13, automaticLayout: true }} />
        </div>
        {emailSteps(text).map((s) => (
          <label key={s.id} className="check"><input type="checkbox" checked={s.docs} disabled={readOnly}
            onChange={(e) => setText(withDocs(text, s.id, e.target.checked))} />{t("flow.attachDocs", { step: s.id })}</label>
        ))}
        <Errors errors={errors} />
        {status && <p role="status">{status}</p>}
        {!readOnly && (
          <>
            {dirty && f.id && <p className="muted">{t("flow.unsaved")}</p>}
            <div className="row">
              <button type="submit" id="flow-save" className="primary">{t("common.save")}</button>
              {f.id && <button type="button" id="flow-run" disabled={dirty} onClick={() => done("run_flow", { flow: f.id }, "flow-run")}>{t("flow.runNow")}</button>}
              {f.id && <button type="button" id="flow-enable" disabled={dirty} onClick={() => done("enable_flow", { flow: f.id, enabled: !f.enabled }, "flow-enable")}>
                {f.enabled ? t("flow.disable") : t("flow.enable")}</button>}
            </div>
          </>
        )}
        {confirm && (
          <Confirm label={confirm.action === "run_flow" ? t("flow.confirmRun") : confirm.action === "enable_flow" ? t("flow.confirmEnable") : t("flow.confirmSave")} message={confirm.message}
            onConfirm={() => done(confirm.action, confirm.body, confirm.from, confirm.token)} onCancel={() => setConfirm(null)} back={confirm.from} />
        )}
      </form>
      {f.id && f.trigger.type === "webhook" && <Webhook project={project} flow={f.id} readOnly={readOnly} />}
      {f.id && <Runs project={project} flow={f.id} steps={f.steps} />}
    </div>
  );
}

// The definition's steps while the text parses, else null (the JSON editor shows the error).
export function parsed(text: string): { steps: FlowStep[]; on_failure?: string } | null {
  try {
    const f = JSON.parse(text);
    return Array.isArray(f?.steps) && f.steps.every((s: FlowStep) => typeof s?.id === "string") ? f : null;
  } catch {
    return null;
  }
}

// D-120 flow diagram: each box a step (column = how far from the trigger), arrows show what waits on what. `status`
// (a run's steps) colours the boxes; without it a box shows the step type and whether it runs only when.
// ponytail: the SVG scales to the pane width, so very wide graphs get small text; add a scroll box if flows grow that wide.
export function FlowGraph({ steps, status, label }: { steps: FlowStep[]; status?: Record<string, Step>; label: string }) {
  const W = 150, H = 44, DX = 190, DY = 60;
  const col = columns(steps), rows: number[] = [], pos: Record<string, { x: number; y: number }> = {};
  for (const s of steps) {
    const c = col[s.id], r = (rows[c] = (rows[c] ?? -1) + 1);
    pos[s.id] = { x: 4 + c * DX, y: 4 + r * DY };
  }
  const width = 8 + Math.max(...Object.values(col), 0) * DX + W, height = 8 + Math.max(...rows, 0) * DY + H;
  const state = (s: FlowStep) => (status ? (status[s.id] ? t(`run.${status[s.id].status}`) : t("graph.notRun"))
    : s.when ? t("graph.typeWhen", { type: t(`graph.type.${s.type}`) }) : t(`graph.type.${s.type}`));
  const text = steps.map((s, i) => t("graph.item", { step: s.id, state: state(s),
    after: waitsFor(steps, i).join(", ") || t("graph.trigger") })).join("; ");
  const short = (id: string) => (id.length > 18 ? `${id.slice(0, 17)}…` : id);
  return (
    <svg className="flow-graph" viewBox={`0 0 ${width} ${height}`} width={width} role="img" aria-label={t("graph.aria", { what: label, steps: text })}>
      {steps.flatMap((s, i) => waitsFor(steps, i).filter((p) => pos[p]).map((p) => {
        const a = pos[p], b = pos[s.id], x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
        return <path key={`${p}-${s.id}`} className="edge" d={`M${x1} ${y1} C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`} />;
      }))}
      {steps.map((s) => (
        <g key={s.id} className={status?.[s.id]?.status ?? (status ? "none" : undefined)}>
          <rect x={pos[s.id].x} y={pos[s.id].y} width={W} height={H} rx={6} />
          <text x={pos[s.id].x + 10} y={pos[s.id].y + 18} className="id">{short(s.id)}</text>
          <text x={pos[s.id].x + 10} y={pos[s.id].y + 35}>{state(s)}</text>
        </g>
      ))}
    </svg>
  );
}

// An input whose text is kept as typed while its value is read from it (a comma list, JSON-or-text), so typing "a," or
// "1." isn't rewritten under the cursor; an edit of the same value elsewhere (the JSON editor) replaces the text.
function Typed<T>({ value, show, read, onChange, ...rest }: { value: T; show: (v: T) => string; read: (s: string) => T; onChange: (v: T) => void }
  & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  const [raw, setRaw] = useState(show(value));
  useServerChange(value, (now) => { if (show(read(raw)) !== show(now)) setRaw(show(now)); });
  return <input {...rest} value={raw} onChange={(e) => { setRaw(e.target.value); onChange(read(e.target.value)); }} />;
}
// A first `when` value: an agent's verdict, another step's whole output, or the trigger's data (edit the path after).
const placeholder = (steps: FlowStep[], id?: string) =>
  `{{ data.${id ?? "trigger"}${steps.find((s) => s.id === id)?.type === "agent" ? ".verdict" : ""} }}`;
const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
const jsonOrText = (s: string): unknown => { try { return JSON.parse(s); } catch { return s; } };
const showValue = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v) ?? "");

// FLOW-DAG by hand (D-94): per step, the steps it waits for (a join when several), an "only when" route, an agent step's
// choices; add a parallel branch or a step after; the flow's failure policy. Every control rewrites the JSON; Save checks it.
function GraphEditor({ text, setText, readOnly, say }: { text: string; setText: (s: string) => void; readOnly: boolean; say: (s: string) => void }) {
  const f = parsed(text);
  if (!f) return <p className="muted">{t("graph.badJson")}</p>;
  const steps = f.steps;
  const edit = (fn: (g: { steps: FlowStep[]; on_failure?: string }) => void) => setText(edited(text, fn));
  const add = (after: string[]) => edit((g) => say(t("graph.added", { step: addStep(g.steps, after) })));
  return (
    <section aria-labelledby="graph-h" className="graph">
      <h3 id="graph-h">{t("graph.title")}</h3>
      <FlowGraph steps={steps} label={t("graph.label")} />
      <fieldset disabled={readOnly} className="plain">
        <legend className="sr-only">{t("graph.title")}</legend>
        <label>{t("graph.onFailure")} <select id="graph-on-failure" value={f.on_failure ?? "stop"}
          onChange={(e) => edit((g) => { if (e.target.value === "stop") delete g.on_failure; else g.on_failure = e.target.value; })}>
          {POLICIES.map((p) => <option key={p} value={p}>{t(`graph.policy.${p}`)}</option>)}
        </select></label>
        {steps.map((s, i) => {
          const waits = waitsFor(steps, i), w = s.when as When | undefined, id = `step-${i}`;
          const setWhen = (fn: (w: When) => void) => edit((g) => fn(g.steps[i].when as When));
          return (
            <fieldset key={i} className="step">
              <legend>{t("graph.step", { step: s.id, type: t(`graph.type.${s.type}`) })}</legend>
              <div role="group" aria-labelledby={`${id}-after`} className="row">
                <span id={`${id}-after`}>{t("graph.waitsFor")}</span>
                {steps.slice(0, i).map((p) => (
                  <label key={p.id} className="check"><input type="checkbox" id={`${id}-after-${p.id}`} checked={waits.includes(p.id)}
                    onChange={(e) => edit((g) => setAfter(g.steps, i, e.target.checked ? [...waits, p.id] : waits.filter((x) => x !== p.id)))} />{p.id}</label>
                ))}
                {!waits.length && <span className="muted">{t("graph.atTrigger")}</span>}
              </div>
              <label className="check"><input type="checkbox" id={`${id}-when`} checked={!!w}
                onChange={(e) => edit((g) => { if (e.target.checked) g.steps[i].when = { value: placeholder(steps, waits[0]), op: "=", to: "" }; else delete g.steps[i].when; })} />
                {t("graph.onlyWhen")}</label>
              {w && (
                <div className="row">
                  <label>{t("graph.value")} <input id={`${id}-when-value`} value={w.value} size={30} onChange={(e) => setWhen((x) => { x.value = e.target.value; })} /></label>
                  <label>{t("graph.op")} <select id={`${id}-when-op`} value={w.op} onChange={(e) => setWhen((x) => { x.op = e.target.value; })}>
                    {OPS.map((o) => <option key={o} value={o}>{o}</option>)}</select></label>
                  <label>{t("graph.to")} <Typed id={`${id}-when-to`} aria-describedby={`${id}-to-hint`} value={w.to} show={showValue} read={jsonOrText}
                    onChange={(v) => setWhen((x) => { x.to = v; })} /></label>
                  <span id={`${id}-to-hint`} className="muted">{t("graph.toHint")}</span>
                </div>
              )}
              {s.type === "agent" && (
                <label>{t("graph.choices")} <Typed id={`${id}-choices`} value={(s.choices as string[] | undefined) ?? []} show={(v) => v.join(", ")} read={list}
                  onChange={(v) => edit((g) => { if (v.length) g.steps[i].choices = v; else delete g.steps[i].choices; })} /></label>
              )}
              <div className="row">
                <button type="button" id={`${id}-branch`} onClick={() => add(waits)}>{t("graph.addBranch", { step: s.id })}</button>
                <button type="button" id={`${id}-next`} onClick={() => add([s.id])}>{t("graph.addAfter", { step: s.id })}</button>
              </div>
            </fieldset>
          );
        })}
      </fieldset>
    </section>
  );
}

// SC-8: how a caller signs a webhook call, from schemas/v1/flow.json (one source for the server docs and this screen).
export const RECIPE: string = (schema.properties.trigger.oneOf as { properties: { type: { const?: string } }; description?: string }[])
  .find((t) => t.properties.type.const === "webhook")!.description!;

// SC-8 (X-02.7): Rotate token (always a server confirmation) shows the new token once, with a copy button and the recipe.
// The token lives only in this component's state: never in storage, the URL, the query cache or a log; it's gone on unmount.
function Webhook({ project, flow, readOnly }: { project: string; flow: string; readOnly: boolean }) {
  const [confirm, setConfirm] = useState<{ message: string; token: string } | null>(null);
  const [made, setMade] = useState<{ token: string; url: string } | null>(null);
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(null);
  const rotate = async (token?: string) => {
    setNote(null);
    const r = await confirmable("rotate_webhook_token", { project, flow }, token);
    if (r.confirm) return setConfirm(r.confirm); // Confirm takes focus
    setConfirm(null);
    if (!r.ok) {
      focusSoon("hook-rotate");
      return setNote({ error: true, text: r.error });
    }
    setMade(r.json); // the token field takes focus
  };
  const copy = async () => {
    const ok = await copyText(made!.token);
    setNote({ error: !ok, text: ok ? t("hook.copied") : t("hook.copyFailed") });
  };
  return (
    <section aria-labelledby="hook-h">
      <h3 id="hook-h">{t("hook.title")}</h3>
      <p className="muted">{RECIPE}</p>
      {readOnly && <ReadOnly id="hook-why" project={project} why={t("hook.readOnly", { project })} />}
      <button type="button" id="hook-rotate" disabled={readOnly} aria-describedby={readOnly ? "hook-why" : undefined} onClick={() => rotate()}>{t("hook.rotate")}</button>
      {confirm && (
        <Confirm label={t("hook.confirm")} message={confirm.message} onConfirm={() => rotate(confirm.token)}
          onCancel={() => setConfirm(null)} back="hook-rotate" />
      )}
      {made && (
        <div className="row">
          <label>{t("hook.newToken")} <input id="hook-token" key={made.token} autoFocus readOnly value={made.token} size={48} autoComplete="off" spellCheck={false} onFocus={(e) => e.target.select()} /></label>
          <button type="button" onClick={copy}>{t("hook.copy")}</button>
          <span>{t("hook.endpoint", { url: location.origin + made.url })}</span>
        </div>
      )}
      {note && <p role={note.error ? "alert" : "status"} className={note.error ? "error" : undefined}>{note.text}</p>}
    </section>
  );
}

type Preview = { flow: string; message: string; remaining: string[] };

// FL-5 run history: each run's steps with status, output summary, error and timing. Polls while a run is queued, running or
// waiting. FL-1: a waiting run has Approve (server preview, Confirm/Cancel, §5 #3) and Reject for its approvers (D-31).
export function Runs({ project, flow, steps }: { project: string; flow: string; steps?: FlowStep[] }) {
  const q = useQuery({
    queryKey: ["flow_runs", project, flow],
    queryFn: () => post<{ runs: Run[] }>("flow_runs", { project, flow }),
    refetchInterval: (q) => (q.state.data?.runs.some((r) => ["queued", "running", "waiting"].includes(r.status)) ? 2000 : false),
  });
  const me = useQuery(meQuery(project));
  const user = useUi((s) => s.user);
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<{ message: string; token: string; preview: Preview; run: string; step?: string; from: string } | null>(null);
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(null);
  const latest = q.data?.runs[0]?.status;
  useEffect(() => { // the header and the Data tab read the latest run from list_flows: refresh it when the polled run changes state
    if (latest) qc.invalidateQueries({ queryKey: ["flows", project] });
  }, [latest, project, qc]);

  // FLOW-DAG.2-fix-1: with approvals waiting in parallel branches, `step` names the one decided
  const decide = async (action: "approve_flow_run" | "reject_flow_run", run: string, step: string | undefined, from: string, token?: string) => {
    setNote(null);
    const r = await confirmable(action, { project, run, step }, token);
    if (r.confirm) return setConfirm({ ...r.confirm, run, step, from }); // Confirm takes focus
    setConfirm(null);
    if (!r.ok) {
      focusSoon(from);
      return setNote({ error: true, text: r.error });
    }
    setNote({ error: false, text: action === "approve_flow_run" ? t("run.approved") : t("run.rejectedNote") });
    focusSoon("flow-runs-h"); // WCAG 2.4.3: the run's buttons go away once it isn't waiting
    qc.invalidateQueries({ queryKey: ["flow_runs", project, flow] });
    qc.invalidateQueries({ queryKey: ["flows", project] });
  };

  const buttons = (r: Run) => {
    if (r.status !== "waiting") return null;
    const waits = r.steps.filter((x) => x.status === "waiting"), one = waits.length === 1;
    return waits.filter((s) => mayDecide(s.input?.approvers, me.data?.role ?? null, user)).map((s) => {
      const step = one ? undefined : s.step, a = one ? `approve-${r.id}` : `approve-${r.id}-${s.step}`, j = one ? `reject-${r.id}` : `reject-${r.id}-${s.step}`;
      const c = confirm?.run === r.id && confirm.step === step ? confirm : null;
      return (
        <div key={s.step}>
          <div className="row">
            {!one && <span>{t("run.atStep", { step: s.step })}</span>}
            <button type="button" id={a} onClick={() => decide("approve_flow_run", r.id, step, a)}>{t("inbox.approve")}</button>
            <button type="button" id={j} onClick={() => decide("reject_flow_run", r.id, step, j)}>{t("inbox.reject")}</button>
          </div>
          {c && (
            <Confirm label={t("run.confirmApproval")} message={c.message} onConfirm={() => decide("approve_flow_run", r.id, step, a, c.token)}
              onCancel={() => setConfirm(null)} back={a}>
              {!!c.preview?.remaining.length && <p>{t("run.thenRuns", { steps: c.preview.remaining.join(", ") })}</p>}
            </Confirm>
          )}
        </div>
      );
    });
  };

  return (
    <section aria-labelledby="flow-runs-h">
      <h3 id="flow-runs-h" tabIndex={-1}>{t("run.history")}</h3>
      {q.error && <p role="alert">{q.error.message}</p>}
      {note && <p role={note.error ? "alert" : "status"} className={note.error ? "error" : undefined}>{note.text}</p>}
      {q.data?.runs.length === 0 && <p className="muted">{t("run.none")}</p>}
      <RunList runs={q.data?.runs ?? []} actions={buttons} steps={steps} />
    </section>
  );
}

// `steps` (the flow's definition): each run also shows as the graph, with its skipped and not-run steps.
export function RunList({ runs, actions, steps }: { runs: Run[]; actions?: (r: Run) => ReactNode; steps?: FlowStep[] }) {
  return (
    <ol className="results">{runs.map((r) => (
      <li key={r.id}>
        <strong>{`${fmtDate(r.created)}: ${t(`run.${r.status}`)}`}</strong> <span className="muted">{duration(r.created, r.finished, r.status)}</span>
        {!!r.tokens?.prompt_tokens && <span className="muted">{t("run.tokens", { prompt: r.tokens.prompt_tokens, completion: r.tokens.completion_tokens })}</span>}
        {r.error && <p className={r.status === "failed" ? "error" : undefined}>{r.error}</p>}
        {actions?.(r)}
        {steps && <FlowGraph steps={steps} status={Object.fromEntries(r.steps.map((s) => [s.step, s]))} label={t("graph.runLabel", { run: r.id })} />}
        {r.steps.length > 0 && (
          <table>
            <caption className="sr-only">{t("run.steps", { run: r.id })}</caption>
            <thead><tr><th scope="col">{t("run.step")}</th><th scope="col">{t("run.status")}</th><th scope="col">{t("run.output")}</th><th scope="col">{t("run.time")}</th></tr></thead>
            <tbody>{r.steps.map((s) => (
              <tr key={s.step}>
                <td>{s.step}</td>
                <td className={s.status === "failed" ? "error" : undefined}>{stepState(s)}</td>
                <td><code>{s.output != null && s.status !== "failed" ? summary(s.output) : ""}</code></td>
                <td>{duration(s.started, s.finished, s.status)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </li>
    ))}</ol>
  );
}
