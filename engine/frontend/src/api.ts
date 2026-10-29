import { QueryClient } from "@tanstack/react-query";
import { t } from "./i18n";
import { focusSoon, useUi } from "./store";
import type { Model } from "./Model";
import type { Choice } from "./Effort";

export const queryClient = new QueryClient();

// SC-1 sign-out and session expiry: drop cached data, open tabs and the token so the next person on this
// browser sees nothing of the previous user, then focus the sign-in name (WCAG 2.4.3).
export function endSession(notice: string | null = null) {
  queryClient.clear();
  useUi.setState({ tabs: [], active: null, project: null, notice });
  useUi.getState().signIn(null);
  focusSoon("signin-name");
}

// SC-1 sign-out: revoke the session server-side, then clear locally; an OIDC session also ends at the provider
export async function logout() {
  const out = await api<{ end_session_url?: string }>("/api/logout", { method: "POST" });
  endSession();
  if (out?.end_session_url) location.assign(out.end_session_url);
}

// SC-1 OIDC: providers for the sign-in buttons (404 while ABI_PUBLIC_URL is unset: none)
export const providersQuery = {
  queryKey: ["oidc-providers"],
  queryFn: () => api<{ id: string; name: string }[]>("/api/oidc/providers").catch(() => []),
};

// D-98 AGPL §13: the running version's source, offered on the sign-in page and in the Account menu
export const versionQuery = {
  queryKey: ["version"],
  queryFn: () => api<{ version: string; source: string }>("/api/version"),
  staleTime: Infinity,
};

// Starts a sign-in (or, signed in, links this account): the API sets the browser cookie and returns the IdP URL.
export async function startOidc(provider: string) {
  location.assign((await api<{ url: string }>(`/api/oidc/${provider}/start`, { method: "POST" })).url);
}

// Fixed texts (`signin.notice.<code>`) for the callback's `?signin=<code>` (E-01 decision 7: nothing from the IdP is shown).
const SIGNIN = ["linked", "unknown", "conflict", "expired", "denied", "browser"];

// The return from the IdP (main.tsx, before the first render): clear `?signin=`, redeem `ok` once for the session
// token, show any other outcome as a notice (focus on the name field when signed out).
export async function signinReturn() {
  const code = new URLSearchParams(location.search).get("signin");
  if (code === null) return;
  history.replaceState(null, "", location.pathname + location.hash);
  if (code === "ok") {
    try {
      const s = await api<{ token: string; name: string }>("/api/oidc/session", { method: "POST" });
      return useUi.getState().signIn(s.token, s.name);
    } catch {} // falls through to the generic message
  }
  useUi.setState({ notice: t(SIGNIN.includes(code) ? `signin.notice.${code}` : "signin.notice.failed") });
  // the name field takes focus via autoFocus while a notice shows: this runs before the first render (main.tsx)
}

export type Page = { columns: { name: string; type: string }[]; total: number; rows: (string | null)[][] };
// DuckDB column types that hold numbers (grid alignment, filter values)
export const NUMERIC = /INT|DECIMAL|DOUBLE|FLOAT|REAL|NUMERIC/i;

export async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const token = useUi.getState().token;
  const r = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
  });
  // Expired or revoked session; a stale request from an earlier session must not end a newer one.
  if (r.status === 401 && token && useUi.getState().token === token) endSession(t("signin.notice.ended"));
  return r;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = await call(path, init);
  const body = await r.json().catch(() => null);
  if (!r.ok) throw failure(r, body);
  return body as T;
}

export function failure(r: Response, body: any) {
  const d = body?.detail;
  // pydantic errors carry loc/msg; PC-3 import checks (save_report) carry file, JSON path, error and fix
  type Err = { loc?: unknown[]; msg?: string; file?: string; path?: string; error?: string; fix?: string };
  const why = (d?.errors ?? []).map((e: Err) => (e.loc ? ` ${e.loc.join(".")}: ${e.msg}.` : ` ${e.file} ${e.path}: ${e.error} ${e.fix}`)).join("");
  // `status` lets a caller tell a WB-7 "changed since you read it" (409) from a bad value (400)
  return Object.assign(new Error(typeof d === "string" ? d : (d?.message ?? r.statusText) + why), { status: r.status });
}

