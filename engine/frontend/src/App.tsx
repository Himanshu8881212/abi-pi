import { useQueries, useQuery } from "@tanstack/react-query";
import { Fragment, lazy, Suspense, useEffect, useRef, useState, type ElementType, type FormEvent, type MouseEvent, type ReactNode } from "react";
import { Icon, type IconName } from "./icons";
import { agentsQuery, canEdit, driftText, runStatus, api, brandingQuery, call, chatsQuery, meQuery, flowsQuery, flowStatus, inboxQuery, logout, measuresQuery, mlQuery, modelQuery, projectsQuery, qualityQuery, queryClient, sourceStatus, type QualityRow, providersQuery, readinessQuery, renameQuery, reportsQuery, sharedQuery, startOidc, tablesQuery, toolsQuery, toolStatus, triggerText, unread, usageQuery, versionQuery } from "./api";
import { Chat, openSaved, prefill, RecentChats } from "./Chat";
import { parse } from "./taskFlow";
import Notifications, { NOTES, NotesButton } from "./Notifications";
import { focusSoon, useDismiss, useUi, type Theme } from "./store";
import { fmtDate, fmtNumber, LOCALES, t, useLang } from "./i18n";
import { BrandBar, useBrand } from "./Brand";
import { Codes, Key, Request, SetPassword, type Link } from "./Account";
import { Dataset, SharedList } from "./Sharing";
import { ProductionBadge } from "./Production";
import { NewData, openDialog, useEditable } from "./NewData";
import type { Glossary } from "./Model"; // a type only: Model stays a lazy chunk

const TableTab = lazy(() => import("./Steps"));
const ModelDiagram = lazy(() => import("./Model"));
const Changes = lazy(() => import("./Changes"));
const Docs = lazy(() => import("./Docs"));
const Report = lazy(() => import("./Report"));
const Measure = lazy(() => import("./Measure"));
const Tool = lazy(() => import("./Tool"));
const Flow = lazy(() => import("./Flow"));
const Agent = lazy(() => import("./Agent"));
const Task = lazy(() => import("./Task"));
const Inbox = lazy(() => import("./Inbox"));
const Ml = lazy(() => import("./Ml"));
const Senders = lazy(() => import("./Senders"));
const Security = lazy(() => import("./Security"));
const Quality = lazy(() => import("./Quality"));
const Usage = lazy(() => import("./Usage"));
const Tenants = lazy(() => import("./Tenants"));
const Providers = lazy(() => import("./Providers"));
const Settings = lazy(() => import("./Settings"));
const Where = lazy(() => import("./Where"));
// "project/#model", "project/#changes", "project/#docs" and "project/#report/<id>" are a project's model, changes,
// docs and report tabs; table names can't contain "#". "*/#docs" is the workspace's docs ("*" is no project ID).
const MODEL = "#model";
const CHANGES = "#changes";
const SECURITY = "#security"; // "project/#security": roles, row and column rules (UI-DS.12)
const DOCS = "#docs";
const QUALITY = "#quality"; // "project/#quality": the project's IN-8 Data-quality view
const INBOX = "#inbox"; // "project/#inbox": the project's AT-1 in-app inbox
const REPORT = "#report/";
const DATASET = "#dataset/"; // "project/#dataset/<id>": a dataset's rows (SE-1), shareable (SH-1)
const MEASURE = "#measure/"; // "project/#measure/" (empty ID) is a new measure
const TOOL = "#tool/"; // "project/#tool/" (empty ID) is a new tool
const FLOW = "#flow/"; // "project/#flow/" (empty ID) is a new flow
const AGENT = "#agent/"; // "project/#agent/" (empty ID) is a new agent
const ML = "#ml/"; // "project/#ml/<id>": an ML model (X-01)
const TASK = "#task/"; // "project/#task/" (empty ID) is a new agent task (AT-1); a task is a flow with the task shape
export const WORKSPACE_DOCS = "*/#docs";
export const SENDERS = "*/#senders"; // SC-7 channel sender links (workspace-wide)
export const USAGE = "*/#usage"; // SC-11 usage per tenant (workspace owners and tenant admins)
export const TENANTS = "*/#tenants"; // SE-13 tenant time zone and currency (workspace owners)
export const PROVIDERS = "*/#providers"; // AG-6 workspace LLM providers and the Main Agent model (workspace owners)
export const SETTINGS = "*/#settings"; // UI-DS.13 Settings and account (account, ChatGPT, language, project locale and delivery)
export const MAP = "*/#map"; // UI-DS.14 Where everything lives (D-119 piece 4), from Settings and the account menu
const LIST = "@"; // "*/@<item>": a sidebar item's list screen (UI-DS.2), e.g. "*/@reports"
const kind = (t: string | null) => t?.slice(t.indexOf("/") + 1);

// UI-DS.2 sidebar (D-119 groups), ONLY items whose screen exists today (D-120); labels are locale keys nav.<id> and
// nav.group.<group>. An item opens its list screen ("*/@<id>"), or with `per` and a project picked that project's
// screen ("<project>/<per>"), or its one workspace screen.
export type NavItem = { id: string; icon: IconName; per?: string; screen?: string; label?: string };
export const NAV: [string, NavItem[]][] = [
  ["data", [{ id: "sources", icon: "database" }, { id: "inputs", icon: "pencil" }, { id: "quality", icon: "badge-check", per: QUALITY }]],
  ["model", [{ id: "model", icon: "network", per: MODEL }, { id: "measures", icon: "sigma" }, { id: "glossary", icon: "file-text" }]],
  ["analyze", [{ id: "reports", icon: "layout-dashboard" }, { id: "datasets", icon: "table-2" }]],
  ["predict", [{ id: "ml", icon: "brain-circuit" }]],
  ["automate", [{ id: "flows", icon: "workflow" }, { id: "agents", icon: "bot" }, { id: "tools", icon: "wrench" }]],
  ["collaborate", [{ id: "sharing", icon: "share" }, { id: "inbox", icon: "inbox", per: INBOX }]],
  ["govern", [{ id: "security", icon: "lock", per: SECURITY }, { id: "changes", icon: "git-branch", per: CHANGES }, { id: "docs", icon: "book-open" }]],
];
// UI-DS.3 (D-119 piece 1) the shell by role: SC-2 roles are per project, so the shell follows the highest role in any
// listed project (each screen still gates its own controls by that project's role and production flag). No project:
// a share recipient (SH-1) when something is shared with them, else the first run of a workspace owner (an empty
// workspace; actions refuse anyone else).
export type ShellRole = "owner" | "editor" | "viewer" | "recipient";
const RANKS = ["viewer", "editor", "owner"] as const;
export const shellRole = (roles: (string | null | undefined)[], shared: number): ShellRole =>
  RANKS[Math.max(-1, ...roles.map((r) => RANKS.indexOf(r as never)))] ?? (shared ? "recipient" : "owner");
