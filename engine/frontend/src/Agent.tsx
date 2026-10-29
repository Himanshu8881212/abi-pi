import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { agentsQuery, aliasesQuery, canDesign, canShare, confirmable, meQuery, modelQuery, post, readinessQuery, sharedQuery, toolsQuery, toolStatus, type AgentSpec, type Channel, type EvalCase } from "./api";
import { ReadOnly } from "./Production";
import { SharingDialog } from "./Sharing";
import { actionErrors, Confirm, Errors, type ActionError } from "./mel";
import type { SemanticModel } from "./Properties";
import { focusSoon, useServerChange, useUi } from "./store";
import { fmtDate, t } from "./i18n";
import { ModelEffort, type Choice } from "./Effort";

const AGENT_TOOLS = ["semantic_query", "report_snapshot", "send_email", "post_message", "refresh", "propose_edit"]; // D-3 + TL-2 reads
const CHANNELS: Channel["type"][] = ["in_app", "email", "slack", "teams", "api", "mcp"];
const SCOPES = ["tables", "measures", "datasets", "glossary"] as const;
type Model = SemanticModel & { tables: Record<string, unknown>; datasets: { id: string; name: string }[]; glossary: { id: string; term: string }[] };
type Row = { question: string; contains: string; tools: string }; // an eval case as typed: comma-separated values
// AGENT-AS-TOOL: an ask_agent call carries the asked agent's own run (its trace nests the same way)
type Nested = { id: string; agent: string; trace: Step[]; prompt_tokens: number; completion_tokens: number };
type Step = { tool: string; input: unknown; output: unknown; run?: Nested };
export type Run = { id: string; at: string; question: string; answer: string; grounded: boolean | null; retried: boolean; template: boolean;
  trace: Step[]; via: string; prompt_tokens: number; completion_tokens: number };
type Evals = { passed: boolean; cases: { question: string; passed: boolean; missing: string[]; answer: string }[] };

export const NEW: AgentSpec = { id: "", name: "", purpose: "", instructions: "", scope: {}, tools: [], mode: "tools_only", grounding: true, channels: [{ type: "in_app" }] };
const external = (cs: Channel[]) => cs.some((c) => c.type !== "in_app"); // §7.2: grounding required
// "Bikes, 30" → ["Bikes", 30]; numbers match numerically (AG-4 number normalization).
// ponytail: a comma always separates values, so an expected text can't contain one; add a list editor if one ever must.
export const values = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean).map((x) => (Number.isFinite(Number(x)) ? Number(x) : x));
export const rows = (cases: EvalCase[] = []): Row[] =>
  cases.map((c) => ({ question: c.question, contains: c.expect.contains.join(", "), tools: (c.expect.tools ?? []).join(", ") }));

// save_agent's payload: the fields a user sets (save_agent sets $schema, version, status, owner, sharing, evaluation).
export function payload(a: AgentSpec, cases: Row[]) {
  const { id, name, purpose, instructions, scope, tools, mode, channels } = a;
  return {
    agent: { id, name, purpose, instructions, scope, tools, mode, model: a.model ?? "auto", ...(a.effort && { effort: a.effort }), ...(a.agents?.length && { agents: a.agents }), grounding: a.grounding || external(channels),
      channels: channels.map((c) => Object.fromEntries(Object.entries(c).filter(([, v]) => v)) as Channel) },
    cases: cases.map((r) => {
      const t = values(r.tools).map(String);
      return { question: r.question, expect: { contains: values(r.contains), ...(t.length ? { tools: t } : {}) } };
    }),
  };
}

// AG-2/AG-3 grounding result of one run.
export const verdict = (r: Pick<Run, "grounded" | "retried" | "template">) =>
  r.grounded === null ? t("agent.groundingOff")
  : (r.grounded ? t("agent.grounded") : t("agent.notGrounded")) + (r.retried ? t("agent.retried") : "") + (r.template ? t("agent.template") : "");
const short = (o: unknown) => { const s = JSON.stringify(o) ?? ""; return s.length > 300 ? `${s.slice(0, 300)}…` : s; };

// AG-1 Agent Studio tab ("project/#agent/<id>"; an empty id is a new agent): define, evaluate, test, publish.
export default function Agent({ project, id }: { project: string; id: string }) {
  const me = useQuery(meQuery(project));
  if (me.error) return <p role="alert">{t("agent.loadFailed", { project, error: me.error.message })}</p>;
  if (!me.data) return <p role="status">{t("agent.loading", { id })}</p>;
  return me.data.role === null ? <Shared project={project} id={id} /> : <Studio project={project} id={id} />;
}