// §5 #3 / UI-5: what the confirmation of a destructive or confirms=True action says: a translated sentence per action
// (never built from its ID); `{details}` keeps the server's own preview text (who gains access, what goes with it).
// backend tests/test_actions.py lists every such action against this table and en.json.
const CONFIRMS: Record<string, (details: string) => string> = {
  restore_snapshot: () => t("confirm.restore_snapshot"),
  delete_project: (details) => t("confirm.delete_project", { details }),
  rename_project: (details) => t("confirm.rename_project", { details }),
  delete_secret: () => t("confirm.delete_secret"),
  delete_llm_provider: () => t("confirm.delete_llm_provider"),
  forget: () => t("confirm.forget"),
  remove_relationship: () => t("confirm.remove_relationship"),
  delete_glossary_term: () => t("confirm.delete_glossary_term"),
  delete_dataset: () => t("confirm.delete_dataset"),
  delete_measure: () => t("confirm.delete_measure"),
  delete_flow: () => t("confirm.delete_flow"),
  delete_ml_model: () => t("confirm.delete_ml_model"),
  use_ml_trial: (details) => t("confirm.use_ml_trial", { details }),
  delete_tool: () => t("confirm.delete_tool"),
  delete_table: (details) => t("confirm.delete_table", { details }),
  delete_source: (details) => t("confirm.delete_source", { details }),
  delete_report: () => t("confirm.delete_report"),
  set_sharing: (details) => t("confirm.set_sharing", { details }),
  delete_tenant: () => t("confirm.delete_tenant"),
  set_project_tenant: (details) => t("confirm.set_project_tenant", { details }),
  set_delivery: (details) => t("confirm.set_delivery", { details }),
  delete_agent: () => t("confirm.delete_agent"),
  delete_oidc_provider: () => t("confirm.delete_oidc_provider"),
  delete_chat: () => t("confirm.delete_chat"),
  remove_role: () => t("confirm.remove_role"),
  delete_user: () => t("confirm.delete_user"),
  set_workspace_owner: () => t("confirm.set_workspace_owner"),
  reset_mfa: () => t("confirm.reset_mfa"),
};
export const confirmText = (action: string, server: string) => (Object.hasOwn(CONFIRMS, action) ? CONFIRMS[action](server) : server);

// MA-3 / WB-9: an action that may ask for a confirmation. A 409 with a confirm_token comes back as `confirm` (the
// server's preview and token) for a Confirm button; the token goes back only on that click, never to an agent.
export async function confirmable(action: string, body: unknown, token?: string) {
  let r: Response;
  try {
    r = await call(`/api/actions/${action}`, { method: "POST", body: JSON.stringify(body), headers: token ? { "X-ABI-Confirm": token } : {} });
  } catch (e) { // the request never got an answer (network down, server gone): say so instead of nothing
    return { ok: false, status: 0, json: null, confirm: null, error: `Couldn't reach the server: ${(e as Error).message}. Reload to see whether it went through.` };
  }
  const json = await r.json().catch(() => null);
  const d = json?.detail;
  const confirm = r.status === 409 && d?.confirm_token ? { message: confirmText(action, d.message), token: d.confirm_token as string, preview: d.preview } : null;
  return { ok: r.ok, status: r.status, json, confirm, error: failure(r, json).message }; // with any PC-3/pydantic errors
}

// The artifact route (/api/artifacts/...) needs the bearer session, so a plain href can't fetch it: save it through `call`.
export async function download(link: string, name = link.split("/").pop()) {
  const r = await call(link);
  if (!r.ok) throw new Error(r.status === 404 ? "it no longer exists" : r.statusText);
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(await r.blob()), download: name });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href));
}

export const post = <T,>(action: string, body: unknown) => api<T>(`/api/actions/${action}`, { method: "POST", body: JSON.stringify(body) });