const ADVANCED = ["predict", "automate", "govern"]; // collapsed for an Editor until opened once
const item = (id: string) => NAV.flatMap(([, i]) => i).find((i) => i.id === id)!;
const SHARED = { ...item("sharing"), label: "sharing.withMe" };
// the top items (no heading) and the groups each shell lists; a Viewer: Chats, Inbox, Reports, published Agents (AG-1:
// ask them, definitions read-only) and Shared with me (the F-10 bell sits in the foot); a recipient: only what is shared with them
export function navFor(role: ShellRole): { top: NavItem[]; groups: [string, NavItem[]][] } {
  const chat = { id: "chat", icon: "message-square" as const };
  if (role === "viewer") return { top: [chat, item("inbox")], groups: [["portal", [item("reports"), item("agents"), SHARED]]] }; // "portal": a group id unlike any item id (ids nav-<id>)
  if (role === "recipient") return { top: [], groups: [["portal", [SHARED]]] };
  return { top: [chat], groups: NAV };
}
// UI-DS.3: the shell's role, once known (null while the lists load, so a Viewer never sees the full sidebar flash)
export function useShell() {
  const projects = useQuery(projectsQuery);
  const mine = projects.data ?? [];
  const roles = useQueries({ queries: mine.map(meQuery) });
  const shared = useQuery({ ...sharedQuery, enabled: projects.isSuccess && !mine.length });
  const role = !projects.isSuccess || roles.some((q) => q.isPending) || (!mine.length && shared.isPending) ? null
    : shellRole(roles.map((q) => q.data?.role), shared.data?.length ?? 0);
  return { mine, role };
}
// the screen a sidebar item opens: its one screen, the picked project's screen (`per`), else its list screen
export const screenOf = (i: NavItem, project: string | null, mine: string[]) =>
  i.screen ?? (i.per && project && mine.includes(project) ? `${project}/${i.per}` : `*/${LIST}${i.id}`);
// the sidebar item a screen belongs to (breadcrumb, aria-current); foot screens: notifications, usage, settings
const ITEM_OF: Record<string, string> = {
  [MODEL]: "model", [SECURITY]: "security", [CHANGES]: "changes", [DOCS]: "docs", [QUALITY]: "quality", [INBOX]: "inbox", [REPORT]: "reports", [DATASET]: "datasets",
  [MEASURE]: "measures", [TOOL]: "tools", [FLOW]: "flows", [TASK]: "flows", [AGENT]: "agents", [ML]: "ml",
  [kind(SENDERS)!]: "security", [kind(USAGE)!]: "usage", [kind(TENANTS)!]: "security", [kind(PROVIDERS)!]: "settings",
  [kind(NOTES)!]: "notifications", [kind(SETTINGS)!]: "settings", [kind(MAP)!]: "settings",
};
const FOOT = [USAGE, NOTES, SETTINGS]; // a foot item's one screen: no detail in its breadcrumb
export function itemOf(tab: string | null): string {
  if (!tab) return "chat";
  const k = kind(tab)!;
  if (k.startsWith(LIST)) return k.slice(LIST.length);
  const prefix = k.slice(0, k.indexOf("/") + 1) || k;
  if (Object.hasOwn(ITEM_OF, prefix)) return ITEM_OF[prefix];
  // anything else is a table: an input table (WB-2) by its cached IN-8 row (UI-DS.8), else a source
  return queryClient.getQueryData<QualityRow[]>(qualityQuery(tab.slice(0, tab.indexOf("/"))).queryKey)
    ?.some((r) => r[0] === k && r[1] === "input") ? "inputs" : "sources";
}
const groupOf = (item: string, groups = NAV) => groups.find(([, items]) => items.some((i) => i.id === item))?.[0];
// the shell's item for a screen: a shareable object outside the shell's items (a Viewer's shared dataset, anything a
// recipient opens) belongs to Shared with me
const itemIn = (tab: string | null, groups: [string, NavItem[]][]) => {
  const i = itemOf(tab);
  return ["reports", "datasets", "agents"].includes(i) && !groupOf(i, groups) && groupOf("sharing", groups) ? "sharing" : i;
};
const navLabel = (id: string, groups: [string, NavItem[]][]) => t(groups.flatMap(([, i]) => i).find((i) => i.id === id)?.label ?? `nav.${id}`);
const listable = (item: string) => NAV.some(([, items]) => items.some((i) => i.id === item && !i.screen));
// The workspace panel by tab kind: an exact kind ("#model") or an ID prefix ("#measure/<id>"); anything else is a table.
// `project` is "*" for workspace tabs, `id` what follows the prefix.
const VIEWS: Record<string, ElementType<{ project: string; id: string }>> = {
  [MODEL]: ModelDiagram, [SECURITY]: Security, [CHANGES]: Changes, [QUALITY]: Quality, [INBOX]: Inbox,
  [DOCS]: ({ project }) => <Docs project={project === "*" ? null : project} />,
  [kind(SENDERS)!]: Senders, [kind(USAGE)!]: Usage, [kind(TENANTS)!]: Tenants, [kind(PROVIDERS)!]: Providers,
  [kind(NOTES)!]: Notifications, [kind(SETTINGS)!]: Settings, [kind(MAP)!]: Where,
  [MEASURE]: Measure, [TOOL]: Tool, [FLOW]: Flow, [TASK]: Task, [ML]: Ml, [AGENT]: Agent, [REPORT]: Report, [DATASET]: Dataset,
};
export function view(tab: string) {
  const k = kind(tab)!;
  if (k.startsWith(LIST)) return <List key={tab} id={k.slice(LIST.length)} />;
  const prefix = k.slice(0, k.indexOf("/") + 1) || k;
  const View = Object.hasOwn(VIEWS, prefix) ? VIEWS[prefix] : null; // a table may be named "constructor"
  return View ? <View key={tab} project={tab.split("/")[0]} id={k.slice(prefix.length)} /> : <TableTab key={tab} table={tab} />;
}
// a tab's label: "<project> <kind>[ <id>]"; kind words are locale keys tab.<kind> (dynamic: listed in en.json)
const label = (tab: string) => {
  if (tab === WORKSPACE_DOCS) return t("tab.workspaceDocs");
  if (tab === SENDERS) return t("tab.senders");
  if (tab === USAGE) return t("tab.usage");
  if (tab === TENANTS) return t("tab.tenants");
  if (tab === PROVIDERS) return t("tab.providers");
  if (tab === NOTES) return t("tab.notifications");
  if (tab === SETTINGS) return t("tab.settings");
  if (tab === MAP) return t("tab.map");
  const k = kind(tab);
  if (!k?.startsWith("#")) return tab;
  const name = k.slice(1).split("/")[0];
  return `${tab.split("/")[0]} ${t(`tab.${name}`)}${k.slice(1 + name.length).replace("/", " ")}`;
};