// SH-1 recipient (no role here): ask the shared agent and see its own runs, nothing of the studio (D-51 view-only).
function Shared({ project, id }: { project: string; id: string }) {
  const name = useQuery(sharedQuery).data?.find((s) => s.project === project && s.kind === "agent" && s.id === id)?.name ?? id;
  return (
    <div className="measure tool agent">
      <h2 id="agent-title">{t("agent.title", { name })}</h2>
      <p className="muted">{t("agent.sharedNote")}</p>
      <History project={project} agent={id} />
    </div>
  );
}

function Studio({ project, id }: { project: string; id: string }) {
  const agents = useQuery(agentsQuery(project));
  const model = useQuery(modelQuery<Model>(project));
  const tools = useQuery(toolsQuery(project));
  const ready = useQuery(readinessQuery(project));
  const me = useQuery(meQuery(project));
  const aliases = useQuery(aliasesQuery);
  const err = agents.error ?? model.error ?? tools.error ?? ready.error ?? me.error ?? aliases.error;
  if (err) return <p role="alert">{t("agent.loadFailed", { project, error: err.message })}</p>;
  if (!agents.data || !model.data || !tools.data || !ready.data || !me.data || !aliases.data) return <p role="status">{t("agent.loading", { id })}</p>;
  const a = id ? agents.data.find((x) => x.id === id) : NEW;
  if (!a) return <p role="alert">{t("agent.none", { id, project })}</p>;
  return <Form project={project} f={a} model={model.data} custom={tools.data.tools.map((x) => x.id)} missing={ready.data.missing}
    aliases={aliases.data.aliases} readOnly={!canDesign(me.data)} others={agents.data.filter((x) => x.id !== a.id)} />;
}

