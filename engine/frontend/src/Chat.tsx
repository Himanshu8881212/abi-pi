import { useQuery } from "@tanstack/react-query";
import { lazy, Suspense, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { aliasesQuery, call, canDesign, chatgptQuery, chatsQuery, confirmable, confirmText, copyText, download, meQuery, post, queryClient, reportsQuery, tablesQuery, type ChatGptAccount, type ChatRow, type LlmProvider, type MeasureSpec } from "./api";
import { locale } from "./Locale";
import { level, levelText, type Choice } from "./Effort";
import { Icon, type IconName } from "./icons";
import { openDialog } from "./NewData";
import { focusSoon, pickTable, useDismiss, useUi } from "./store";
import { fmtDate, fmtNumber, t } from "./i18n";
import { chatSpec, Md, visualBlocks, type ChatSpec } from "./md";

const ChatVisual = lazy(() => import("./Report").then((m) => ({ default: m.ChatVisual }))); // ECharts stays in its own chunk
import { Confirm as ConfirmBox } from "./mel";

// UI-1 Chat tab: the Main Agent over POST /api/chat (SSE, F-06). A `confirm` event carries the
// token for a destructive action; only the user's Confirm click sends it (§5 #3), never the LLM.
// `shown`: one-time values the confirmed action returned for the person (SC-4/SC-8), kept out of `chatHistory`
// `status`: the click's outcome as a word, the only part of the result a saved chat keeps (CHAT-PERSIST, SC-8)
type Shown = { label: string; value: string };
type Confirm = { action: string; input: unknown; message: string; confirm_token: string; state?: string; shown?: Shown[]; status?: "done" | "failed" | "cancelled" };
// `template`: AG-2 refused the answer twice (the `message` event carries `flags`); chats saved before GROUNDING-UX hold
// the old tool-results text with `template` alone
// `used`: the answer's tokens and effort from the SSE `done` event (D-71; numbers from the server, principle 7)
// and the model that answered as the server named it (UI-DS.5.4)
export type Used = { prompt_tokens: number; completion_tokens: number; reasoning_tokens: number; effort: string | null; model?: string | null };
// GROUNDING-UX: `flags` = each refused draft's counts (never its values); `results` = this answer's tool results, shown
// on request as tables and never saved; `how`: RP-5 for a semantic_query (the server's `reports.explain`, UI-DS.5.4)
export type Flag = { numbers: number; names: number; visuals?: number }; // visuals: refused chart blocks (UI-DS.6)
export type How = { measures: MeasureSpec[]; filters?: { column: string; op: string; value: unknown; level?: string }[];
  rates_table?: string | null; sql?: string; clickhouse?: Record<string, string>; error?: string };
export type Result = { name: string; result: unknown; how?: How };
// AGENT-CONTEXT.4 (D-117 item 4): the server's live line for one tool step, kept on its tool item for the footer
export type Step = { step: number; tool: string; status: "ok" | "refused" | "confirm"; rows?: number };
export type Item =
  | { kind: "user" | "assistant" | "error" | "tool"; text: string; template?: boolean; used?: Used; flags?: Flag[]; results?: Result[]; ctx?: string; step?: Step }
  | { kind: "confirm"; c: Confirm }
  | { kind: "file"; link: string };

export function parseSse(buf: string): { events: { event: string; data: any }[]; rest: string } {
  const blocks = buf.split(/\r?\n\r?\n/);
  const rest = blocks.pop()!;
  const events = blocks.flatMap((b) => {
    const field = (f: string) => b.split(/\r?\n/).filter((l) => l.startsWith(f + ":")).map((l) => l.slice(f.length + 1).trimStart());
    const data = field("data").join("\n");
    return data ? [{ event: field("event")[0] ?? "message", data: JSON.parse(data) }] : [];
  });
  return { events, rest };
}

// Reads an SSE body to its end, handing each event to `on` (Chat and RP-7 live tiles).
export async function readSse(body: NonNullable<Response["body"]>, on: (event: string, data: any) => void) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    const { events, rest } = parseSse(buf + value);
    buf = rest;
    for (const { event, data } of events) on(event, data);
  }
}

// POST /api/chat with the current project, tab and picked tables (§5 context-aware) and this chat's effort (D-71) and model pick
// (MODEL-PICKER: "gpt:<slug>" a ChatGPT plan model, "ws:<alias>" a workspace alias; never stored); each SSE event goes to `on`.
// The Ask about this drawer passes its own `context` (askContext).
export async function postChat(messages: { role: string; content: string; ctx?: string }[], on: (event: string, data: any) => void, effort = "", pick = "", context?: object) {
  const { project, active, chatTables } = useUi.getState();
  const model = pick.startsWith("gpt:") ? { model: pick.slice(4) } : pick.startsWith("ws:") ? { alias: pick.slice(3) } : {};
  const r = await call("/api/chat", { method: "POST", body: JSON.stringify({ messages, project, context: context ?? { tab: active, ...(chatTables.length && { tables: chatTables }) }, ...(effort && { effort }), ...model }) });
  if (!r.ok || !r.body) throw new Error((await r.json().catch(() => null))?.detail ?? r.statusText);
  await readSse(r.body, on);
}

// Only the conversation goes to the LLM; confirm items (and a token a confirmed action returned) stay on screen.
// An answer carries its sealed tool context (`ctx`, GATE-9-fix-5): the server replays that turn's tool calls from it.
export const chatHistory = (items: Item[]) =>
  items.flatMap((i) => (i.kind === "user" || i.kind === "assistant" ? [{ role: i.kind, content: i.text, ...(i.ctx && { ctx: i.ctx }) }] : []));

// SC-4/SC-8: what a confirmed action made for the person (a webhook token, an embed signing key, a share/embed/hook
// link) is shown once, here, with Copy; nothing stores it or sends it on (LLM-2.8.2-fix-1).
export function shown(out: Record<string, unknown> | null): Shown[] {
  const origin = globalThis.location?.origin ?? "";
  return [
    typeof out?.token === "string" && { label: t("chat.shownToken"), value: out.token },
    typeof out?.key === "string" && { label: t("chat.shownKey"), value: out.key },
    typeof out?.url === "string" && { label: t("chat.shownLink"), value: origin + out.url },
  ].filter((x): x is Shown => !!x);
}