// SC-10/UI-3: the Account menu closes on a screen choice and focus moves to the opened tab (WCAG 2.4.3)
export const pick = (e: Pick<MouseEvent<HTMLElement>, "currentTarget">, tab: string) => {
  e.currentTarget.closest("details")!.open = false;
  useUi.getState().open(tab);
  focusSoon(`ws-${tab}`);
};

// §5: the project the chat works in; opening a tab still sets it to that tab's project (store.open)
function ProjectSwitch() {
  const listed = useQuery(projectsQuery);
  const projects = listed.data ?? [];
  const { project, setProject, restored, checkProject } = useUi();
  useEffect(() => {
    if (restored && listed.isSuccess) checkProject(projects); // PROJECT-REMEMBER: a deleted project or removed role → No project
  });
  // E-15: an environment is a project; Production when flagged (view-only, D-56), else Dev (no Staging flag exists)
  const prod = useQuery({ ...projectsQuery, select: (rows) => rows.filter((r) => r.production).map((r) => r.project) }).data ?? [];
  const owner = useQuery({ ...meQuery(project ?? ""), enabled: !!project && prod.includes(project) }).data?.role === "owner";
  return (
    <div className="project-switch">
      <label title={t("shell.projectHint")}><span className="label">{t("shell.project")}</span>{" "}
        <select id="project-switch" value={project ?? ""} onChange={(e) => setProject(e.target.value || null)}>
          <option value="">{t("shell.noProject")}</option>
          {projects.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
      </label>
      {project && projects.includes(project) && (prod.includes(project) ? <ProductionBadge project={project} />
        : <span className="badge env">{t("shell.envDev")}</span>)}
      {/* D-56: production changes only by promoting from dev, an Owner's confirmed pull in the project's Changes screen */}
      {project && prod.includes(project) && owner && (
        <button id="promote" className="ghost sm" onClick={() => (useUi.getState().open(`${project}/${CHANGES}`), focusSoon(`ws-${project}/${CHANGES}`))}>{t("shell.promote")}</button>
      )}
    </div>
  );
}

export function App(props: { link?: Link | null }) {
  const token = useUi((s) => s.token);
  const [link, setLink] = useState(props.link); // an emailed link's token (SC-10), held only here
  useLang((s) => s.lang); // UI-5: a language change re-renders the whole app
  const theme = useUi((s) => s.theme); // D-77: on <html>, only in the app (print, share and embed views stay light)
  useEffect(() => void (document.documentElement.dataset.theme = theme), [theme]);
  const compact = useUi((s) => s.compact); // D-77 density, on <html> like the theme
  useEffect(() => void (document.documentElement.dataset.density = compact ? "compact" : "comfortable"), [compact]);
  if (link) return <SetPassword {...link} onDone={() => setLink(null)} />;
  return token ? <Workspace /> : <SignIn />;
}

// UI-5 language picker: each locale file listed by its own name, in its own language (WCAG 3.1.2)
export function Language() {
  const { lang, setLang } = useLang();
  return (
    <label className="language">{t("language.label")}{" "}
      <select value={lang} onChange={(e) => setLang(e.target.value)}>
        {Object.keys(LOCALES).sort().map((l) => <option key={l} value={l} lang={l}>{LOCALES[l]["language.name"] ?? l}</option>)}
      </select>
    </label>
  );
}

// D-98 AGPL §13: a link to the running version's source for every network user
function Source({ className }: { className?: string }) {
  const v = useQuery(versionQuery).data;
  return v ? <a className={className} href={v.source} target="_blank" rel="noreferrer">{t("shell.source", { version: v.version })}</a> : null;
}

function SignIn() {
  const signIn = useUi((s) => s.signIn);
  const notice = useUi((s) => s.notice);
  const [error, setError] = useState<string | null>(null);
  // SC-10 second step: the code (401 {mfa: "code"}) or enrolment (403 {mfa: "setup", secret, uri}); the key and the
  // recovery codes stay in this component's state only (SC-4)
  const [mfa, setMfa] = useState<{ mfa: "code" | "setup"; secret?: string; uri?: string } | null>(null);
  const [codes, setCodes] = useState<{ token: string; name: string; codes: string[] } | null>(null);
  const [ask, setAsk] = useState<"reset" | "signup" | null>(null);
  const providers = useQuery(providersQuery).data ?? [];

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const name = String(form.get("name"));
    const code = form.get("code");
    const body = JSON.stringify({ name, password: form.get("password"), ...(code ? { code } : {}) });
    try {
      // First run only (SC-1): the button creates the first account, a workspace owner.
      if ((e.nativeEvent as SubmitEvent).submitter?.id === "setup") await api("/api/setup", { method: "POST", body });
      const r = await call("/api/login", { method: "POST", body });
      const json = await r.json().catch(() => null);
      const d = json?.detail;
      if (r.ok) return json.recovery_codes ? setCodes({ token: json.token, name, codes: json.recovery_codes }) : signIn(json.token, name);
      if (!d?.mfa) return (setMfa(null), setError(typeof d === "string" ? d : r.statusText));
      setMfa(d);
      setError(code ? t("signin.mfa.wrong") : null);
      if (code) focusSoon("signin-code"); // WCAG 3.3.1: a wrong code is announced, focus stays on the code field
    } catch (err) {
      setError((err as Error).message);
    }
  }

  if (codes) return <main className="signin"><Codes codes={codes.codes} onDone={() => signIn(codes.token, codes.name)} /></main>;
  return (
    <main className="signin">
      <h1>{t("app.name")}</h1>
      <Language />
      {ask ? <Request kind={ask} onBack={() => (setAsk(null), focusSoon("signin-name"))} /> : (<>
      <form onSubmit={submit}>
        {notice && <p id="signin-notice" role="status">{notice}</p>}
        <label>{t("signin.name")} <input id="signin-name" name="name" autoComplete="username" required autoFocus={!!notice} aria-describedby={notice ? "signin-notice" : undefined} /></label>
        <label>{t("signin.password")} <input name="password" type="password" autoComplete="current-password" minLength={8} required /></label>
        {mfa && (<>
          <p id="signin-code-hint">{t(mfa.mfa === "setup" ? "signin.mfa.setup" : "signin.mfa.code")}</p>
          {mfa.secret && mfa.uri && <Key secret={mfa.secret} uri={mfa.uri} />}
          <label>{t("mfa.code")} <input id="signin-code" name="code" autoComplete="one-time-code" required autoFocus aria-describedby="signin-code-hint" aria-invalid={!!error} /></label>
        </>)}
        {error && <p role="alert">{error}</p>}
        <button type="submit" className="primary">{t("signin.submit")}</button>
        {!mfa && <button type="submit" id="setup" className="link">{t("signin.setup")}</button>}
      </form>
      <button id="signin-forgot" className="link" onClick={() => setAsk("reset")}>{t("signin.forgot")}</button>{" "}
      <button id="signin-signup" className="link" onClick={() => setAsk("signup")}>{t("signin.signup")}</button>
      </>)}
      {/* SC-1 OIDC: one button per configured provider (none while OIDC is off) */}
      {providers.map((p) => (
        <button key={p.id} className="oidc" onClick={() => startOidc(p.id).catch((e) => setError((e as Error).message))}>{t("signin.with", { name: p.name })}</button>
      ))}
      <p className="source"><Source /></p>
    </main>
  );
}

