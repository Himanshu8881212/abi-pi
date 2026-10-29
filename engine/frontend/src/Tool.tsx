import Editor from "@monaco-editor/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { jsonDefaults } from "monaco-editor/languages/features/json/register";
import { useEffect, useState } from "react";
import schema from "../../schemas/v1/tool.json";
import { canDesign, canEdit, confirmable, meQuery, modelQuery, post, toolsQuery, toolStatus, type ToolSpec } from "./api";
import { ReadOnly } from "./Production";
import { actionErrors, Confirm, Errors, type ActionError } from "./mel";
import "./monaco";
import type { SemanticModel } from "./Properties";
import { t } from "./i18n";
import { focusSoon, useServerChange, useUi } from "./store";

// save_tool sets the first four; list_tools adds the TL-4 runtime state and a calculation's code (not in the JSON file)
const HIDDEN = ["$schema", "version", "owner", "status", "disabled", "error", "code"];
const columnsOf = (m: SemanticModel) => Object.entries(m.columns).flatMap(([t, cs]) => Object.keys(cs).map((c) => `${t}.${c}`));

// A new tool's starting point: valid against the schema except the empty id, name, description and measures.
export const NEW = { id: "", name: "", description: "", inputs: { type: "object", properties: {}, additionalProperties: false },
  query: { measures: [] }, outputs: { type: "array", items: { type: "object" } }, tests: [{ inputs: {} }], permissions: { role: "viewer" } };
// TL-5 a new calculation's code (tools/<id>.py)
export const CODE = "def calculate(rows, inputs):\n    # rows: the query's result rows; return the result rows (matching `outputs`)\n    return rows\n";

// The editor's text: the tool file without the fields the server sets or the runtime state.
export const editable = (t: object) =>
  JSON.stringify(Object.fromEntries(Object.entries(t).filter(([k]) => !HIDDEN.includes(k))), null, 2);

// schemas/v1/tool.json for the editor: server-set fields aren't required, and the project's measure IDs and
// table.column names (semantic_model) are enums, so Monaco completes them and marks unknown ones.
export function editorSchema(m: SemanticModel) {
  const s = structuredClone(schema) as Record<string, any>;
  s.required = s.required.filter((k: string) => !HIDDEN.includes(k));
  const q = s.$defs.query.properties;
  const cols = columnsOf(m);
  q.measures.items.enum = m.measures.map((x) => x.id);
  q.dimensions.items.enum = cols;
  q.filters.items.properties.column.enum = cols;
  return s;
}

type Doc = Record<string, any>;
type Kind = "query" | "calculation" | "api";
const json = (s: string) => { try { return { v: JSON.parse(s) }; } catch { return null; } };
const parse = (text: string): Doc | null => {
  const d = json(text)?.v;
  return d && typeof d === "object" && !Array.isArray(d) ? d : null;
};
const show = (d: Doc) => JSON.stringify(d, null, 2);

// TL-5: the kind decides the fields (schemas/v1/tool.json if/then/else): query and calculation tools have `query`,
// an API tool `request` + `rows` instead. A switch keeps everything else.
export function withKind(d: Doc, kind: Kind): Doc {
  const { kind: _, query, request, rows, ...rest } = d;
  if (kind === "api") return { ...rest, kind, request: request ?? { method: "GET", url: "https://" }, rows: rows ?? "" };
  return { ...rest, ...(kind === "calculation" && { kind }), query: query ?? { measures: [] } };
}

// An API tool's request with `patch` applied; undefined drops a field, and a GET has no body (schema).
export function withRequest(d: Doc, patch: Doc): Doc {
  const r = Object.fromEntries(Object.entries({ ...d.request, ...patch }).filter(([, v]) => v !== undefined));
  if (r.method === "GET") delete r.body;
  return { ...d, request: r };
}

// A header/query value: literal text, exactly ${secret:NAME} (D-2; the name only, never a value), or {"$input": name}.
const SECRET = /^\$\{secret:([A-Za-z0-9_]+)\}$/;
export const ref = (name: string) => `\${secret:${name}}`;
export const valueKind = (v: unknown) =>
  v && typeof v === "object" && "$input" in v ? "input" : typeof v === "string" && SECRET.test(v) ? "secret" : "text";