// MA-3: the Confirm click's answer. A 409 with a new token means the signed preview changed since the agent asked
// (an earlier click removed what it listed): show the new message and Confirm again (the token never reaches the LLM).
export function afterConfirm(c: Confirm, r: Awaited<ReturnType<typeof confirmable>>): Confirm {
  if (r.confirm) return { ...c, message: r.confirm.message, confirm_token: r.confirm.token, state: undefined };
  return r.ok ? { ...c, state: t("common.done"), shown: shown(r.json), status: "done" } : { ...c, state: t("common.failed", { error: r.error }), status: "failed" };
}

// CHAT-PERSIST (SC-4, SC-8): what `save_chat` keeps. A confirm item loses its token and its shown values (a one-time
// token, key or link) and keeps only the status word; a file item (a one-off download link) isn't kept either.
export const stored = (items: Item[]) => items.flatMap((i): object[] => i.kind === "file" ? [] : i.kind !== "confirm" ? [{ ...i, results: undefined }]
  : [{ kind: i.kind, c: { action: i.c.action, input: i.c.input, message: i.c.message, ...(i.c.status && { status: i.c.status }) } }]);

// A saved chat back on screen: a clicked confirm shows its status word; an unclicked one has no token, so its Confirm
// click gets a fresh preview and token from the server (MA-3, `afterConfirm`).
export const reopened = (items: Item[]): Item[] => items.map((i) => i.kind !== "confirm" ? i
  : { kind: "confirm", c: { ...i.c, confirm_token: "", state: i.c.status ? STATUS[i.c.status]() : undefined } });
const STATUS = { done: () => t("common.done"), failed: () => t("chat.failedSaved"), cancelled: () => t("chat.cancelled") };

// UI-DS.5.4: Cancel drops the preview's token with no call; the chat keeps only the status word
export const cancelled = (c: Confirm): Confirm => ({ ...c, confirm_token: "", state: t("chat.cancelled"), status: "cancelled" });

// The open chat's saved id and project; the project is the one current at its first save (the server keeps it).
export type Saved = { id: number | null; project?: string | null };
let saving = Promise.resolve(); // saves run one after another, so a replace carries the id its create got
export function saveChat(c: Saved, items: Item[]) {
  if (c.project === undefined) c.project = useUi.getState().project;
  const body = { project: c.project, items: stored(items) };
  const done = saving.then(() => post<{ id: number }>("save_chat", { ...body, id: c.id })).then((r) => void (c.id = r.id));
  saving = done.catch(() => {});
  return done;
}

// A saved chat as stored once every save already sent has landed (a chat just left elsewhere keeps its last turn).
export const history = (id: number) => saving.then(() => post<{ id: number; project: string | null; items: Item[] }>("chat_history", { id }));

// UI-DS.5.3-fix-1 (CHAT-PERSIST): one saved chat lives in one Chat, or both would save it and drop each other's turns.
// Asked to open a chat (☰, ⌘K, the sidebar), the home chat opens it ("open") and the drawer leaves it if it holds it
// ("leave"); nothing moves while the home chat streams, nor while the drawer streams that chat (its next save would win).
type Live = { id: number | null; busy: boolean };
export function moves(open: number, drawer: boolean, home: Live, ask: Live): "open" | "leave" | null {
  if (home.busy || (ask.busy && ask.id === open)) return null;
  return !drawer ? "open" : ask.id === open ? "leave" : null;
}

// a chat that ends in the person's question while nothing runs has no answer: a reload cut it
export const interrupted = (items: Item[], busy: boolean) => !busy && items[items.length - 1]?.kind === "user";

export const stepText = (s: Step) => s.status === "confirm" ? t("chat.stepConfirm", { n: s.step })
  : s.status === "refused" ? t("chat.stepRefused", { n: s.step })
  : s.rows === undefined ? t("chat.stepOk", { n: s.step }) : t("chat.stepRows", { n: s.step, count: s.rows });

// UI-1.3: one SSE event → one chat item. A blank answer becomes an error with Retry, never an empty bubble (LLM-2.3).
export function toItem(event: string, data: any): Item | null {
  if (event === "message") return String(data.content ?? "").trim() ? { kind: "assistant", text: data.content, ...(data.flags && { flags: data.flags, template: true }) }
    : { kind: "error", text: t("chat.noAnswer") };
  if (event === "error") return { kind: "error", text: data.message };
  if (event === "tool_call") return { kind: "tool", text: data.name };
  if (event === "confirm") return { kind: "confirm", c: { ...data, message: confirmText(data.action, data.message) } };
  // RP-6: a tool result carrying a file (export_visual's `link`, report_snapshot's `artifact:` snapshot, as `builtin.link`)
  const { link, snapshot } = (event === "tool_result" && data.result) || {};
  if (typeof link === "string" && link.startsWith("/api/artifacts/")) return { kind: "file", link };
  if (typeof snapshot === "string" && snapshot.startsWith("artifact:")) return { kind: "file", link: "/api/artifacts/" + snapshot.slice(9) };
  return null;
}

function ShownValue({ s, first }: { s: Shown; first: boolean }) {
  const [note, setNote] = useState<string | null>(null);
  return (
    <div className="row">
      <label>{s.label} <input key={s.value} autoFocus={first} readOnly value={s.value} autoComplete="off" spellCheck={false} onFocus={(e) => e.target.select()} /></label>
      <button type="button" className="sm" onClick={async () => setNote((await copyText(s.value)) ? t("chat.copied") : t("chat.copyFailed"))}>{t("chat.copy")}</button>
      {note && <span role="status">{note}</span>}
    </div>
  );
}

function FileItem({ link }: { link: string }) {
  const [error, setError] = useState<string | null>(null);
  const name = link.split("/").pop()!;
  return (
    <li className="file">
      <button type="button" className="sm" onClick={() => download(link).then(() => setError(null), (e) => setError((e as Error).message))}>
        {t("chat.download", { file: name.split(".").pop()!.toUpperCase() })}
      </button>
      {error && <p role="alert">{t("chat.downloadFailed", { error })}</p>}
    </li>
  );
}