export function Form({ project, f, model, custom, missing, aliases, readOnly, others = [] }:
  { project: string; f: AgentSpec; model: Model; custom: string[]; missing: string[]; aliases: Choice[]; readOnly: boolean; others?: AgentSpec[] }) {
  const tab = `${project}/#agent/${f.id}`;
  const share = canShare(useQuery(meQuery(project)).data); // D-60: the Owner's in production
  const [a, setA] = useState(f);
  const [cases, setCases] = useState(() => rows(f.cases));
  const [saved, setSaved] = useState(() => JSON.stringify(payload(f, rows(f.cases))));
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [status, setStatus] = useState("");
  const [evals, setEvals] = useState<Evals | null>(null);
  const [confirm, setConfirm] = useState<{ message: string; token: string } | null>(null);
  const qc = useQueryClient();
  const body = payload(a, cases);
  const dirty = JSON.stringify(body) !== saved;
  useServerChange(f, (now) => {
    if (dirty) return;
    const c = rows(now.cases);
    setA(now);
    setCases(c);
    setSaved(JSON.stringify(payload(now, c)));
  });
  const set = (p: Partial<AgentSpec>) => setA((s) => ({ ...s, ...p }));
  const flip = (list: string[] = [], x: string, on: boolean) => (on ? [...list, x] : list.filter((y) => y !== x));
  const setCase = (i: number, p: Partial<Row>) => setCases((cs) => cs.map((c, j) => (j === i ? { ...c, ...p } : c)));
  const forced = external(a.channels);
  const options: Record<(typeof SCOPES)[number], [string, string][]> = {
    tables: Object.keys(model.tables).map((t) => [t, t]),
    measures: model.measures.map((m) => [m.id, m.name]),
    datasets: model.datasets.map((d) => [d.id, d.name]),
    glossary: model.glossary.map((t) => [t.id, t.term]),
  };

  // every button: the action, then its 409 preview (Confirm re-sends with the token), errors by JSON path, or the result
  const send = async (action: string, extra: object, token?: string) => {
    setErrors([]);
    setStatus("");
    const r = await confirmable(action, { project, ...extra }, token);
    if (r.confirm) return setConfirm(r.confirm); // Confirm takes focus
    if (confirm) focusSoon("agent-publish"); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) {
      const failures: { question: string; missing: string[] }[] = r.json?.detail?.failures ?? []; // publish_agent: failing cases
      return setErrors([...actionErrors(r.status, r.json?.detail), ...failures.map((c) => ({ error: t("agent.caseMisses", { question: c.question, missing: c.missing.join(", ") }) }))]);
    }
    qc.invalidateQueries({ queryKey: ["agents", project] });
    return r.json;
  };

  const save = async () => {
    const out = await send("save_agent", body);
    if (!out) return;
    setSaved(JSON.stringify(body));
    setEvals(null);
    setStatus(t("agent.saved", { id: out.id }));
    if (out.id !== f.id) { // a new agent gets its own tab
      const { close, open } = useUi.getState();
      close(tab);
      open(`${project}/#agent/${out.id}`);
      focusSoon(`ws-${project}/#agent/${out.id}`);
    }
  };

  const test = async () => {
    const out: Evals | undefined = await send("test_agent", { agent: f.id });
    if (!out) return;
    setEvals(out);
    const failed = out.cases.filter((c) => !c.passed).length;
    setStatus(out.cases.length === 0 ? t("agent.noCases") : failed ? t("agent.casesFail", { failed, n: out.cases.length }) : t("agent.casesPass", { n: out.cases.length }));
  };

  const publish = async (token?: string) => {
    const out = await send("publish_agent", { agent: f.id }, token);
    if (out) setStatus(t("agent.published", { id: out.id, version: String(out.version) }));
  };

  return (
    <div className="measure tool agent">
      <form aria-labelledby="agent-title" onSubmit={(e) => { e.preventDefault(); save(); }}>
        <h2 id="agent-title">{f.id ? t("agent.title", { name: f.name }) : t("agent.new")}</h2>
        {f.id && <p>{t("agent.status")} <span className={f.disabled ? "error" : undefined}>{toolStatus(f)}</span> {t("agent.ownerSharing", { owner: f.owner ?? "", project })}</p>}
        {missing.length > 0 && (
          <div role={f.id ? undefined : "alert"}>
            <p className="error">{f.id ? t("agent.notReady") : t("agent.notReadyNew")}</p>
            <ul className="errors">{missing.map((g) => <li key={g}>{g}</li>)}</ul>
          </div>
        )}
        {readOnly && <ReadOnly project={project} why={t("agent.readOnly", { project })} />}
        <fieldset className="plain" disabled={readOnly}>
          <legend className="sr-only">{t("agent.definition")}</legend>
          {!f.id && <label>{t("agent.id")} <input value={a.id} onChange={(e) => set({ id: e.target.value })} required /></label>}
          <label>{t("agent.name")} <input value={a.name} onChange={(e) => set({ name: e.target.value })} required /></label>
          <label>{t("agent.purpose")} <input value={a.purpose} onChange={(e) => set({ purpose: e.target.value })} required /></label>
          <label>{t("agent.instructions")} <textarea rows={4} value={a.instructions} onChange={(e) => set({ instructions: e.target.value })} required /></label>
          <fieldset>
            <legend>{t("agent.scope")}</legend>
            {SCOPES.map((k) => (
              <fieldset key={k}>
                <legend>{t(`agent.scope.${k}`)}</legend>
                {options[k].length === 0 && <span className="muted">{t("agent.noneHere")}</span>}
                {options[k].map(([v, name]) => (
                  <label key={v} className="check"><input type="checkbox" checked={!!a.scope[k]?.includes(v)}
                    onChange={(e) => set({ scope: { ...a.scope, [k]: flip(a.scope[k], v, e.target.checked) } })} />{name}</label>
                ))}
              </fieldset>
            ))}
          </fieldset>
          <fieldset>
            <legend>{t("agent.tools")}</legend>
            {[...custom, ...AGENT_TOOLS].map((tool) => (
              <label key={tool} className="check"><input type="checkbox" checked={a.tools.includes(tool)} onChange={(e) => set({ tools: flip(a.tools, tool, e.target.checked) })} />
                {custom.includes(tool) ? t("agent.customTool", { tool }) : tool}</label>
            ))}
          </fieldset>
          {/* AGENT-AS-TOOL: other agents it may ask; only published ones run as a tool, so drafts can't be picked (a listed one can still be unticked) */}
          <fieldset>
            <legend>{t("agent.asks")}</legend>
            {others.length === 0 && <span className="muted">{t("agent.noneHere")}</span>}
            {others.map((o) => {
              const on = !!a.agents?.includes(o.id);
              const draft = o.status !== "published";
              return (
                <label key={o.id} className="check"><input type="checkbox" checked={on} disabled={draft && !on}
                  aria-describedby={draft ? `agent-asks-${o.id}` : undefined} onChange={(e) => set({ agents: flip(a.agents, o.id, e.target.checked) })} />
                  {o.name}{draft && <span className="muted" id={`agent-asks-${o.id}`}>{t("agent.asksDraft")}</span>}</label>
              );
            })}
          </fieldset>
          <label>{t("agent.mode")} <select value={a.mode} onChange={(e) => set({ mode: e.target.value as AgentSpec["mode"] })}>
            <option value="tools_only">{t("agent.toolsOnly")}</option><option value="exploratory">{t("agent.exploratory")}</option>
          </select></label>
          {/* AG-6: a model alias of the workspace providers with its effort (D-71); a saved alias no provider has any more stays listed */}
          <ModelEffort id="agent-model" label={t("agent.model")} choices={aliases} model={a.model ?? "auto"} effort={a.effort ?? null}
            lead={[{ value: "auto|", text: t("agent.auto") }]} onPick={(model, effort) => set({ model, effort: effort ?? undefined })} />
          <label className="check"><input type="checkbox" checked={a.grounding || forced} disabled={forced} aria-describedby="agent-grounding-help"
            onChange={(e) => set({ grounding: e.target.checked })} />{t("agent.grounding")}</label>
          <p className="muted" id="agent-grounding-help">{forced ? t("agent.groundingForced") : t("agent.groundingOptional")}</p>
          <fieldset>
            <legend>{t("agent.channels")}</legend>
            {a.channels.map((c, i) => (
              <div key={i} className="row">
                <label>{t("agent.channel", { n: i + 1 })} <select value={c.type} onChange={(e) => set({ channels: a.channels.map((x, j) => (j === i ? { type: e.target.value as Channel["type"] } : x)) })}>
                  {CHANNELS.map((c) => <option key={c} value={c}>{t(`agent.channel.${c}`)}</option>)}</select></label>
                {(c.type === "email" || c.type === "slack" || c.type === "teams") && (
                  <label>{c.type === "email" ? t("agent.address") : t("agent.channelId")} <input value={(c.type === "email" ? c.address : c.channel) ?? ""}
                    onChange={(e) => set({ channels: a.channels.map((x, j) => (j === i ? { ...x, [c.type === "email" ? "address" : "channel"]: e.target.value } : x)) })} /></label>
                )}
                {c.type === "teams" && (
                  <label>{t("agent.reply")} <input value={c.reply ?? ""}
                    onChange={(e) => set({ channels: a.channels.map((x, j) => (j === i ? { ...x, reply: e.target.value } : x)) })} /></label>
                )}
                {!readOnly && <button type="button" onClick={() => { set({ channels: a.channels.filter((_, j) => j !== i) }); focusSoon("agent-add-channel"); }}>{t("agent.removeChannel", { n: i + 1 })}</button>}
              </div>
            ))}
            {!readOnly && <button type="button" id="agent-add-channel" onClick={() => set({ channels: [...a.channels, { type: "in_app" }] })}>{t("agent.addChannel")}</button>}
          </fieldset>
          <fieldset>
            <legend>{t("agent.evalSet")}</legend>
            <ol className="results">{cases.map((c, i) => (
              <li key={i}>
                <label>{t("agent.question")} <input value={c.question} onChange={(e) => setCase(i, { question: e.target.value })} required /></label>
                <label>{t("agent.contains")} <input value={c.contains} onChange={(e) => setCase(i, { contains: e.target.value })} /></label>
                <label>{t("agent.mustCall")} <input value={c.tools} onChange={(e) => setCase(i, { tools: e.target.value })} /></label>
                {!readOnly && <button type="button" onClick={() => { setCases(cases.filter((_, j) => j !== i)); focusSoon("agent-add-case"); }}>{t("agent.removeCase", { n: i + 1 })}</button>}
              </li>
            ))}</ol>
            {!readOnly && <button type="button" id="agent-add-case" onClick={() => setCases([...cases, { question: "", contains: "", tools: "" }])}>{t("agent.addCase")}</button>}
          </fieldset>
        </fieldset>
        <Errors errors={errors} />
        {status && <p role="status">{status}</p>}
        {!readOnly && (
          <>
            {dirty && f.id && <p className="muted">{t("agent.unsaved")}</p>}
            <div className="row">
              <button type="submit" id="agent-save" className="primary" disabled={!f.id && missing.length > 0}>{t("common.save")}</button>
              {f.id && <button type="button" id="agent-test" disabled={dirty} onClick={test}>{t("agent.runEvals")}</button>}
              {f.id && <button type="button" id="agent-publish" className="primary" disabled={dirty || !evals?.passed} onClick={() => publish()}>{t("agent.publish")}</button>}
            </div>
          </>
        )}
        {confirm && (
          <Confirm label={t("agent.confirmPublish")} message={confirm.message} onConfirm={() => publish(confirm.token)}
            onCancel={() => setConfirm(null)} back="agent-publish" />
        )}
        {evals && (
          <section aria-labelledby="agent-evals-h">
            <h3 id="agent-evals-h">{t("agent.evalResults")}</h3>
            <ol className="results">{evals.cases.map((c, i) => (
              <li key={i}>
                <strong>{c.passed ? t("agent.casePassed", { question: c.question }) : t("agent.caseFailed", { question: c.question })}</strong>
                {!c.passed && <p className="error">{t("agent.missingList", { missing: c.missing.join(", ") })}</p>}
                <p>{t("agent.answer", { answer: c.answer })}</p>
              </li>
            ))}</ol>
          </section>
        )}
      </form>
      {f.id && share && <div className="row"><SharingDialog project={project} kind="agent" id={f.id} name={f.name} /></div>}
      {f.id && (f.status === "published" || !readOnly) && <History project={project} agent={f.id} />}
    </div>
  );
}