// SC-2 your role in a project (every tab shares this cache entry); editors and owners may change definitions and data.
export type Me = { role: string | null; production?: boolean };
export const meQuery = (project: string) => ({ queryKey: ["me", project], queryFn: () => api<Me>(`/api/projects/${project}/me`) });
export const canEdit = (role?: string | null) => role === "editor" || role === "owner";
// D-56: a production environment is view-only (write-back keeps canEdit, D-57); D-60: access admin is the Owner's there
export const canDesign = (me?: Me) => canEdit(me?.role) && !me?.production;
export const canShare = (me?: Me) => (me?.production ? me.role === "owner" : canEdit(me?.role));
// SC-3 row-level rules: every rule for Owners/Editors (Owners also get the accounts to pick from), own values only when restricted
type RlsValue = string | number | boolean;
export type RlsRule = { id: string; table: string; column: string; users: { id: string; name: string; values: RlsValue[] }[] };
// D-110 column rules hide whole columns from Viewers; a restricted Viewer's copy has no `id`
export type ColumnRule = { id?: string; table: string; columns: string[] };
export type RlsRules = { restricted: boolean; rules: (RlsRule | { table: string; column: string; values: RlsValue[] })[];
  columns?: ColumnRule[]; accounts?: { id: string; name: string; role: string | null }[] };
export const rlsQuery = (project: string) => ({ queryKey: ["rls_rules", project], queryFn: () => post<RlsRules>("rls_rules", { project }) });
// SE-1 the semantic model (tool, agent and report tabs share this cache entry)
export const modelQuery = <T,>(project: string) => ({ queryKey: ["semantic_model", project], queryFn: () => post<T>("semantic_model", { project }) });

// Copies to the clipboard; false when the browser refuses (no permission, insecure context). Callers say which.
export async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// UI-2 the model file as the Model tab draws it (Model tab and table grid share this cache entry)
export const lakeModelQuery = (project: string) => ({ queryKey: ["model", project], queryFn: () => api<Model>(`/api/projects/${encodeURIComponent(project)}/model`) });

// UI-2 measures of a project: a select over the shared semantic_model entry (one request per project).
export type MeasureSpec = { id: string; name: string; expression: string; description?: string };
export const measuresQuery = (project: string) => ({
  ...modelQuery<{ measures: MeasureSpec[] }>(project), select: (m: { measures: MeasureSpec[] }) => m.measures,
});

// TL-1 tools of a project (Data tab and tool tabs share this cache entry), with the TL-2 built-ins.
export type ToolSpec = { id: string; name: string; description: string; version: number; status: "draft" | "published"; disabled: boolean; error: string | null;
  kind?: "query" | "calculation" | "api"; code?: string };
export const toolsQuery = (project: string) => ({
  queryKey: ["tools", project],
  queryFn: () => post<{ tools: ToolSpec[]; builtins: { id: string; description: string }[] }>("list_tools", { project }),
});
// TL-4: a published tool whose tests fail is disabled, with the last failure (AG-4: agents the same).
export const toolStatus = (o: { disabled?: boolean; error?: string | null; status?: string; version?: number }) =>
  o.disabled ? t("status.disabled", { error: o.error ?? "" }) : o.status === "published" ? t("status.published", { version: String(o.version) }) : t("status.draft");

// FL-1 flows of a project (Data tab and flow tabs share this cache entry), with each flow's latest run.
// FL-1/FL-2/AT-2 one condition shape: holds when any result row's `measure` value satisfies `op value` (the server checks it)
export type Op = ">" | ">=" | "<" | "<=" | "=" | "!=";
export type Condition = { query: { measures: string[]; dimensions?: string[]; filters?: unknown[]; [k: string]: unknown }; measure: string; op: Op; value: number };
export type Trigger = { type: "manual" } | { type: "schedule"; cron: string; timezone: string } | { type: "after_refresh" | "writeback"; tables: string[] }
  | ({ type: "data_condition"; cooldown_minutes?: number } & Condition) | { type: "webhook" }
  | { type: "message"; channel: "email" | "slack" | "teams"; address?: string; channel_id?: string } | { type: "data_quality"; tables?: string[] };
export type FlowSpec = { id: string; name: string; owner?: string; enabled: boolean; trigger: Trigger; steps: { id: string; type: string; [k: string]: unknown }[] };
export type LastRun = { status: "queued" | "running" | "ok" | "failed" | "stopped" | "waiting" | "rejected"; error: string | null; created: string; finished: string | null };
export type Tokens = { prompt_tokens: number; completion_tokens: number }; // OB-1 per task (flow) and per run
export const flowsQuery = (project: string) => ({
  queryKey: ["flows", project],
  queryFn: () => post<{ flows: FlowSpec[]; last_runs: Record<string, LastRun>; tokens: Record<string, Tokens> }>("list_flows", { project }),
});
const conditionText = (c: Condition) =>
  t("trigger.cond", { measure: c.measure, op: c.op, value: c.value }) + (c.query.dimensions?.length ? t("trigger.forAny", { dimensions: c.query.dimensions.join(", ") }) : "")
  + (c.query.filters?.length ? t("trigger.filtered") : "");