// A starter: open the Chats screen with an editable question in the composer; nothing is sent.
export function prefill(text: string) {
  useUi.getState().home();
  setTimeout(() => {
    const input = document.getElementById("chat-input") as HTMLTextAreaElement | null;
    if (input) (input.value = text), input.focus();
  });
}

// GROUNDING-UX: a tool result as tables (rows with columns, else one row per key; nested values as nested tables)
const ROWS = 100;
function Value({ v }: { v: unknown }) {
  if (v === null || v === undefined) return null;
  if (typeof v === "number") return <>{fmtNumber(v)}</>;
  if (typeof v !== "object") return <>{String(v)}</>;
  if (Array.isArray(v) && v.every((x) => x === null || typeof x !== "object")) return <>{v.map((x, k) => <span key={k}>{k > 0 && ", "}<Value v={x} /></span>)}</>;
  return <Table r={v} />;
}

function Table({ r, caption }: { r: any; caption?: string }) {
  const grid = Array.isArray(r?.columns) && Array.isArray(r?.rows);
  const head: string[] = grid ? r.columns.map(String) : [t("chat.resultKey"), t("chat.resultValue")];
  const body: unknown[][] = grid ? r.rows.map((x: any) => (Array.isArray(x) ? x : Object.values(x ?? {})))
    : Array.isArray(r) ? r.map((x, k) => [k + 1, x]) : Object.entries(r ?? {});
  return (
    <table>
      {caption && <caption>{caption}</caption>}
      <thead><tr>{head.map((h, k) => <th key={k} scope="col">{h}</th>)}</tr></thead>
      <tbody>{body.slice(0, ROWS).map((row, k) => <tr key={k}>{row.map((v, c) => <td key={c}><Value v={v} /></td>)}</tr>)}</tbody>
      {body.length > ROWS && <tfoot><tr><td colSpan={head.length}>{t("chat.moreRows", { count: body.length - ROWS })}</td></tr></tfoot>}
    </table>
  );
}

export function Results({ results }: { results: Result[] }) {
  return (
    <div className="results" tabIndex={0} aria-label={t("chat.results")}>
      {results.map((x, k) => <Table key={k} r={x.result} caption={t("chat.resultOf", { n: String(k + 1), tool: x.name })} />)}
    </div>
  );
}

// GROUNDING-UX: why the answer is masked (counts and reasons only) and Retry; its tool results are in "How is this calculated?"
function Unverified({ flags, onRetry }: { flags: Flag[]; onRetry: () => void }) {
  return (
    <>
      <details className="why">
        <summary>{t("chat.why")}</summary>
        <ul>{flags.map((f, k) => <li key={k}>{t("chat.whyDraft", { n: String(k + 1), numbers: f.numbers, names: f.names })}
          {!!f.visuals && ` ${t("chat.whyVisuals", { count: f.visuals })}`}</li>)}</ul>
        <p>{t("chat.whyReason")}</p>
      </details>
      <div className="row"><button type="button" className="sm" onClick={onRetry}>{t("chat.retry")}</button></div>
    </>
  );
}

// RP-5 "How is this calculated?" of one semantic query: measures (with the ones they reference), filters (with their
// level on a report), time zone and currency, the SQL (and what ClickHouse runs); shared by report visuals and the chat
export function Explanation({ v, loc }: { v: How; loc: { time_zone?: string; currency?: string | null } }) {
  return (
    <div className="explain">
      <h4>{t("report.measures")}</h4>
      <ul>{v.measures.map((m) => <li key={m.id}>{m.name} <span className="muted">({m.id})</span>: <code>{m.expression}</code>{m.description && ` — ${m.description}`}</li>)}</ul>
      <h4>{t("report.filters")}</h4>
      {v.filters?.length ? <ul>{v.filters.map((f, i) => <li key={i}>{f.level && `${f.level}: `}<code>{f.column} {f.op} {JSON.stringify(f.value)}</code></li>)}</ul> : <p className="muted">{t("report.noFilters")}</p>}
      {loc.time_zone && <><h4>{t("report.localeTitle")}</h4>
        <p>{locale(loc)}{v.rates_table && <> {t("report.ratesTable", { table: v.rates_table })}</>}</p></>}
      <h4>{t("report.sql")}</h4>
      {v.error ? <p className="error">{v.error}</p> : <pre tabIndex={0} aria-label={t("report.generatedSql")}>{v.sql}</pre>}
      {Object.entries(v.clickhouse ?? {}).map(([name, sql]) => (
        <pre key={name} tabIndex={0} aria-label={t("report.liveSql", { name })}>{`-- ${t("report.runInClickHouse", { name })}\n${sql}`}</pre>))}
    </div>
  );
}

// UI-DS.5.4: the answer's tool results as tables, and for each semantic query its measures, filters and SQL
export function Calc({ results }: { results: Result[] }) {
  return (
    <details className="calc">
      <summary>{t("report.how")}</summary>
      <Results results={results} />
      {results.map((x, k) => x.how && (
        <section key={k}>
          <h4>{t("chat.howOf", { n: String(k + 1), tool: x.name })}</h4>
          <Explanation v={{ ...x.how, filters: (x.result as { filters?: How["filters"] })?.filters }} loc={x.result as object} />
        </section>))}
    </details>
  );
}

// UI-DS.5.4 trust footer, one per answer: the tool row, "Checked against data" (or the masked/template state),
// "How is this calculated?" and model · tokens (figures from the server's `done` event, principle 7)
function Footer({ i, tools, onRetry }: { i: Extract<Item, { text: string }>; tools?: Tool[]; onRetry: () => void }) {
  const u = i.used;
  return (
    <div className="foot">
      {!!tools?.length && <div className="tool"><Tools names={tools} /></div>}
      <p className="trust">{i.flags ? t("chat.masked") : t(i.template ? "chat.template" : "chat.grounded")}</p>
      {i.flags && <Unverified flags={i.flags} onRetry={onRetry} />}
      {!!i.results?.length && <Calc results={i.results} />}
      {u && <p className="muted tokens">{[u.model, t(u.effort ? "chat.tokensEffort" : "chat.tokens", { prompt: u.prompt_tokens,
        completion: u.completion_tokens, reasoning: u.reasoning_tokens, effort: u.effort ? level(u.effort) : "" })].filter(Boolean).join(" · ")}</p>}
    </div>
  );
}