type TestRun = { passed: boolean; tests: { test: number; passed: boolean; error?: string; rows?: Record<string, unknown>[] }[] };
type Confirm = { message: string; token: string; preview?: unknown; action: string };

// TL-3 tool editor tab ("project/#tool/<id>"; an empty id is a new tool): draft → test on real data → publish.
export default function Tool({ project, id }: { project: string; id: string }) {
  const tools = useQuery(toolsQuery(project));
  const model = useQuery(modelQuery<SemanticModel>(project));
  const me = useQuery(meQuery(project));
  const editor = canEdit(me.data?.role);
  // D-2: secret names for API tool headers/query (an Editor right); never values
  const secrets = useQuery({ queryKey: ["secrets", project], queryFn: () => post<{ secrets: string[] }>("list_secrets", { project }), enabled: editor });
  const err = tools.error ?? model.error ?? me.error ?? secrets.error;
  if (err) return <p role="alert">{t("tool.loadFailed", { project, error: err.message })}</p>;
  if (!tools.data || !model.data || !me.data || (editor && !secrets.data)) return <p role="status">{t("tool.loading", { id })}</p>;
  const tool = id ? tools.data.tools.find((x) => x.id === id) : NEW;
  if (!tool) return <p role="alert">{t("tool.none", { id, project })}</p>;
  return <Form project={project} t={tool} model={model.data} readOnly={!canDesign(me.data)} owner={me.data.role === "owner"} secrets={secrets.data?.secrets ?? []} />;
}