// AG-3 test pane and run history: ask as yourself, then every run's answer, grounding result and full trace, with token totals.
function History({ project, agent }: { project: string; agent: string }) {
  const [question, setQuestion] = useState("");
  const [last, setLast] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["agent_runs", project, agent],
    queryFn: () => post<{ runs: Run[]; tokens: { agent: string; runs: number; prompt_tokens: number; completion_tokens: number }[] }>("agent_runs", { project, agent }) });
  const ask = async () => {
    setError("");
    setBusy(true);
    try {
      const r = await post<{ run: string }>("run_agent", { project, agent, question });
      await qc.invalidateQueries({ queryKey: ["agent_runs", project, agent] });
      setLast(r.run);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const runs = q.data?.runs ?? [];
  const latest = runs.find((r) => r.id === last);
  const tok = q.data?.tokens.find((x) => x.agent === agent);
  return (
    <>
      <form aria-labelledby="agent-ask-h" onSubmit={(e) => { e.preventDefault(); ask(); }}>
        <h3 id="agent-ask-h">{t("agent.ask")}</h3>
        <label>{t("agent.yourQuestion")} <input id="agent-question" value={question} onChange={(e) => setQuestion(e.target.value)} required /></label>
        <div className="row"><button type="submit" id="agent-ask" disabled={busy}>{t("agent.askButton")}</button></div>
        {busy && <p role="status">{t("agent.asking")}</p>}
        {error && <p role="alert" className="error">{error}</p>}
        {latest && <div role="status"><RunView r={latest} /></div>}
      </form>
      <section aria-labelledby="agent-runs-h">
        <h3 id="agent-runs-h">{t("run.history")}</h3>
        {q.error && <p role="alert">{q.error.message}</p>}
        {tok && <p>{t("agent.tokens", { runs: tok.runs, prompt: tok.prompt_tokens, completion: tok.completion_tokens })}</p>}
        {q.data && runs.length === 0 && <p className="muted">{t("run.none")}</p>}
        <ol className="results">{runs.map((r) => <li key={r.id}><RunView r={r} /></li>)}</ol>
      </section>
    </>
  );
}

export function RunView({ r }: { r: Run }) {
  return (
    <>
      <strong>{`${fmtDate(r.at)} (${r.via}): ${r.question}`}</strong>
      <p>{t("agent.answer", { answer: r.answer })}</p>
      <p className={r.grounded === false ? "error" : undefined}>{t("agent.verdict", { verdict: verdict(r) })}</p>
      <Trace run={r.id} steps={r.trace} />
    </>
  );
}

// AG-3 tool calls of one run; an ask_agent call shows the asked agent's run (its tools and tokens) right after the table.
function Trace({ run, steps }: { run: string; steps: Step[] }) {
  if (steps.length === 0) return <p className="muted">{t("agent.noCalls")}</p>;
  return (
    <>
      <table>
        <caption className="sr-only">{t("agent.calls", { run })}</caption>
        <thead><tr><th scope="col">{t("agent.tool")}</th><th scope="col">{t("agent.input")}</th><th scope="col">{t("run.output")}</th></tr></thead>
        <tbody>{steps.map((s, i) => (
          <tr key={i}><td>{s.tool}</td><td><code>{short(s.input)}</code></td><td><code>{short(s.output)}</code></td></tr>
        ))}</tbody>
      </table>
      {steps.map((s, i) => s.run && (
        <div key={i} className="nested">
          <p>{t("agent.nested", { n: i + 1, agent: s.run.agent, prompt: s.run.prompt_tokens, completion: s.run.completion_tokens })}</p>
          <Trace run={s.run.id} steps={s.run.trace} />
        </div>
      ))}
    </>
  );
}