// UI-DS.2 V2 shell (D-119): sidebar of screens, header with breadcrumb, project switcher and New chat, the screen in the
// main area. Chats is the home; the chat stays mounted while another screen shows, so the conversation survives.
function Workspace() {
  const { names, active, open, home, startChat } = useUi();
  const [error, setError] = useState<string | null>(null);
  // SC-1 sign-out: revoke the session server-side, then clear locally (endSession).
  // a 401 already ended the session; otherwise still signed in: the session wasn't revoked
  const signOut = () => logout().catch((e) => useUi.getState().token && setError((e as Error).message));
  const notice = useUi((s) => s.notice);
  useEffect(() => void (notice && document.getElementById("account-notice")?.focus()), [notice]); // WCAG 2.4.3: a return notice
  const brand = useQuery(brandingQuery()).data; // SC-9: the app chrome follows the signed-in account's tenant
  useBrand(brand);
  // SC-11: the `usage` action decides who sees usage (workspace owners, tenant admins); asked once the account menu
  // first opens (a refusal is audited, so not on every page load), the Usage item shows only on success
  const [menu, setMenu] = useState(false);
  const usage = useQuery({ ...usageQuery(), enabled: menu });
  const { user, theme, setTheme, compact, setCompact } = useUi();
  const account = useRef<HTMLDetailsElement>(null);
  useDismiss(account);
  // UI-1.3 landing: a Viewer (no project they can edit) with a report and no saved screens gets the first report open,
  // so nobody lands on an empty workspace
  const { mine, role } = useShell();
  const reports = useQueries({ queries: mine.map(reportsQuery) });
  const { top, groups } = role ? navFor(role) : { top: [], groups: [] };
  const portal = role === "viewer" || role === "recipient"; // D-119: the tenant-branded portal of a Viewer
  const landed = useRef(false);
  useEffect(() => {
    if (landed.current || !role || reports.some((q) => q.isPending)) return;
    landed.current = true;
    if (useUi.getState().tabs.length) return;
    if (role === "recipient") return open(`*/${LIST}sharing`); // only shared objects: no chat home
    const first = reports.flatMap((q, i) => (q.data ?? []).map((r) => `${mine[i]}/${REPORT}${r.id}`))[0];
    if (first && role === "viewer") open(first);
  });
  const project = useUi((s) => s.project);
  // UI-DS.4 (D-119 piece 2): up to 1024 px the sidebar is a drawer behind the header's toggle; any screen choice closes it
  const [drawer, setDrawer] = useState(false);
  const closeDrawer = () => (setDrawer(false), focusSoon("side-toggle"));
  useEffect(() => setDrawer(false), [active]);
  // UI-DS.5.3 (§5 always visible, D-77 Viewer chat drawer): Ask about this on every screen but the chat opens its own chat
  // beside the screen; it stays mounted while closed, so its conversation survives
  const ask = useUi((s) => s.ask);
  const setAsk = (on: boolean) => useUi.setState({ ask: on });
  const asking = ask && !!active;
  const closeAsk = () => (setAsk(false), focusSoon("ask-open"));
  const go = (i: NavItem) => {
    setDrawer(false);
    if (i.id === "chat") home();
    else open(screenOf(i, project, mine));
  };
  const current = itemIn(active, groups);
  const nav = (id: string, icon: IconName, onClick: () => void, key = `nav-${id}`) => (
    <button key={key} id={key} data-nav={id} className="nav-item" aria-current={current === id ? "page" : undefined} onClick={onClick}>
      <Icon name={icon} /><span>{navLabel(id, groups)}</span>
    </button>
  );
  // groups fold for Owners and Editors (an Editor's advanced ones start folded); the current screen's group unfolds
  const { groups: folds, showGroup } = useUi();
  const fold = role === "owner" || role === "editor";
  const expanded = (g: string) => !fold || (folds[g] ?? !(role === "editor" && ADVANCED.includes(g)));
  const mineGroup = groupOf(current, groups);
  useEffect(() => void (mineGroup && !expanded(mineGroup) && showGroup(mineGroup, true)), [active, role]); // on navigation only: a group folded by hand stays folded
  return (
    <>
    <a className="skip" href="#main">{t("shell.skip")}</a>
    <div className="shell">
      <h1 className="sr-only">{t("app.name")}</h1>
      <aside className={drawer ? "side open" : "side"} id="side" aria-label={t("shell.side")}
        onKeyDown={(e) => drawer && e.key === "Escape" && !(e.target as HTMLElement).closest("details[open]") && closeDrawer()}>
        {brand && !portal ? <BrandBar b={brand} as="div" /> : <div className="brand"><span className="mark" aria-hidden /><span className="product">{t("app.name")}</span></div>}
        <nav className="side-nav" aria-label={t("shell.nav")}>
          {top.map((i) => nav(i.id, i.icon, () => go(i)))}
          {groups.map(([g, items]) => (
            <div key={g} className="nav-group">
              <h2 id={`nav-${g}-h`}>{fold
                ? <button className="nav-fold" aria-expanded={expanded(g)} aria-controls={`nav-${g}`} onClick={() => showGroup(g, !expanded(g))}>
                    {t(`nav.group.${g}`)}<Icon name="chevron-down" /></button>
                : t(`nav.group.${g}`)}</h2>
              <ul id={`nav-${g}`} aria-labelledby={`nav-${g}-h`} hidden={!expanded(g)}>{items.map((i) => <li key={i.id}>{nav(i.id, i.icon, () => go(i))}</li>)}</ul>
            </div>
          ))}
        </nav>
        <div className="side-foot">
          <NotesButton current={current === "notifications"} />
          {usage.isSuccess && nav("usage", "gauge", () => (open(USAGE), focusSoon(`ws-${USAGE}`)), "usage-open")}
          <details className="account" ref={account} onToggle={(e) => e.currentTarget.open && setMenu(true)}>
            <summary aria-label={user ? t("shell.accountOf", { name: user }) : undefined}>
              <span className="avatar" aria-hidden data-initial={(user ?? "").slice(0, 1).toUpperCase()} />
              <span className="who">{user ?? t("shell.account")}<span className="muted">{t("nav.settings")}</span></span>
            </summary>
            <div className="account-menu">
              {user && <p className="signed-in">{t("shell.signedInAs", { name: user })}</p>}
              {/* UI-DS.13: account, ChatGPT, language, workspace and project settings live on the Settings screen;
                  appearance and density stay here, per browser and one click away (D-77, the prototype's foot toggle) */}
              <button id="settings-open" className="item" onClick={(e) => pick(e, SETTINGS)}>{t("settings.open")}</button>
              <button id="map-open" className="item" onClick={(e) => pick(e, MAP)}>{t("tab.map")}</button>
              <label className="language">{t("shell.theme")}{" "}
                <select id="theme" value={theme} onChange={(e) => setTheme(e.target.value as Theme)}>
                  {(["system", "light", "dark"] as const).map((x) => <option key={x} value={x}>{t(`shell.theme.${x}`)}</option>)}
                </select>
              </label>
              <label className="language">{t("shell.density")}{" "}
                <select id="density" value={compact ? "compact" : "comfortable"} onChange={(e) => setCompact(e.target.value === "compact")}>
                  <option value="comfortable">{t("shell.density.comfortable")}</option>
                  <option value="compact">{t("shell.density.compact")}</option>
                </select>
              </label>
              <Source className="item" />
              <button className="signout item" onClick={signOut}>{t("shell.signOut")}</button>
            </div>
          </details>
        </div>
      </aside>
      {drawer && <div className="scrim" onClick={closeDrawer} />}
      <div className="main-col">
        <header className={portal && brand ? "topbar branded" : "topbar"}>
          <button id="side-toggle" className="ghost side-toggle" aria-expanded={drawer} aria-controls="side" aria-label={t("shell.side")} title={t("shell.side")}
            onClick={() => drawer ? setDrawer(false) : (setDrawer(true), focusSoon(`nav-${(top[0] ?? groups[0]?.[1][0])?.id}`))}><Icon name="panel-left" /></button>
          {portal && brand && <BrandBar b={brand} as="div" />}
          <nav className="crumb" aria-label={t("shell.breadcrumb")}><Crumb tab={active} name={active ? (names[active] ?? label(active)) : ""} groups={groups} /></nav>
          <ProjectSwitch />
          {error && <p role="alert" className="notice error">{error}</p>}
          {notice && <p id="account-notice" role="status" tabIndex={-1} className="notice">{notice}</p>}
          {active && role && role !== "recipient" && (
            <button id="ask-open" className="ghost" aria-expanded={asking} aria-controls="ask"
              onClick={() => asking ? setAsk(false) : (setAsk(true), focusSoon("ask-input"))}><Icon name="message-square" /><span className="label">{t("ask.open")}</span></button>
          )}
          {role !== "recipient" && <button id="new-chat" className="ghost" onClick={startChat}><Icon name="plus" /><span className="label">{t("shell.newChat")}</span></button>}
          {role && role !== "recipient" && <RecentChats />}
          {role && <Finder role={role} go={go} />}
          <NewData />
        </header>
        <div className="work">
        <main id="main" tabIndex={-1} className="workspace">
          {/* the chat stays mounted while hidden, so the conversation survives screen switches */}
          <div id="panel-chat" className="chat-screen" hidden={!!active}>
            <Chat viewer={role === "viewer"} />
          </div>
          {active && (
            <div id="ws-panel" role="region" aria-labelledby={`ws-${active}`} className="grid">
              <Suspense fallback={<p role="status">{t("common.loading")}</p>}>{view(active)}</Suspense>
            </div>
          )}
        </main>
        {role && role !== "recipient" && (
          <aside id="ask" className="ask" aria-labelledby="ask-h" hidden={!asking}
            onKeyDown={(e) => e.key === "Escape" && !(e.target as HTMLElement).closest("details[open]") && closeAsk()}>
            <div className="ask-head">
              <h2 id="ask-h">{t("ask.title")}</h2>
              <button type="button" id="ask-close" className="ghost sm" aria-label={t("common.close")} title={t("common.close")} onClick={closeAsk}><Icon name="x" /></button>
            </div>
            <Chat screen={active ? crumbText(active, names[active] ?? label(active), groups) : ""} />
          </aside>
        )}
        </div>
        {/* UI-DS.4: a Viewer's phone tab bar (shown under 640 px only); the drawer keeps the bell and the account menu */}
        {role === "viewer" && (
          <nav className="tabbar" aria-label={t("shell.nav")}>
            {["chat", "reports", "sharing", "inbox"].map((id) => {
              const i = [...top, ...groups.flatMap(([, g]) => g)].find((x) => x.id === id)!;
              return (
                <button key={id} id={`tab-${id}`} aria-current={current === id ? "page" : undefined} onClick={() => go(i)}>
                  <Icon name={i.icon} /><span>{navLabel(id, groups)}</span>
                </button>
              );
            })}
          </nav>
        )}
      </div>
    </div>
    </>
  );
}