export function Form({ project, t: tool, model, readOnly, owner = false, secrets = [] }: { project: string; t: { id: string; name: string } & Partial<ToolSpec>;
  model: SemanticModel; readOnly: boolean; owner?: boolean; secrets?: string[] }) {
  const tab = `${project}/#tool/${tool.id}`;
  const [text, setText] = useState(() => editable(tool));
  const [saved, setSaved] = useState(text);
  const [code, setCode] = useState(tool.code ?? "");
  const [savedCode, setSavedCode] = useState(code);
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [status, setStatus] = useState("");
  const [run, setRun] = useState<TestRun | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const qc = useQueryClient();
  const dirty = text !== saved || code !== savedCode;
  useServerChange(tool, (now) => {
    if (dirty) return;
    const e = editable(now);
    setText(e);
    setSaved(e);
    setCode(now.code ?? "");
    setSavedCode(now.code ?? "");
  });
  const cols = columnsOf(model);
  const doc = parse(text);
  const kind: Kind = doc?.kind ?? "query";
  const needOwner = tool.kind === "api" && !owner; // D-35, UI-3: shown disabled with the reason

  useEffect(() => { // one tool tab is mounted at a time, so its project's schema applies to every JSON model
    jsonDefaults.setDiagnosticsOptions({ validate: true, enableSchemaRequest: false,
      schemas: [{ uri: schema.$id, fileMatch: ["*"], schema: editorSchema(model) }] });
  }, [model]);

  const send = async (action: string, body: object, token?: string) => {
    setErrors([]);
    setStatus("");
    const r = await confirmable(action, { project, ...body }, token);
    if (r.confirm) return setConfirm({ ...r.confirm, action }); // Confirm takes focus
    if (confirm) focusSoon(confirm.action === "publish_tool" ? "tool-publish" : "tool-delete"); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) {
      const failures: string[] = r.json?.detail?.failures ?? []; // publish_tool: the failing tests
      return setErrors([...actionErrors(r.status, r.json?.detail), ...failures.map((error) => ({ error }))]);
    }
    qc.invalidateQueries({ queryKey: ["tools", project] });
    return r.json;
  };

  const save = async () => {
    let def; // not `tool`: that is the opened tool, whose id decides below whether the tab changes
    try {
      def = JSON.parse(text);
    } catch (e) {
      return setErrors([{ error: t("flow.badJson", { error: (e as Error).message }) }]);
    }
    const out = await send("save_tool", { tool: def, ...(def.kind === "calculation" && { code }) });
    if (!out) return;
    setSaved(text);
    setSavedCode(code);
    setRun(null);
    setStatus(t("tool.saved", { id: out.id }));
    if (out.id !== tool.id) { // a new tool (or a new id) gets its own tab
      const { close, open } = useUi.getState();
      close(tab);
      open(`${project}/#tool/${out.id}`);
      focusSoon(`ws-${project}/#tool/${out.id}`);
    }
  };

  const setKind = (k: Kind) => {
    if (!doc) return;
    setText(show(withKind(doc, k)));
    if (k === "calculation" && !code) setCode(CODE);
  };

  const test = async () => {
    const out = await send("test_tool", { tool: tool.id });
    if (!out) return;
    setRun(out);
    const failed = out.tests.filter((x: { passed: boolean }) => !x.passed).length;
    setStatus(out.tests.length === 0 ? t("tool.noTests") : failed ? t("tool.testsFail", { failed, n: out.tests.length }) : t("tool.testsPass", { n: out.tests.length }));
  };

  const publish = async (token?: string) => {
    const out = await send("publish_tool", { tool: tool.id }, token);
    if (out) setStatus(t("agent.published", { id: out.id, version: String(out.version) }));
  };

  const remove = async (token?: string) => {
    if (!(await send("delete_tool", { tool: tool.id }, token))) return;
    const { close } = useUi.getState();
    close(tab);
    const next = useUi.getState().active;
    focusSoon(next ? `ws-${next}` : "chat-input"); // no screen left: the Chats home
  };

  return (
    <form className="measure tool" aria-labelledby="tool-title" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h2 id="tool-title">{tool.id ? t("tool.title", { name: tool.name }) : t("tool.new")}</h2>
      {tool.id && <p>{t("agent.status")} <span className={tool.disabled ? "error" : undefined}>{toolStatus(tool)}</span></p>}
      {readOnly && <ReadOnly project={project} why={t("tool.readOnly", { project })} />}
      <label>{t("tool.kind")}
        <select id="tool-kind" value={kind} disabled={readOnly || !doc} aria-describedby="tool-kind-help" onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="query">{t("tool.kind.query")}</option>
          <option value="calculation">{t("tool.kind.calculation")}</option>
          <option value="api">{t("tool.kind.api")}</option>
        </select>
      </label>
      <p className="muted" id="tool-kind-help">{doc ? t("tool.kindHelp") : t("tool.kindFix")}</p>
      <span id="tool-json-label">{t("tool.definition")}</span>
      <p className="muted" id="tool-json-help">{t("tool.hint")}</p>
      <div className="mel">
        <Editor height="420px" language="json" value={text} onChange={(v) => setText(v ?? "")} loading={<p role="status">{t("common.loadingEditor")}</p>}
          options={{ readOnly, ariaLabel: t("tool.editorLabel"), tabFocusMode: true, minimap: { enabled: false },
            scrollBeyondLastLine: false, fontSize: 13, automaticLayout: true }} />
      </div>
      {kind === "api" && doc && <Request doc={doc} set={(d) => setText(show(d))} secrets={secrets} readOnly={readOnly} />}
      {kind === "calculation" && (
        <>
          <span>{t("tool.code", { id: tool.id || "<id>" })}</span>
          <p className="muted">{t("tool.codeHint")}</p>
          <div className="mel">
            <Editor height="240px" language="python" value={code} onChange={(v) => setCode(v ?? "")} loading={<p role="status">{t("common.loadingEditor")}</p>}
              options={{ readOnly, ariaLabel: t("tool.codeLabel"), tabFocusMode: true, minimap: { enabled: false },
                scrollBeyondLastLine: false, fontSize: 13, automaticLayout: true }} />
          </div>
        </>
      )}
      <details>
        <summary>{t("tool.idsOf", { project })}</summary>
        <p>{t("tool.measures", { list: model.measures.map((m) => m.id).join(", ") || t("tool.noneList") })}</p>
        <p>{t("tool.columns", { list: cols.join(", ") || t("tool.noneList") })}</p>
      </details>
      <Errors errors={errors} />
      {status && <p role="status">{status}</p>}
      {!readOnly && (
        <>
          {dirty && tool.id && <p className="muted">{t("tool.unsaved")}</p>}
          {needOwner && <p className="muted" id="tool-publish-why">{t("tool.needOwner", { project })}</p>}
          <div className="row">
            <button type="submit" className="primary">{t("common.save")}</button>
            {tool.id && <button type="button" disabled={dirty} onClick={test}>{t("tool.test")}</button>}
            {tool.id && <button type="button" id="tool-publish" className="primary" disabled={dirty || !run?.passed || needOwner}
              aria-describedby={needOwner ? "tool-publish-why" : undefined} onClick={() => publish()}>{t("agent.publish")}</button>}
            {tool.id && <button type="button" id="tool-delete" className="danger" onClick={() => remove()}>{t("tool.delete")}</button>}
          </div>
        </>
      )}
      {confirm && (
        <Confirm label={confirm.action === "publish_tool" ? t("tool.confirmPublishTool") : t("tool.confirmDeleteTool")} message={confirm.message}
          ok={confirm.action === "publish_tool" ? t("tool.confirmPublish") : t("common.confirmDelete")}
          onConfirm={() => (confirm.action === "publish_tool" ? publish : remove)(confirm.token)}
          onCancel={() => setConfirm(null)} back={confirm.action === "publish_tool" ? "tool-publish" : "tool-delete"}>
          {confirm.preview != null && <pre tabIndex={0} aria-label={t("tool.whatYouConfirm")}>{JSON.stringify(confirm.preview, null, 2)}</pre>}
        </Confirm>
      )}
      {run && (
        <section aria-labelledby="tool-results-h">
          <h3 id="tool-results-h">{t("tool.results")}</h3>
          <ol className="results">{run.tests.map((r) => (
            <li key={r.test}>
              <strong>{r.passed ? t("tool.testPassed", { n: r.test }) : t("tool.testFailed", { n: r.test })}</strong>
              {r.error && <p className="error">{r.error}</p>}
              {r.rows && <Rows n={r.test} rows={r.rows} />}
            </li>
          ))}</ol>
        </section>
      )}
    </form>
  );
}