// UI-DS.6 (D-120): an answer's visuals as a report: one page, a visual per valid block, written by save_report under a
// fresh ID (never over an existing report); Editors and Owners only (SC-2), then Open report
function SaveAsReport({ project, specs }: { project: string; specs: ChatSpec[] }) {
  const me = useQuery(meQuery(project));
  const [saved, setSaved] = useState<{ id?: string; error?: string }>({});
  const id = useId();
  if (!canDesign(me.data)) return null;
  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const name = String(new FormData(e.currentTarget).get("name")).trim();
    try {
      const taken = new Set((await queryClient.fetchQuery(reportsQuery(project))).map((r) => r.id));
      const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^[^a-z]+/, "").slice(0, 50) || "report";
      let rid = base;
      for (let n = 2; taken.has(rid); n++) rid = `${base}-${n}`;
      const visuals = specs.map(({ type, ...query }, k) => ({ id: `visual-${k + 1}`, ...(type && { type }), query, layout: { x: 0, y: k * 4, w: 12, h: 4 } }));
      await post("save_report", { project, report: { id: rid, name, pages: [{ id: "page-1", name: t("chat.reportPage"), visuals }] } });
      await queryClient.invalidateQueries({ queryKey: ["reports"] });
      setSaved({ id: rid });
    } catch (err) {
      setSaved({ error: t("common.failed", { error: (err as Error).message }) });
    }
  }
  return (
    <details className="save-report">
      <summary>{t("chat.saveReport")}</summary>
      <form className="row" onSubmit={save}>
        <label htmlFor={`${id}-name`}>{t("chat.reportName")}</label>
        <input id={`${id}-name`} name="name" required maxLength={200} />
        <button type="submit" className="sm">{t("common.save")}</button>
      </form>
      {saved.id && <p role="status">{t("chat.reportSaved")} <button type="button" className="sm" autoFocus key={saved.id}
        onClick={() => useUi.getState().open(`${project}/#report/${saved.id}`)}>{t("chat.openReport")}</button></p>}
      {saved.error && <p role="alert" className="error">{saved.error}</p>}
    </details>
  );
}

export function Bubble({ i, tools, project, onRetry, onConfirm, onCancel = () => {} }: { i: Item; tools?: Tool[]; project?: string | null; onRetry: () => void; onConfirm: () => void; onCancel?: () => void }) {
  if (i.kind === "confirm")
    return (
      <li className={i.kind}>
        <p>{i.c.message}</p>
        {i.c.state ? <p role="status">{i.c.state}</p> : <div className="row">
          <button className="primary" onClick={onConfirm}>{t("common.confirm")}</button>
          <button type="button" className="sm" onClick={onCancel}>{t("common.cancel")}</button>
        </div>}
        {i.c.shown?.map((s, k) => <ShownValue key={k} s={s} first={k === 0} />)}
      </li>
    );
  if (i.kind === "file") return <FileItem link={i.link} />;
  if (i.kind === "error")
    return (
      <li className="error">
        <p role="alert">{i.text}</p>
        <button type="button" className="sm" onClick={onRetry}>{t("chat.retry")}</button>
      </li>
    );
  if (i.kind === "assistant") {
    const specs = visualBlocks(i.text).map(chatSpec).filter((s) => s !== null);
    return (
      <li className="assistant">
        <Md text={i.text} visual={(json) => <Suspense fallback={<p role="status" className="muted">{t("common.loading")}</p>}><ChatVisual project={project ?? null} json={json} /></Suspense>} />
        {project && specs.length > 0 && <SaveAsReport project={project} specs={specs} />}
        <Footer i={i} tools={tools} onRetry={onRetry} />
      </li>
    );
  }
  return <li className={i.kind}>{i.text}</li>;
}

type Tool = { text: string; step?: Step };
// UI-DS-0: tool calls show as ONE "Used N tools" line that opens to their names (and each step's status, D-117)
function Tools({ names }: { names: Tool[] }) {
  return (
    <details>
      <summary>{t("chat.usedTools", { count: names.length })}</summary>
      <ul>{names.map((n, k) => <li key={k}><code>{n.text}</code>{n.step && <span className="muted"> {stepText(n.step)}</span>}</li>)}</ul>
    </details>
  );
}

// AGENT-CONTEXT.4: while a turn runs, its latest tool step (or "Thinking…" before the first), announced politely;
// the role sits inside the <li> (a role on the li itself breaks the list for axe)
export function Running({ step }: { step: Step | null }) {
  return (
    <li className="busy"><span role="status" aria-live="polite"><span className="dots" aria-hidden><span /><span /><span /></span>
      {step ? <span className="live"><code>{step.tool}</code> {stepText(step)}</span> : <span className="sr-only">{t("chat.thinking")}</span>}</span></li>
  );
}

// a turn's tool calls while no answer follows them (still streaming, or ended in an error)
export function Steps({ names }: { names: Tool[] }) {
  return <li className="tool"><Tools names={names} /></li>;
}

// UI-DS.5.4: the tool calls of a turn go into its answer's footer (`tools`); those no answer follows yet stay one Steps
// row per run (`names`). `n` stays the item's index (Confirm updates by it).
export function rows(items: Item[]) {
  const tools = new Map<number, Tool[]>(); // answer index → its turn's tool calls
  const owned = new Set<number>(); // tool items an answer owns
  let answer: number | null = null;
  for (let n = items.length - 1; n >= 0; n--) {
    const i = items[n];
    if (i.kind === "assistant") answer = n, tools.set(n, []);
    else if (i.kind === "user") answer = null;
    else if (i.kind === "tool" && answer !== null) tools.get(answer)!.unshift(i), owned.add(n);
  }
  const out: { i: Item; n: number; names?: Tool[]; tools?: Tool[] }[] = [];
  items.forEach((i, n) => {
    const last = out[out.length - 1];
    if (owned.has(n)) return;
    if (i.kind === "tool" && last?.names) last.names.push(i);
    else out.push({ i, n, ...(i.kind === "tool" && { names: [i] }), ...(tools.has(n) && { tools: tools.get(n) }) });
  });
  return out;
}