// UI-DS.5.2 ⌘K (D-77): the person's saved chats (none for a share recipient: no chat home) and their role's sidebar
// screens (`navFor`, so a Viewer finds no Editor screen) whose label holds the query
export type Hit = { key: string; label: string; chat?: number; item?: NavItem };
export function hits(q: string, chats: { id: number; title: string }[], role: ShellRole): Hit[] {
  const { top, groups } = navFor(role);
  const screens = [...top.map((i): [string | null, NavItem] => [null, i]), ...groups.flatMap(([g, items]) => items.map((i): [string, NavItem] => [g, i]))]
    .map(([g, i]) => ({ key: `nav-${i.id}`, label: (g ? `${t(`nav.group.${g}`)} › ` : "") + navLabel(i.id, groups), item: i }));
  const saved = role === "recipient" ? [] : chats.map((c) => ({ key: `chat-${c.id}`, label: c.title || t("chat.new"), chat: c.id }));
  const f = q.trim().toLowerCase();
  return [...saved, ...screens].filter((h) => h.label.toLowerCase().includes(f));
}

// ⌘K/Ctrl+K: a modal search (WAI-ARIA combobox + listbox): arrows move, Enter opens, Escape closes and the native
// <dialog> puts focus back where it was; an opened chat focuses the message box, a screen its breadcrumb entry
function Finder({ role, go }: { role: ShellRole; go: (i: NavItem) => void }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const on = (e: KeyboardEvent) => (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k" && (e.preventDefault(), setOpen(true));
    document.addEventListener("keydown", on);
    return () => document.removeEventListener("keydown", on);
  }, []);
  useEffect(() => void (open && !dlg.current!.open && dlg.current!.showModal()), [open]);
  const choose = (h: Hit) => {
    dlg.current!.close(); // focus goes back first, then to what opens
    if (h.chat !== undefined) return openSaved(h.chat);
    go(h.item!);
    const a = useUi.getState().active;
    focusSoon(a ? `ws-${a}` : "chat-input");
  };
  return (
    <dialog ref={dlg} id="finder" className="share-dialog finder" aria-labelledby="finder-h" onClose={() => setOpen(false)}>
      <h2 id="finder-h" className="sr-only">{t("finder.title")}</h2>
      {open && <Found role={role} choose={choose} />}
    </dialog>
  );
}