// TL-5 an API tool's request, edited in the definition JSON (one source of truth with the Monaco text).
function Request({ doc, set, secrets, readOnly }: { doc: Doc; set: (d: Doc) => void; secrets: string[]; readOnly: boolean }) {
  const r: Doc = doc.request ?? {};
  const patch = (p: Doc) => set(withRequest(doc, p));
  const [body, setBody] = useState<string | null>(null); // a draft that isn't valid JSON yet; null: the definition's body
  const bodyText = body ?? (r.body === undefined ? "" : show(r.body));
  const bodyOk = !bodyText.trim() || json(bodyText) !== null;
  const editBody = (v: string) => {
    setBody(v);
    const p = v.trim() ? json(v) : { v: undefined };
    if (p) patch({ body: p.v });
  };
  return (
    <fieldset disabled={readOnly}>
      <legend>{t("tool.request")}</legend>
      <label>{t("tool.method")}
        <select value={r.method ?? "GET"} onChange={(e) => patch({ method: e.target.value })}>{["GET", "POST"].map((m) => <option key={m}>{m}</option>)}</select>
      </label>
      <label>{t("tool.url")} <input inputMode="url" value={r.url ?? ""} aria-describedby="tool-url-help" onChange={(e) => patch({ url: e.target.value })} /></label>
      <p className="muted" id="tool-url-help">{t("tool.urlHelp")}</p>
      <Pairs kind="query" value={r.query} onChange={(query) => patch({ query })} secrets={secrets} inputs={Object.keys(doc.inputs?.properties ?? {})} />
      <Pairs kind="header" value={r.headers} onChange={(headers) => patch({ headers })} secrets={secrets} />
      {!secrets.length && !readOnly && <p className="muted">{t("tool.noSecrets")}</p>}
      {r.method === "POST" && (
        <label>{t("tool.body")}
          <textarea rows={4} value={bodyText} aria-invalid={!bodyOk} aria-describedby={bodyOk ? undefined : "tool-body-err"}
            onChange={(e) => editBody(e.target.value)} onBlur={() => bodyOk && setBody(null)} />
        </label>
      )}
      {!bodyOk && <p className="error" id="tool-body-err">{t("tool.bodyInvalid")}</p>}
      <label>{t("tool.rows")}
        <input value={doc.rows ?? ""} onChange={(e) => set({ ...doc, rows: e.target.value })} />
      </label>
    </fieldset>
  );
}