export const triggerText = (g: Trigger) =>
  g.type === "schedule" ? t("trigger.schedule", { cron: g.cron, timezone: g.timezone }) : g.type === "after_refresh" ? t("trigger.afterRefresh", { tables: g.tables.join(", ") })
  : g.type === "writeback" ? t("trigger.writeback", { tables: g.tables.join(", ") })
  : g.type === "data_condition" ? t("trigger.condition", { condition: conditionText(g), minutes: g.cooldown_minutes ?? 1440 })
  : g.type === "webhook" ? t("trigger.webhook")
  : g.type === "data_quality" ? (g.tables ? t("trigger.quality", { tables: g.tables.join(", ") }) : t("trigger.qualityAny"))
  : g.type === "message" ? t("trigger.message", { channel: g.channel, to: g.address ?? g.channel_id ?? "" }) : t("trigger.manual");
// FL-5: a flow whose latest run failed is failing, with that run's error; FL-1: a paused run waits for approval.
export const flowStatus = (f: FlowSpec, last?: LastRun) =>
  t("flow.status", { state: f.enabled ? t("flow.enabled") : t("flow.disabled"), last: !last ? t("flow.neverRun") : last.status === "failed" ? t("flow.failing", { error: last.error ?? "" })
    : t(`flow.last.${last.status}`) });

// AG-1 custom agents of a project (Data tab and agent tabs share this cache entry), each with its eval cases and AG-4 state.
export type Channel = { type: "in_app" | "email" | "slack" | "teams" | "api" | "mcp"; address?: string; channel?: string; reply?: string };
export type EvalCase = { question: string; expect: { contains: (string | number)[]; tools?: string[] } };
export type AgentSpec = {
  id: string; name: string; purpose: string; instructions: string; version?: number; status?: "draft" | "published"; owner?: string;
  scope: { tables?: string[]; measures?: string[]; datasets?: string[]; glossary?: string[] }; tools: string[];
  mode: "tools_only" | "exploratory"; grounding: boolean; channels: Channel[]; disabled?: boolean; error?: string | null; cases?: EvalCase[];
  model?: string; // AG-6: a provider alias or "auto"
  agents?: string[]; // AGENT-AS-TOOL: other published agents it may ask
  effort?: string; // D-71: its reasoning effort (none: the alias default)
};
// AG-6: the model aliases of the workspace LLM providers, in failover order without repeats (none: only Automatic), with
// the effort levels any provider declares for the alias and the first declared default (D-71); `main`: the Main Agent's;
// `providers`: the rows themselves (key references only, SC-4) for the AI providers screen and the chat's fallback order;
// base_url/api_key/prices come only to workspace owners (H-01.3-fix-7), the only ones who open that screen.
export type LlmProvider = {
  id: string; position: number; base_url: string; api_key: string | null; models: Record<string, string>;
  prices: Record<string, unknown>; efforts: Record<string, { levels: string[]; default: string | null }>;
};
type Providers = { providers: LlmProvider[]; main_model: string };
export const aliasesQuery = {
  queryKey: ["llm-aliases"],
  queryFn: async () => {
    const d = await post<Providers>("list_llm_providers", {});
    const aliases = [...new Set(d.providers.flatMap((p) => Object.keys(p.models)))].map((id): Choice => {
      const declared = d.providers.flatMap((p) => (id in p.models && p.efforts?.[id] ? [p.efforts[id]] : []));
      return { id, name: id, efforts: [...new Set(declared.flatMap((e) => e.levels))], default: declared.find((e) => e.default)?.default ?? null };
    });
    return { aliases, main: d.main_model, providers: d.providers };
  },
};
// AG-6 / D-53 "Sign in with ChatGPT": only the link state, never a token (SC-4); per plan model its effort levels (D-71).
export type ChatGptAccount = {
  available: boolean; linked: boolean; email: string | null; model: string | null; backup?: string | null; effort: string | null; relink: boolean;
  models: { slug: string; name: string; efforts: string[]; default_effort: string | null }[]; error?: string;
};
export const chatgptQuery = { queryKey: ["chatgpt"], queryFn: () => post<ChatGptAccount>("chatgpt_account", {}) };
export const agentsQuery = (project: string) => ({
  queryKey: ["agents", project],
  queryFn: async () => (await post<{ agents: AgentSpec[] }>("list_agents", { project })).agents,
});
// §3: custom agents need a Ready semantic model; `missing` lists the gaps.
export const readinessQuery = (project: string) => ({
  queryKey: ["readiness", project],
  queryFn: () => post<{ ready: boolean; missing: string[] }>("readiness", { project }),
});