function Found({ role, choose }: { role: ShellRole; choose: (h: Hit) => void }) {
  const [q, setQ] = useState("");
  const [k, setK] = useState(0);
  const busy = useUi((s) => s.chat.busy); // a chat can't be swapped while an answer streams
  const chats = useQuery({ ...chatsQuery(null), enabled: role !== "recipient" }).data?.chats ?? [];
  const list = hits(q, chats, role);
  const at = Math.min(k, list.length - 1);
  const off = (h: Hit) => busy && h.chat !== undefined;
  const pickHit = (h: Hit | undefined) => h && !off(h) && choose(h);
  const move = (by: number) => setK((at + by + list.length) % list.length);
  return (
    <>
      <label className="sr-only" htmlFor="finder-q">{t("finder.title")}</label>
      <input id="finder-q" type="search" role="combobox" aria-expanded="true" aria-controls="finder-list" aria-autocomplete="list" autoFocus autoComplete="off"
        aria-activedescendant={list[at] ? `finder-${list[at].key}` : undefined} placeholder={t("finder.placeholder")} value={q}
        onChange={(e) => (setQ(e.target.value), setK(0))}
        onKeyDown={(e) => {
          const go = { ArrowDown: () => move(1), ArrowUp: () => move(-1), Enter: () => pickHit(list[at]) }[e.key];
          if (go && list.length) e.preventDefault(), go();
        }} />
      <ul id="finder-list" role="listbox" aria-label={t("finder.results")}>
        {list.map((h, n) => (
          <li key={h.key} id={`finder-${h.key}`} role="option" aria-selected={n === at} aria-disabled={off(h) || undefined} onClick={() => pickHit(h)}>
            <Icon name={h.item?.icon ?? "message-square"} /><span>{h.label}</span><span className="muted">{t(h.chat !== undefined ? "finder.chat" : "finder.screen")}</span>
          </li>
        ))}
      </ul>
      {list.length === 0 && <p role="status" className="muted">{t("finder.none")}</p>}
    </>
  );
}

// a screen's breadcrumb parts: its group, its sidebar item and whether a detail (the object's name) follows; a detail
// sits below its item on an object screen, not on a list screen nor a foot item's one screen
const crumbOf = (tab: string | null, groups: [string, NavItem[]][]) => {
  const item = itemIn(tab, groups);
  return { item, group: groupOf(item, groups), text: navLabel(item, groups), detail: !!tab && !kind(tab)!.startsWith(LIST) && !FOOT.includes(tab) };
};
// UI-DS.5.3: the screen as one line ("Analyze › Reports › Sales"), the Ask about this drawer's context chip; a part equal
// to the one before it is said once (a Viewer's "Reports" group holds its "Reports" item)
export const crumbText = (tab: string | null, name: string, groups = NAV) => {
  const { group, text, detail } = crumbOf(tab, groups);
  return [group && t(`nav.group.${group}`), text, detail && name].filter((x, k, xs) => x && x !== xs[k - 1]).join(" › ");
};

// group › item › detail; the current entry carries `ws-<screen key>`, the focus target editors use after a save
export function Crumb({ tab, name, groups = NAV }: { tab: string | null; name: string; groups?: [string, NavItem[]][] }) {
  const { item, group, text, detail } = crumbOf(tab, groups);
  const here = (text: string) => <li><span id={tab ? `ws-${tab}` : "ws-chat"} tabIndex={-1} aria-current="page">{text}</span></li>;
  return (
    <ol>
      {group && <li>{t(`nav.group.${group}`)}</li>}
      {!detail ? here(text) : listable(item)
        ? <li><button className="link" onClick={() => useUi.getState().open(`*/${LIST}${item}`)}>{text}</button></li>
        : <li>{text}</li>}
      {detail && here(name)}
    </ol>
  );
}

// A sidebar item's list screen (UI-1 Data tab sections, one per item): only the switcher's project, its name kept for
// screen readers (and the CDP checks) as a visually hidden prefix (UI-1.2-fix-1); "No project" lists every project with
// a visible prefix. Items are "project/table" (project names can't contain "/") or the screen keys above.
const scope = (all: string[], picked: string | null) => {
  const one = !!picked && all.includes(picked);
  return { projects: one ? [picked] : all, pre: (p: string, sep = ": ") => (one ? <span className="sr-only">{p}{sep}</span> : `${p}${sep}`) };
};
const H: Record<string, string> = { sources: "tables-h", inputs: "inputs-h", reports: "reports-h", datasets: "datasets-h", measures: "measures-h", glossary: "glossary-h", tools: "tools-h",
  flows: "flows-h", agents: "agents-h", ml: "ml-h", sharing: "shared-list-h" };
// per-project screens listed one row per project: item → screen kind
const PER: Record<string, string> = { quality: QUALITY, model: MODEL, inbox: INBOX, security: SECURITY, changes: CHANGES, docs: DOCS };

function List({ id }: { id: string }) {
  const h = H[id] ?? `list-${id}-h`;
  return (
    <section className="panel list" aria-labelledby={h}>
      <h2 id={h}>{t(`nav.${id}`)}</h2>
      <Items id={id} h={h} />
    </section>
  );
}