// UI-1.2-fix-1, LLM-2-fix-10: a chat answer or a confirmed action may change any object (project, table, measure,
// report, ...), so every cached list is refetched
export const changed = () => queryClient.invalidateQueries();

type Llm = { aliases: Choice[]; main: string; providers?: LlmProvider[] };
// the ChatGPT plan model a chat tries first (this chat's `pick`, else the stored one), none when it goes to the workspace
const planModel = (gpt: ChatGptAccount | undefined, pick: string) =>
  !gpt?.linked || gpt.relink || pick.startsWith("ws:") ? null : pick.startsWith("gpt:") ? pick.slice(4) : gpt.model;
// the workspace alias a chat goes to (this chat's `pick`, else the Main Agent's; `auto`: small or large by question)
const wsAlias = (llm: Llm | undefined, pick: string) => (pick.startsWith("ws:") ? pick.slice(3) : llm?.main ?? "auto");

// D-71: the effort levels of the model this chat goes to first (the picked or stored ChatGPT plan model, else the picked
// or Main Agent's alias; for `auto` the levels both small and large declare, as routing picks one) and its default
export function chatLevels(gpt: ChatGptAccount | undefined, llm: Llm | undefined, pick = ""): Pick<Choice, "efforts" | "default"> | null {
  const plan = planModel(gpt, pick);
  if (plan) {
    const m = gpt!.models.find((x) => x.slug === plan);
    return m ? { efforts: m.efforts, default: m.default_effort } : null;
  }
  if (!llm) return null;
  const main = wsAlias(llm, pick);
  const picked = llm.aliases.filter((a) => (main === "auto" ? ["small", "large"] : [main]).includes(a.id));
  if (!picked.length) return null;
  return { efforts: picked[0].efforts.filter((e) => picked.every((a) => a.efforts.includes(e))),
    default: picked.length === 1 ? picked[0].default : null };
}

// MODEL-PICKER (AG-6 failover): the models this chat tries in order, as the server does: the ChatGPT plan model, then
// its backup (D-80), then every workspace provider holding the alias (`auto`: small or large; none holds it: the other)
export function chatOrder(gpt: ChatGptAccount | undefined, llm: Llm | undefined, pick = ""): string[] {
  const plan = planModel(gpt, pick);
  const name = (slug: string) => t("chat.planModel", { name: gpt?.models.find((m) => m.slug === slug)?.name ?? slug });
  const slugs = plan ? [...new Set([plan, gpt!.backup].filter((x): x is string => !!x))].map(name) : [];
  const alias = wsAlias(llm, pick);
  const holders = (as: string[]) => (llm?.providers ?? []).filter((p) => as.some((a) => a in p.models))
    .map((p) => `${p.id} · ${as.filter((a) => a in p.models).map((a) => p.models[a]).join(" / ")}`);
  const other = ({ small: "large", large: "small" } as Record<string, string>)[alias];
  const ws = alias === "auto" ? holders(["small", "large"]) : holders([alias]).length || !other ? holders([alias]) : holders([other]);
  return [...slugs, ...ws];
}

// UI-DS.5.2: open a saved chat on the Chats screen (the Chat loads it; the ☰ popover and ⌘K ask through the store)
export const openSaved = (id: number) => (useUi.setState({ chatReq: { open: id } }), useUi.getState().home());