// ML-4 ML models of a project with their latest training and scoring run (Data tab and ML tabs share this cache entry).
// Every number here is a registry figure the platform's code wrote (principle 7); the UI only formats it.
export type MlRun = {
  run: string; kind: "train" | "score"; status: "queued" | "running" | "ok" | "failed"; created: string; finished: string | null;
  method: string | null; metrics: Record<string, number> | null; baseline: Record<string, number> | null; better_than_baseline: boolean | null;
  importance: { feature: string; name: string; share: number }[] | null; excluded: string[] | null; summary: string | null; error: string | null;
  drift: { drifted?: string[] | null; psi?: Record<string, number>; recent_mae?: number | null; backtest_mae?: number | null } | null;
  // E-04.1 clustering: each group's row count, mean of each numeric feature, most common level of the others (business names)
  groups?: { cluster: number; rows: number; mean: Record<string, number | null>; most_common: Record<string, string> }[] | null;
  candidates?: Record<string, Record<string, number | null>> | null; // D-99: both candidates' held-out metrics; `method` is the kept one
};
export type MlTask = "forecast" | "classification" | "regression" | "clustering" | "anomaly";
export type MlModel = {
  id: string; name: string; description?: string; task: MlTask; dataset: string; target?: string; features?: string[];
  time?: string; horizon?: number; k?: number; method?: string; latest_run: MlRun | null; latest_score: MlRun | null;
  active_run?: { run: string; pinned: string | null } | null; // ML-4: the training run scoring uses (pin_ml_run)
};
export const busy = (r?: MlRun | null) => r?.status === "queued" || r?.status === "running";
export const mlQuery = (project: string) => ({
  queryKey: ["ml", project],
  queryFn: async () => (await post<{ models: MlModel[] }>("ml_models", { project })).models,
  // a queued train/score run (C-4: it runs in the worker) is followed until it finishes
  refetchInterval: (q: { state: { data?: MlModel[] } }) => (q.state.data?.some((m) => busy(m.latest_run) || busy(m.latest_score)) ? 2000 : false),
});
// ML-4 (D-99) a model's training runs, newest first, with the importance change against the previous successful run
export type MlHistoryRun = MlRun & { active: boolean; pinned: string | null;
  importance_change: { feature: string; name: string; share: number; previous: number; change: number }[] | null };
export const mlHistoryQuery = (project: string, model: string, finished?: string | null) => ({
  queryKey: ["ml", project, "history", model, finished], // a finished run (and every ["ml", project] invalidation) refetches it
  queryFn: async () => (await post<{ runs: MlHistoryRun[] }>("ml_history", { project, model })).runs,
});
export const runStatus = (r: MlRun | null, what: "training" | "scoring") =>
  !r ? t(`ml.${what}.never`) : r.status === "failed" ? t(`ml.${what}.failed`, { error: r.error ?? "" }) : t(`ml.${what}.${r.status}`);
// ML-6 drift state of the latest scoring
export const driftText = (s: MlRun | null) =>
  s?.status !== "ok" || !s.drift || s.drift.drifted == null ? t("ml.driftUnknown") : s.drift.drifted.length ? t("ml.driftIn", { columns: s.drift.drifted.join(", ") }) : t("ml.noDrift");