// Name/value pairs; a value is text, a secret picked by name (written as ${secret:NAME}), or an input (query only).
function Pairs({ kind: of, value = {}, onChange, secrets, inputs }: { kind: "query" | "header"; value?: Doc; onChange: (v: Doc | undefined) => void;
  secrets: string[]; inputs?: string[] }) {
  const rows = Object.entries(value);
  const add = `tool-${of}-add`;
  const put = (next: [string, unknown][]) => onChange(next.length ? Object.fromEntries(next) : undefined);
  const set = (i: number, k: string, v: unknown) => put(rows.map((r, j) => (j === i ? [k, v] : r)));
  return (
    <fieldset>
      <legend>{t(`tool.${of}.legend`)}</legend>
      {rows.map(([k, v], i) => {
        const kind = valueKind(v);
        const item = t(`tool.${of}.item`, { n: i + 1 });
        const secret = kind === "secret" ? SECRET.exec(v as string)![1] : "";
        const input = kind === "input" ? String((v as Doc).$input) : "";
        return (
          <div className="row" key={i}>
            <input aria-label={t("tool.pair.name", { item })} value={k} onChange={(e) => set(i, e.target.value, v)} />
            <select aria-label={t("tool.pair.from", { item })} value={kind}
              onChange={(e) => set(i, k, e.target.value === "secret" ? ref(secrets[0]) : e.target.value === "input" ? { $input: inputs?.[0] } : "")}>
              <option value="text">{t("tool.pair.text")}</option>
              <option value="secret" disabled={!secrets.length && kind !== "secret"}>{t("tool.pair.secretOption")}</option>
              {inputs && <option value="input" disabled={!inputs.length && kind !== "input"}>{t("agent.input")}</option>}
            </select>
            {kind === "secret" ? (
              <select aria-label={t("tool.pair.secret", { item })} value={secret} onChange={(e) => set(i, k, ref(e.target.value))}>
                {[...new Set([...secrets, secret])].map((s) => <option key={s}>{s}</option>)}
              </select>
            ) : kind === "input" ? (
              <select aria-label={t("tool.pair.input", { item })} value={input} onChange={(e) => set(i, k, { $input: e.target.value })}>
                {[...new Set([...(inputs ?? []), input])].map((s) => <option key={s}>{s}</option>)}
              </select>
            ) : <input aria-label={t("tool.pair.value", { item })} value={String(v)} onChange={(e) => set(i, k, e.target.value)} />}
            <button type="button" aria-label={t(`tool.${of}.remove`, { n: i + 1 })} onClick={() => { put(rows.filter((_, j) => j !== i)); focusSoon(add); }}>{t("tool.remove")}</button>
          </div>
        );
      })}
      <button type="button" id={add} disabled={"" in value} onClick={() => put([...rows, ["", ""]])}>{t(`tool.${of}.add`)}</button>
    </fieldset>
  );
}

function Rows({ n, rows }: { n: number; rows: Record<string, unknown>[] }) {
  if (!rows.length) return <p className="muted">{t("tool.noRows")}</p>;
  const cols = Object.keys(rows[0]);
  return (
    <table>
      <caption className="sr-only">{t("tool.rowsOf", { n })}</caption>
      <thead><tr>{cols.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={i}>{cols.map((c) => <td key={c}>{r[c] == null ? "" : String(r[c])}</td>)}</tr>)}</tbody>
    </table>
  );
}