// CHAT-PERSIST, UI-DS.5.2 (D-119 ☰): the header's recent chats, the person's own saved chats (the server lists no one
// else's), newest first: open, rename, delete
export function RecentChats() {
  const { project, chat } = useUi();
  const d = useRef<HTMLDetailsElement>(null);
  useDismiss(d);
  const [shown, setShown] = useState(false); // listed once first opened
  const [all, setAll] = useState(false);
  const here = !all && project ? project : null;
  const list = useQuery({ ...chatsQuery(here), enabled: shown });
  const [renaming, setRenaming] = useState<number | null>(null);
  const [back, setBack] = useState<number | null>(null); // the Rename button that takes focus back when its form closes
  const [del, setDel] = useState<{ id: number; message: string; token: string } | null>(null);
  const [error, setError] = useState("");
  const reload = () => queryClient.invalidateQueries({ queryKey: ["chats"] });
  const name = (c: ChatRow) => c.title || t("chat.new");

  async function rename(e: FormEvent<HTMLFormElement>, id: number) {
    e.preventDefault();
    try {
      await post("rename_chat", { id, title: String(new FormData(e.currentTarget).get("title")).trim() });
      setRenaming(null), setBack(id), setError(""), reload();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  // §5 #3: the server's 409 preview and token go to the Confirm click only
  async function remove(id: number, token?: string) {
    const r = await confirmable("delete_chat", { id }, token);
    if (r.confirm) return setDel({ id, message: r.confirm.message, token: r.confirm.token });
    setDel(null);
    if (!r.ok) return setError(r.error);
    setError(""), useUi.setState({ chatReq: { drop: id } }), reload(), focusSoon("chat-scope");
  }

  return (
    <details className="chats" ref={d} onToggle={(e) => e.currentTarget.open && setShown(true)}>
      <summary id="recent-chats" className="ghost" aria-label={t("chat.recent")} title={t("chat.recent")}><Icon name="menu" /></summary>
      <div className="chats-pop">
        <h2 className="sr-only">{t("chat.recent")}</h2>
        <label className="scope">{t("chat.show")}{" "}
          <select id="chat-scope" value={here ? "project" : "all"} onChange={(e) => setAll(e.target.value === "all")}>
            {project && <option value="project">{t("chat.thisProject")}</option>}
            <option value="all">{t("chat.allChats")}</option>
          </select>
        </label>
        {error && <p role="alert">{t("common.failed", { error })}</p>}
        {list.data?.chats.length === 0 && <p className="muted">{t("chat.none")}</p>}
        <ul>
          {list.data?.chats.map((c) => (
            <li key={c.id}>
              {renaming === c.id ? (
                <form onSubmit={(e) => rename(e, c.id)}>
                  <label className="sr-only" htmlFor="chat-title">{t("chat.name")}</label>
                  <input id="chat-title" name="title" defaultValue={c.title} maxLength={80} required autoFocus />
                  <button type="submit" className="sm primary">{t("common.save")}</button>
                  <button type="button" className="sm" onClick={() => { setRenaming(null); setBack(c.id); }}>{t("common.cancel")}</button>
                </form>
              ) : (
                <>
                  <button type="button" className="link" aria-current={c.id === chat.id ? "true" : undefined} disabled={chat.busy}
                    onClick={() => ((d.current!.open = false), openSaved(c.id))}>
                    <span className="title">{name(c)}</span> <span className="muted">{fmtDate(c.updated, { dateStyle: "short" })}</span>
                  </button>
                  <span className="acts">
                    <button type="button" className="sm" id={`chat-rename-${c.id}`} autoFocus={back === c.id} aria-label={t("chat.renameLabel", { title: name(c) })} onClick={() => setRenaming(c.id)}>{t("chat.rename")}</button>
                    <button type="button" className="sm" id={`chat-delete-${c.id}`} aria-label={t("chat.deleteLabel", { title: name(c) })}
                      disabled={chat.busy && c.id === chat.id} onClick={() => remove(c.id)}>{t("chat.delete")}</button>
                  </span>
                </>
              )}
              {del?.id === c.id && (
                <ConfirmBox label={t("chat.confirmDelete")} message={del.message} ok={t("common.confirmDelete")} onConfirm={() => remove(c.id, del.token)}
                  onCancel={() => setDel(null)} back={`chat-delete-${c.id}`} />
              )}
            </li>
          ))}
        </ul>
      </div>
    </details>
  );
}

// UI-DS.5.1 (D-119) the empty chat's task-first starters: each fills the composer (nothing is sent) or opens its screen.
// A Viewer can only ask (SC-2: no loading, reports, flows or models; their shared items are in the sidebar).
export const starters = (viewer: boolean) => ([
    ["ask", "message-square", t("home.ask"), t("home.askHint"), () => prefill(t("home.askQ"))],
    ["data", "plug", t("home.data"), t("home.dataHint"), () => openDialog("data")],
    ["report", "layout-dashboard", t("home.report"), t("home.reportHint"), () => prefill(t("home.reportQ"))],
    ["automate", "workflow", t("home.automate"), t("home.automateHint"), () => prefill(t("home.automateQ"))],
    ["predict", "brain-circuit", t("home.predict"), t("home.predictHint"), () => prefill(t("home.predictQ"))],
    ["share", "share", t("home.share"), t("home.shareHint"), () => (useUi.getState().open("*/@sharing"), focusSoon("ws-*/@sharing"))],
  ] as [string, IconName, string, string, () => void][]).filter(([id]) => !viewer || id === "ask");

export function Home({ viewer }: { viewer: boolean }) {
  const user = useUi((s) => s.user);
  return (
    <section className="home" aria-labelledby="home-h">
      <h2 id="home-h">{user ? t("home.titleOf", { name: user }) : t("home.title")}</h2>
      <h3 id="home-tasks">{t("home.tasks")}</h3>
      <ul className="starters" aria-labelledby="home-tasks">
        {starters(viewer).map(([id, icon, label, hint, go]) => (
          <li key={id}><button type="button" id={`start-${id}`} onClick={go}><Icon name={icon} /><span><b>{label}</b><span>{hint}</span></span></button></li>
        ))}
      </ul>
    </section>
  );
}

// a picked table's name: its table name in the switcher's project, else "project/table"
const tableLabel = (tab: string, project: string | null) => (project && tab.startsWith(project + "/") ? tab.slice(project.length + 1) : tab);

// UI-DS.5.1 Database: the switcher's project's tables (no project: every table) with search; picked ones become chips
function Database() {
  const { project, chatTables } = useUi();
  const all = useQuery(tablesQuery).data ?? [];
  const [q, setQ] = useState("");
  const d = useRef<HTMLDetailsElement>(null);
  useDismiss(d);
  const mine = all.filter((tab) => !project || tab.startsWith(project + "/"));
  const shown = mine.filter((tab) => tab.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <details className="db" ref={d}>
      <summary className="ctool" aria-label={t("chat.dbLabel", { count: chatTables.length })}>
        <Icon name="database" /><span>{t("chat.db")}</span>{chatTables.length > 0 && <span className="badge" aria-hidden>{chatTables.length}</span>}
      </summary>
      <div className="db-pop">
        <label className="sr-only" htmlFor="chat-db-search">{t("chat.dbSearch")}</label>
        <input id="chat-db-search" type="search" value={q} placeholder={t("chat.dbSearch")} autoComplete="off" onChange={(e) => setQ(e.target.value)} />
        {mine.length === 0 && <p className="muted">{t("chat.dbNone")}</p>}
        <ul aria-label={t("chat.dbTables")}>
          {shown.map((tab) => (
            <li key={tab}><label><input type="checkbox" checked={chatTables.includes(tab)} onChange={(e) => pickTable(tab, e.target.checked)} />{" "}
              <span className="mono">{tableLabel(tab, project)}</span></label></li>
          ))}
        </ul>
      </div>
    </details>
  );
}

// the picked tables above the message box; a removed chip's focus goes to the chip before it, else the message box (WCAG 2.4.3)
export function Chips() {
  const { project, chatTables } = useUi();
  if (!chatTables.length) return null;
  return (
    <ul className="chips" aria-label={t("chat.context")}>
      {chatTables.map((tab, k) => (
        <li key={tab} className="chip"><Icon name="database" /><span>{tableLabel(tab, project)}</span>
          <button type="button" id={`chip-${k}`} aria-label={t("chat.chipRemove", { table: tableLabel(tab, project) })}
            onClick={() => (pickTable(tab, false), focusSoon(chatTables.length > 1 ? `chip-${Math.max(0, k - 1)}` : "chat-input"))}><Icon name="x" /></button>
        </li>
      ))}
    </ul>
  );
}

// UI-DS.5.3: the Ask about this drawer's context: the open screen while its chip shows, else none
export const askContext = (chip: string | null) => (chip ? { tab: useUi.getState().active, screen: chip } : {});

// `screen` (the open screen as a breadcrumb line) makes this the Ask about this drawer's own chat: the screen is a
// removable chip, no saved-chats list, home, tables or model pick (the stored defaults apply), ids prefixed "ask-"
export function Chat({ viewer = false, screen }: { viewer?: boolean; screen?: string }) {
  const drawer = screen !== undefined;
  const pre = drawer ? "ask" : "chat"; // the id prefix
  const [off, setOff] = useState<string | null>(null); // the screen whose chip was removed; another screen shows its chip again
  const chip = drawer && screen && off !== screen ? screen : null;
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState<Step | null>(null); // the running turn's latest tool step (D-117 item 4)
  const [effort, setEffort] = useState(""); // this chat's override; "": the stored or default effort
  const [pick, setPick] = useState(""); // this chat's model (MODEL-PICKER); "": the stored pick or the workspace default
  const [current, setCurrent] = useState<number | null>(null); // the open chat's saved id
  const [problem, setProblem] = useState("");
  const own = useRef<Saved>({ id: null }); // the open chat: a new object per chat
  const uiProject = useUi((s) => s.project); // a chat's visuals read its own project once saved (UI-DS.6)
  const dirty = useRef(false); // asked or confirmed since the last save
  const touched = useRef(false); // the person asked or picked a chat: the newest one is no longer opened for them
  const gpt = useQuery(chatgptQuery).data;
  const llm = useQuery(aliasesQuery).data;
  const levels = chatLevels(gpt, llm, pick);
  const order = chatOrder(gpt, llm, pick);
  const plans = gpt?.linked && !gpt.relink ? gpt.models : [];
  const add = (i: Item) => setItems((xs) => [...xs, i]);

  // CHAT-PERSIST: save the question before the answer streams (a reload then shows it interrupted), and the chat
  // after each answer or Confirm click. The chat's project is the one current at its first save.
  useEffect(() => {
    if (!dirty.current || (busy && items[items.length - 1]?.kind !== "user")) return;
    if (!busy) dirty.current = false;
    const c = own.current;
    saveChat(c, items).then(() => {
      if (own.current === c) setCurrent(c.id), setProblem("");
      queryClient.invalidateQueries({ queryKey: ["chats"] });
    }, (err) => setProblem(t("chat.notSaved", { error: (err as Error).message })));
  }, [items, busy]);

  async function open(id: number, auto = false) {
    try {
      const h = await history(id);
      if (auto && touched.current) return;
      touched.current = true;
      own.current = { id: h.id, project: h.project };
      dirty.current = false;
      setItems(reopened(h.items)), setCurrent(h.id), setProblem(""), useUi.setState({ chatTables: [] });
      if (!auto) focusSoon(`${pre}-input`);
    } catch (err) {
      if (!auto) setProblem(t("common.failed", { error: (err as Error).message }));
    }
  }

  useEffect(() => { // a reload or sign-in reopens the person's newest chat (in the home chat; the drawer starts empty)
    if (!drawer) post<{ chats: ChatRow[] }>("list_chats", {}).then(({ chats }) => chats[0] && open(chats[0].id, true), () => {});
  }, []);

  // New chat: empty, and the LLM gets none of the earlier conversation (it only ever gets the open chat's items)
  function newChat(focus = true) {
    touched.current = true;
    own.current = { id: null };
    dirty.current = false;
    setItems([]), setCurrent(null), setProblem("");
    if (!drawer) useUi.setState({ chatTables: [] }); // the home chat's picked tables
    if (focus) focusSoon(`${pre}-input`);
  }
  const newChats = useUi((s) => s.newChats); // the header's New chat (UI-DS.2), the home chat's
  useEffect(() => void (newChats && !busy && !drawer && newChat()), [newChats]); // like #chat-new, not while an answer streams
  // UI-DS.5.2: the header's recent chats and ⌘K read the home chat and ask it to open one or leave a deleted one; the
  // drawer only leaves its own chat when that one is deleted or opened there (`moves`)
  useEffect(() => void useUi.setState(drawer ? { askChat: { id: current, busy } } : { chat: { id: current, busy } }), [current, busy]);
  const req = useUi((s) => s.chatReq);
  useEffect(() => {
    if (!req) return;
    if (!drawer) useUi.setState({ chatReq: null });
    if ("open" in req) {
      const { chat, askChat } = useUi.getState();
      const m = moves(req.open, drawer, chat, askChat);
      if (m === "open") open(req.open);
      else if (m === "leave") newChat(false);
    } else if (own.current.id === req.drop) newChat(false);
  }, [req]);

  // Asks `text` after the conversation `base` (Retry passes the items before the last question).
  async function ask(text: string, base: Item[]) {
    if (!text || busy) return;
    const asked: Item[] = [...base, { kind: "user", text }];
    touched.current = dirty.current = true;
    setItems(asked);
    setBusy(true), setLive(null);
    let answered = false;
    const results: Result[] = [];
    try {
      await postChat(chatHistory(asked), (event, data) => {
        if (event === "tool_result" && "result" in data) results.push({ name: data.name, result: data.result, ...(data.how && { how: data.how }) });
        if (event === "status") { // onto the turn's last tool item, for the footer once the answer is in
          setLive(data);
          setItems((xs) => {
            const j = xs.map((x) => x.kind).lastIndexOf("tool");
            return xs.map((x, k) => (k === j ? { ...x, step: data } as Item : x));
          });
        }
        if (event === "done") {
          const { ctx, ...used } = data;
          setItems((xs) => xs.map((x, k) => (k === xs.length - 1 && x.kind === "assistant" ? { ...x, used, ...(ctx && { ctx }) } : x)));
        }
        const i = toItem(event, data);
        if (i) (answered ||= i.kind !== "tool" && i.kind !== "file"), add(i.kind === "assistant" && results.length ? { ...i, results } : i);
      }, levels?.efforts.includes(effort) ? effort : "", pick, drawer ? askContext(chip) : undefined);
      if (!answered) add({ kind: "error", text: t("chat.noAnswer") });
    } catch (err) {
      add({ kind: "error", text: (err as Error).message });
    } finally {
      setBusy(false);
      changed();
    }
  }

  function send(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const text = String(new FormData(form).get("message")).trim();
    if (!text || busy) return;
    form.reset();
    ask(text, items);
  }

  function retry() {
    const k = items.map((i) => i.kind).lastIndexOf("user");
    if (k < 0 || busy) return;
    focusSoon(`${pre}-input`); // the Retry button is gone with its bubble
    ask((items[k] as { text: string }).text, items.slice(0, k));
  }

  async function confirm(n: number, c: Confirm) {
    const next = afterConfirm(c, await confirmable(c.action, c.input, c.confirm_token));
    dirty.current = true;
    setItems((xs) => xs.map((i, k) => (k === n ? { kind: "confirm", c: next } : i)));
    // the Confirm button is gone: a shown value takes focus (autoFocus), else the composer; the status is announced
    if (next.state && !next.shown?.length) focusSoon(`${pre}-input`);
    changed();
  }

  function cancel(n: number, c: Confirm) { // no request: the token is dropped, the saved chat keeps "cancelled"
    dirty.current = true;
    setItems((xs) => xs.map((i, k) => (k === n ? { kind: "confirm", c: cancelled(c) } : i)));
    focusSoon("chat-input");
  }

  return (
    <section className="chat" aria-label={drawer ? undefined : t("chat.label")}>
      <div className="chat-top">
        <button type="button" id={`${pre}-new`} className="sm" disabled={busy} onClick={() => newChat()}>{t("chat.new")}</button>
      </div>
      <div role="log" aria-live="polite" className="log"><ol>
        {rows(items).map(({ i, n, names, tools }) => names ? <Steps key={n} names={names} />
          : <Bubble key={n} i={i} tools={tools} project={own.current.project === undefined ? uiProject : own.current.project} onRetry={retry} onConfirm={() => i.kind === "confirm" && confirm(n, i.c)}
            onCancel={() => i.kind === "confirm" && cancel(n, i.c)} />)}
        {interrupted(items, busy) && (
          <li className="error interrupted">
            <p>{t("chat.interrupted")}</p>
            <button type="button" className="sm" onClick={retry}>{t("chat.retry")}</button>
          </li>
        )}
        {busy && <Running step={live} />}
      </ol></div>
      {items.length === 0 && (drawer ? <p className="muted ask-empty">{chip ? t("ask.empty", { screen: chip }) : t("ask.emptyNone")}</p> : <Home viewer={viewer} />)}
      {problem && <p role="alert" className="problem">{problem}</p>}
      <form onSubmit={send} className="composer">
        {!drawer ? <Chips /> : chip && (
          <ul className="chips" aria-label={t("ask.context")}>
            <li className="chip"><Icon name="layout-dashboard" /><span>{chip}</span>
              <button type="button" id="ask-chip" aria-label={t("ask.chipRemove", { screen: chip })} onClick={() => (setOff(screen!), focusSoon("ask-input"))}><Icon name="x" /></button>
            </li>
          </ul>
        )}
        <label className="sr-only" htmlFor={`${pre}-input`}>{t("chat.message")}</label>
        <textarea id={`${pre}-input`} name="message" rows={2} placeholder={drawer ? t("ask.placeholder") : t("chat.placeholder")} aria-describedby={`${pre}-hint`}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && (e.preventDefault(), e.currentTarget.form?.requestSubmit())} />
        <div className="composer-row">
          {!drawer && <>
          {/* IN-7: the file goes through the Add data dialog's upload; the loaded table comes back as a chip */}
          {!viewer && <button type="button" id="chat-attach" className="ctool" aria-label={t("chat.attach")} title={t("chat.attach")}
            onClick={() => useUi.setState({ dialog: "data", attach: true })}><Icon name="paperclip" /></button>}
          <Database />
          {(plans.length > 0 || !!llm?.aliases.length) && (
            <label className="effort">{t("chat.model")}{" "}
              <select id="chat-model" value={pick} aria-describedby={order.length ? "chat-order" : undefined}
                onChange={(e) => (setPick(e.target.value), setEffort(""))}>
                <option value="">{t("chat.modelDefault")}</option>
                {plans.length > 0 && <optgroup label={t("chat.modelPlan")}>
                  {plans.map((m) => <option key={m.slug} value={`gpt:${m.slug}`}>{m.name}</option>)}
                </optgroup>}
                {!!llm?.aliases.length && <optgroup label={t("chat.modelWorkspace")}>
                  {llm.aliases.map((a) => <option key={a.id} value={`ws:${a.id}`}>{a.name}</option>)}
                </optgroup>}
              </select>
            </label>
          )}
          {!!levels?.efforts.length && (
            <label className="effort">{t("chat.effort")}{" "}
              <select id="chat-effort" value={effort} onChange={(e) => setEffort(e.target.value)}>
                <option value="">{t("chat.effortDefault")}</option>
                {levels.efforts.map((e) => <option key={e} value={e}>{levelText(e, levels.default)}</option>)}
              </select>
            </label>
          )}
          </>}
          <button type="submit" className="primary send" disabled={busy}>{t("chat.send")}</button>
        </div>
        {!drawer && order.length > 0 && <p id="chat-order" className="muted order">{t("chat.order", { models: order.join(" → ") })}</p>}
        <p id={`${pre}-hint`} className="muted hint">{t("chat.hint")}</p>
      </form>
    </section>
  );
}