// PC-8/PC-9 the project's Git remote (the password only as its ${secret:NAME} reference) and environment branch, no network.
export type RemoteSettings = { environment: string; remote: { url: string; username?: string; password?: string } | null };
export const remoteQuery = (project: string) => ({
  queryKey: ["remote_settings", project],
  queryFn: () => post<RemoteSettings>("remote_settings", { project }),
});
// SE-13 the project's own time zone/display currency (null = its tenant's), the effective ones and the rates table.
export type Locale = { time_zone: string | null; currency: string | null; rates_table: string | null;
  effective: { time_zone: string; currency: string | null } | null };
export const localeQuery = (project: string) => ({
  queryKey: ["project_locale", project],
  queryFn: () => post<Locale>("project_locale", { project }),
});
// SC-9/SE-13 tenants with their time zone and display currency (workspace owners only: anyone else gets 403)
export type Tenant = { id: string; name: string; time_zone: string | null; currency: string | null };
export const tenantsQuery = {
  queryKey: ["tenants"],
  queryFn: async () => (await post<{ tenants: Tenant[] }>("list_tenants", {})).tenants,
  retry: false,
};
// PC-8 the remote's branches with how far the project is ahead of / behind each (fetches the remote).
export type Branch = { name: string; commit: string; ahead: number; behind: number };
export const branchesQuery = (project: string) => ({
  queryKey: ["remote_branches", project],
  queryFn: async () => (await post<{ branches: Branch[] }>("remote_branches", { project })).branches,
  retry: false,
});

// UI-1 every table the user can read, as "project/table" (the Data tab and the task form share this cache entry;
// the write-back panel reads its `sync` flag from the same rows, WB-10).
type TableRow = { project: string; table: string; sync?: boolean };
export const tablesQuery = {
  queryKey: ["tables"],
  queryFn: () => api<TableRow[]>("/api/tables"),
  select: (rows: TableRow[]) => rows.map((t) => `${t.project}/${t.table}`),
};

// UI-1 every project the user has a role in, tables or not (SC-2 filtered server-side), so a new project's
// Changes tab and remote setup are reachable (PC-8).
type ProjectRow = { project: string; rename_needed?: string; production?: boolean };
export const projectsQuery = {
  queryKey: ["projects"],
  queryFn: async () => (await post<{ projects: ProjectRow[] }>("list_projects", {})).projects,
  select: (rows: ProjectRow[]) => rows.filter((p) => !p.rename_needed).map((p) => p.project),
  refetchInterval: 30_000, // UI-1.2-fix-1: a project made elsewhere (a script, another person) shows up; also on window focus
};
// X-04-fix-4: a project folder whose ID the lake can't hold is listed with why and the fix, never dropped silently
export const renameQuery = { ...projectsQuery, select: (rows: ProjectRow[]) => rows.flatMap((p) => p.rename_needed ?? []) };

// SC-9 branding: the account's tenant, or with `project` that project's (print view); null = the default look.
// The logo route needs the bearer session, so <img> gets it as an object URL, decoded before the query resolves
// (the print view's data-ready marker waits for it). endSession clears this cache with the rest.
export const brandingQuery = (project?: string) => ({
  queryKey: ["branding", project ?? null],
  queryFn: async () => {
    const b = await api<import("./Brand").Branding | null>(`/api/branding${project ? `?project=${encodeURIComponent(project)}` : ""}`);
    if (!b?.logo) return b;
    const r = await call(b.logo);
    if (!r.ok) return { ...b, logo: null };
    const img = new Image();
    img.src = URL.createObjectURL(await r.blob()); // ponytail: never revoked; one small image per tenant and session
    await img.decode().catch(() => {});
    return { ...b, logo: img.src };
  },
});

// RP-2 report specs of a project (the Data tab and the report tab share this cache entry).
export const reportsQuery = (project: string) => ({
  queryKey: ["reports", project],
  queryFn: async () => (await post<{ reports: import("./Report").ReportSpec[] }>("list_reports", { project })).reports,
});

// SH-1 reports, datasets and custom agents shared with you across projects (also without any project role).
export type Shared = { project: string; kind: "report" | "dataset" | "agent"; id: string; name: string; owner: string | null };
// CHAT-PERSIST: the person's own saved chats, newest first (a project's, or all with null); the ☰ popover and ⌘K share it
export type ChatRow = { id: number; title: string; updated: string };
export const chatsQuery = (project: string | null) => ({
  queryKey: ["chats", project],
  queryFn: () => post<{ chats: ChatRow[] }>("list_chats", project ? { project } : {}),
});