function Items({ id, h }: { id: string; h: string }) {
  const { user, inboxRead, project, open: onOpen } = useUi();
  const all = useQuery(projectsQuery).data ?? [];
  const { projects, pre } = scope(all, project);
  const has = (item: string) => id === item;
  const tables = useQuery({ ...tablesQuery, enabled: has("sources") || has("inputs") });
  // UI-DS.8 each table's IN-8 row: its status, refresh, last load and rows, and whether it is an input table (WB-2)
  const quality = useQueries({ queries: projects.map((p) => ({ ...qualityQuery(p), enabled: has("sources") || has("inputs") })) });
  // D-56: no new design objects in a production environment
  const prod = new Set(useQuery({ ...projectsQuery, select: (rows) => rows.filter((r) => r.production).map((r) => r.project) }).data);
  const rename = useQuery(renameQuery).data ?? [];
  const editable = useEditable(); // UI-2: Add data only with a project to load into
  const shared = useQuery({ ...sharedQuery, enabled: has("sharing") || has("reports") });
  const reportQs = useQueries({ queries: projects.map((p) => ({ ...reportsQuery(p), enabled: has("reports") })) });
  const reports = reportQs.flatMap((q, i) => (q.data ?? []).map((r) => ({ tab: `${projects[i]}/${REPORT}${r.id}`, p: projects[i], r })));
  const measures = useQueries({ queries: projects.map((p) => ({ ...measuresQuery(p), enabled: has("measures") })) });
  // UI-DS.9 M-03 terms as semantic_model returns them (a restricted Viewer's agent-written ones masked, H-01.4-fix-5)
  const terms = useQueries({ queries: projects.map((p) => ({ ...modelQuery<Glossary>(p), enabled: has("glossary") })) });
  const glossary = terms.flatMap((q, i) => (q.data?.glossary ?? []).map((g) => ({ p: projects[i], g })));
  const tools = useQueries({ queries: projects.map((p) => ({ ...toolsQuery(p), enabled: has("tools") })) });
  const builtins = tools.find((q) => q.data)?.data?.builtins ?? [];
  const flows = useQueries({ queries: projects.map((p) => ({ ...flowsQuery(p), enabled: has("flows") })) });
  const agents = useQueries({ queries: projects.map((p) => ({ ...agentsQuery(p), enabled: has("agents") })) });
  const ready = useQueries({ queries: projects.map((p) => ({ ...readinessQuery(p), enabled: has("agents") })) });
  const roles = useQueries({ queries: projects.map((p) => ({ ...meQuery(p), enabled: has("agents") })) });
  const inbox = useQueries({ queries: projects.map((p) => ({ ...inboxQuery(p), enabled: has("inbox") })) });
  const mlq = useQueries({ queries: projects.map((p) => ({ ...mlQuery(p), enabled: has("ml") })) });
  const ml = mlq.flatMap((q, i) => (q.data ?? []).map((m) => ({ p: projects[i], m })));
  const datasets = useQueries({ queries: projects.map((p) => ({ ...modelQuery<{ datasets: { id: string; name: string }[] }>(p), enabled: has("datasets") })) })
    .flatMap((q, i) => (q.data?.datasets ?? []).map((d) => ({ tab: `${projects[i]}/${DATASET}${d.id}`, p: projects[i], name: d.name })));
  const link = (tab: string, text: ReactNode, extra?: ReactNode, key = tab) =>
    <li key={key}><button className="link" onClick={() => onOpen(tab)}>{text}</button>{extra && <>{" "}{extra}</>}</li>;

  if (has("sharing")) {
    if (shared.data?.length === 0) return <p className="muted">{t("nav.sharingNone")}</p>;
    return <SharedList onOpen={onOpen} />;
  }
  if (has("sources") || has("inputs")) {
    // a table shows once its project's IN-8 rows are in (an input table must not flash under Sources); a failed
    // data_quality read lists the project's tables as sources without status
    const rows = (tables.data ?? []).flatMap((tab) => {
      const i = projects.indexOf(tab.split("/")[0]);
      const name = tab.slice(tab.indexOf("/") + 1);
      const r = quality[i]?.data?.find((r) => r[0] === name);
      if (i < 0 || quality[i].isPending || (!r && quality[i].isFetching)) return []; // a new table waits for its row
      return (r?.[1] === "input") === has("inputs") ? [{ tab, p: projects[i], name, r }] : [];
    });
    const loading = tables.isPending || quality.some((q) => q.isPending);
    const head = [t("quality.table"), t("quality.refresh"), t("quality.lastLoad"), t("quality.rows"), t("source.status")];
    return (
      <>
        <p className="sub muted">{has("inputs") ? t("source.inputsSub") : t("source.sourcesSub")}</p>
        {has("sources") && <div className="row new-data-starts">
          <button className="sm" onClick={() => openDialog("project")}>{t("newProject.title")}</button>
          {editable.length > 0 && <button className="sm primary" onClick={() => openDialog("data")}>{t("addData.open")}</button>}
        </div>}
        {loading && <p role="status">{t("common.loading")}</p>}
        {tables.error && <p role="alert">{tables.error.message}</p>}
        {!loading && rows.length === 0 && <p className="muted">{has("inputs") ? t("source.noInputs")
          : `${t("data.noTables")} ${editable.length ? t("data.noTablesAdd") : t("data.noTablesHow")}`}</p>}
        {has("sources") && rename.map((why) => <p key={why}>{t("data.renameNeeded", { why })}</p>)}
        {rows.length > 0 && (has("inputs")
          ? <ul aria-labelledby={h}>{rows.map(({ tab, p, name }) => link(tab, <>{pre(p, "/")}{name}</>))}</ul>
          : (
            <div className="table-wrap" tabIndex={0} role="region" aria-labelledby={h}>
              <table className="sources">
                <thead><tr>{head.map((x) => <th key={x} scope="col">{x}</th>)}</tr></thead>
                <tbody>
                  {rows.map(({ tab, p, name, r }) => {
                    const [tone, text] = r ? sourceStatus(r) : ["neutral", ""];
                    return (
                      <tr key={tab}>
                        <th scope="row"><button className="link" onClick={() => onOpen(tab)}>{pre(p, "/")}{name}</button></th>
                        <td>{r?.[1] ?? ""}</td>
                        <td>{r?.[3] ? <time dateTime={r[3]}>{fmtDate(r[3])}</time> : ""}</td>
                        <td className="num">{typeof r?.[4] === "number" ? fmtNumber(r[4]) : (r?.[4] ?? "")}</td>
                        <td>{text && <span className={`status ${tone}`}>{text}</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
      </>
    );
  }
  if (has("datasets"))
    return (
      <>
        <p className="sub muted">{t("dataset.listSub")}</p>
        {datasets.length ? <ul aria-labelledby={h}>{datasets.map((d) => link(d.tab, <>{pre(d.p)}{d.name}</>))}</ul> : <p className="muted">{t("nav.datasetsNone")}</p>}
      </>
    );
  // UI-DS.9 a raised table of the model's definitions (design/v2-chat-first .oc-table); text only, no figures (principle 7)
  const table = (head: string[], body: ReactNode) => (
    <div className="table-wrap" tabIndex={0} role="region" aria-labelledby={h}>
      <table>
        <thead><tr>{head.map((x) => <th key={x} scope="col">{x}</th>)}</tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>
  );
  // UI-DS.9/UI-DS.11 a design-object row: its name opens its screen, then text cells (an error tone when it fails)
  const row = (tab: string, p: string, name: string, cells: ReactNode[]) => (
    <tr key={tab}>
      <th scope="row"><button className="link" onClick={() => onOpen(tab)}>{pre(p)}{name}</button></th>
      {cells.map((c, i) => <td key={i}>{c}</td>)}
    </tr>
  );
  const bad = (on: boolean | undefined, text: string) => <span className={on ? "error" : undefined}>{text}</span>;
  // "New x in p" per project the person may design in (D-56: none in production)
  const starts = (kinds: [string, string][]) => (
    <div className="row new-data-starts">{projects.filter((p) => !prod.has(p)).flatMap((p) => kinds.map(([kind, key]) => (
      <button key={p + kind} className="sm primary" onClick={() => onOpen(`${p}/${kind}`)}>{t(key, { project: p })}</button>)))}</div>
  );
  const pending = (qs: { isPending: boolean }[]) => qs.some((q) => q.isPending) && <p role="status">{t("common.loading")}</p>;
  if (has("reports")) {
    // UI-DS.10 (D-119/D-120): reports are made in chat; mine = the projects' reports, then SH-1 reports others shared with me
    const theirs = (shared.data ?? []).filter((s) => s.kind === "report" && !all.includes(s.project));
    return (
      <>
        <p className="sub muted">{t("report.listSub")}</p>
        {editable.length > 0 && <div className="row new-data-starts">
          <button id="reports-new" className="sm primary" onClick={() => prefill(t("home.reportQ"))}>{t("report.newInChat")}</button></div>}
        {reportQs.some((q) => q.isPending) ? <p role="status">{t("common.loading")}</p>
          : reports.length === 0 ? <p className="muted">{t("data.noReports")}</p>
          : table([t("report.name"), t("report.description"), t("report.pages")], reports.map(({ tab, p, r }) => (
            <tr key={tab}>
              <th scope="row"><button className="link" onClick={() => onOpen(tab)}>{pre(p)}{r.name}</button></th>
              <td>{String(r.description ?? "")}</td>
              <td>{r.pages.map((x) => x.name).join(", ")}</td>
            </tr>
          )))}
        {theirs.length > 0 && (
          <>
            <h2 id="reports-shared-h">{t("sharing.withMe")}</h2>
            <ul aria-labelledby="reports-shared-h">{theirs.map((s) => link(`${s.project}/${REPORT}${s.id}`, t("sharing.item", { project: s.project, kind: t("tab.report"), name: s.name }),
              s.owner && <span className="muted">{t("sharing.by", { owner: s.owner })}</span>))}</ul>
          </>
        )}
      </>
    );
  }
  if (has("measures")) {
    const rows = projects.flatMap((p, i) => (measures[i].data ?? []).map((m) => ({ p, m })));
    return (
      <>
        <p className="sub muted">{t("measure.listSub")}</p>
        {starts([[MEASURE, "data.newMeasure"]])}
        {pending(measures)}
        {rows.length > 0 && table([t("measure.name"), t("measure.description"), t("measure.expression")], rows.map(({ p, m }) =>
          row(`${p}/${MEASURE}${m.id}`, p, m.name, [m.description ?? "", <code>{m.expression}</code>])))}
      </>
    );
  }
  if (has("glossary"))
    return (
      <>
        <p className="sub muted">{t("glossary.sub")}</p>
        {terms.some((q) => q.isPending) ? <p role="status">{t("common.loading")}</p>
          : glossary.length === 0 ? <p className="muted">{t("glossary.none")}</p>
          : table([t("glossary.term"), t("glossary.definition"), t("glossary.linked")], glossary.map(({ p, g }) => (
            <tr key={`${p}/${g.id}`}>
              <th scope="row">{pre(p)}{g.term}</th>
              <td>{g.definition}</td>
              <td>{[...(g.measures ?? []), ...(g.columns ?? []).map((c) => `${c.table}.${c.column}`)].join(", ")}</td>
            </tr>
          )))}
      </>
    );
  if (has("tools")) {
    const rows = projects.flatMap((p, i) => (tools[i].data?.tools ?? []).map((tool) =>
      row(`${p}/${TOOL}${tool.id}`, p, tool.name, [t(`tools.kind.${tool.kind ?? "query"}`), bad(tool.disabled, toolStatus(tool))])));
    return (
      <>
        <p className="sub muted">{t("tools.sub")}</p>
        {starts([[TOOL, "data.newTool"]])}
        {pending(tools)}
        {table([t("tools.name"), t("tool.kind"), t("source.status")], [...rows, ...builtins.map((b) => (
          <tr key={b.id}><th scope="row">{b.id}</th><td>{t("data.builtin")}</td><td /></tr>))])}
      </>
    );
  }
  if (has("flows")) {
    const rows = projects.flatMap((p, i) => (flows[i].data?.flows ?? []).map((f) => {
      const last = flows[i].data?.last_runs[f.id];
      const task = !!parse(f); // AT-3: tasks open in the task form, other flows in the flow editor
      return row(`${p}/${task ? TASK : FLOW}${f.id}`, p, f.name, [task ? t("flows.task") : t("flows.flow"), triggerText(f.trigger),
        bad(last?.status === "failed", flowStatus(f, last))]);
    }));
    return (
      <>
        <p className="sub muted">{t("flows.sub")}</p>
        {starts([[TASK, "data.newTask"], [FLOW, "data.newFlow"]])}
        {pending(flows)}
        {rows.length > 0 && table([t("flows.name"), t("tool.kind"), t("flows.trigger"), t("source.status")], rows)}
      </>
    );
  }
  if (has("agents")) {
    // UI-DS.11-fix-1: a Viewer lists only published agents (AG-1: they ask those) and starts none (SC-2)
    const viewer = (i: number) => !canEdit(roles[i].data?.role);
    const rows = projects.flatMap((p, i) => (agents[i].data ?? []).filter((a) => !viewer(i) || a.status === "published").map((a) =>
      row(`${p}/${AGENT}${a.id}`, p, a.name, [a.owner ? t("agents.ownerUser", { owner: a.owner }) : "", bad(a.disabled, toolStatus(a))])));
    return (
      <>
        <p className="sub muted">{t("agents.sub")}</p>
        <div className="row new-data-starts">{projects.map((p, i) => {
          const missing = ready[i].data?.missing ?? [];
          return !prod.has(p) && !viewer(i) && (
            <Fragment key={p}>
              {/* §3: agents need a Ready model; the gaps say what to fix */}
              <button className="sm primary" disabled={!ready[i].data?.ready} aria-describedby={missing.length ? `agents-gaps-${p}` : undefined}
                onClick={() => onOpen(`${p}/${AGENT}`)}>{t("data.newAgent", { project: p })}</button>
              {missing.length > 0 && <p id={`agents-gaps-${p}`} className="muted">{t("data.needsReady", { missing: missing.join(" ") })}</p>}
            </Fragment>
          );
        })}</div>
        {pending(agents)}
        {rows.length > 0 && table([t("agents.name"), t("agents.owner"), t("source.status")], rows)}
      </>
    );
  }
  if (has("ml"))
    return (
      <>
        <p className="sub muted">{t("ml.listSub")}</p>
        {pending(mlq) || (ml.length === 0 && <p className="muted">{t("data.noMl")}</p>)}
        {ml.length > 0 && table([t("ml.name"), t("ml.task"), t("ml.dataset"), t("ml.training"), t("ml.drift")], ml.map(({ p, m }) =>
          row(`${p}/${ML}${m.id}`, p, m.name, [m.task, m.dataset,
            bad(m.latest_run?.status === "failed", runStatus(m.latest_run, "training") + (m.latest_run?.better_than_baseline === false ? t("ml.notBetter") : "")),
            driftText(m.latest_score)])))}
      </>
    );
  const kind = PER[id];
  return (
    <ul aria-labelledby={h}>
      {projects.map((p, i) => {
        const n = id === "inbox" ? unread(inbox[i].data, inboxRead[`${user}/${p}`]) : 0;
        return link(`${p}/${kind}`, <>{pre(p, " ")}{t(`tab.${kind.slice(1)}`)}</>, n > 0 && <span className="muted">{t("data.unread", { count: n })}</span>);
      })}
      {id === "docs" && link(WORKSPACE_DOCS, label(WORKSPACE_DOCS))}
      {id === "security" && [link(SENDERS, t("nav.senders")), link(TENANTS, t("tenants.title"))]}
    </ul>
  );
}