export const sharedQuery = {
  queryKey: ["shared_with_me"],
  queryFn: async () => (await post<{ shared: Shared[] }>("shared_with_me", {})).shared,
};
// SH-1: a recipient (no role in the project) reads only its one report (list_reports with `report`)
export const reportQuery = (project: string, id: string) => ({
  queryKey: ["reports", project, id],
  queryFn: async () => (await post<{ reports: import("./Report").ReportSpec[] }>("list_reports", { project, report: id })).reports,
});

// AT-1/IB-1 in-app inbox of a project, newest first (the Data tab's unread counts and the inbox tab share this cache entry).
export type InboxMessage = {
  id: number; at: string; sender: string; text: string; snapshot: string | null; // snapshot: "/api/artifacts/<project>/<file>"
  data: { columns: string[]; rows: Record<string, unknown>[] } | null;
};
export const inboxQuery = (project: string) => ({
  queryKey: ["inbox", project],
  queryFn: async () => (await post<{ messages: InboxMessage[] }>("inbox", { project })).messages,
  refetchInterval: 30_000, // quiet polling, paused while the browser tab is hidden
});
// AG-5 (X-02.2): action tool calls an agent made for this user's Slack/Teams/email message, waiting for their click here.
export type InboundApproval = { id: number; at: string; run_id: string; agent: string; action: string; input: Record<string, unknown>; status: "pending" | "approved" };
export const approvalsQuery = (project: string) => ({
  queryKey: ["inbound_approvals", project],
  queryFn: async () => (await post<{ approvals: InboundApproval[] }>("inbound_approvals", { project })).approvals,
  refetchInterval: 30_000,
});
// SC-7 (X-02.1): workspace-wide sender links; a 403 means the user isn't a workspace owner.
export type SenderLink = { kind: "slack" | "teams" | "email"; external_id: string; user: string };
export const sendersQuery = {
  queryKey: ["senders"],
  queryFn: async () => (await post<{ senders: SenderLink[] }>("list_senders", {})).senders,
  retry: false,
};
// Messages newer than the newest one this user marked read (IDs only grow).
export const unread = (messages: InboxMessage[] | undefined, read = 0) => messages?.filter((m) => m.id > read).length ?? 0;

// IN-8 data quality per table of a project (the Data-quality tab; COLUMNS of abi/ingest/quality.py, values as delivered)
export type QualityRow = [table: string, refresh: string, monitored: boolean, last_load: string | null, rows: number | null,
  fresh: boolean | null, period: string | null, missing_periods: number | null, gaps: string[][], volume_drop: number[] | null,
  null_rise: (string | number)[][], problems: string[]];
export const qualityQuery = (project: string) => ({
  queryKey: ["data_quality", project],
  queryFn: async () => (await post<{ rows: QualityRow[] }>("data_quality", { project })).rows,
});
// UI-DS.8 a source's status dot (tone) and its word, from its IN-8 row as delivered (a dot never stands alone: WCAG 1.4.1);
// tone "bad", not "danger": the global .danger is the destructive-button fill
export const sourceStatus = ([, refresh, , last, , , , , , , , problems]: QualityRow): [tone: string, text: string] =>
  refresh === "live" ? ["neutral", t("source.live")] : problems.length ? ["bad", t("source.attention")]
    : !last ? ["neutral", t("source.notLoaded")] : ["ok", t("source.healthy")];

// SC-11 usage per tenant and project for [from, to) (dates; empty = the current UTC month): the `usage` action's own
// figures and totals (E-07.3). A 403 means the user is neither a workspace owner nor a tenant admin.
type Cells = (string | number | null)[][];
// by "project": the rows end in the action's Total row; by "effort": LLM tokens per reasoning effort (LLM-2.8.3-fix-3)
export type Usage = { from: string; to: string; columns: string[]; rows: Cells };
export const usageQuery = (from = "", to = "", by: "project" | "effort" = "project") => ({
  queryKey: ["usage", from, to, by],
  queryFn: () => post<Usage>("usage", { ...(from && to ? { from, to } : {}), by }),
  retry: false,
});
