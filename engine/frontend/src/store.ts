import { useEffect, useState, useSyncExternalStore, type RefObject } from "react";
import { create } from "zustand";

export type Theme = "light" | "dark" | "system";
// UI-DS.7/UI-DS.8: canvas-drawn parts (charts, the Glide grid) follow <html data-theme> (App.tsx sets it; print, share
// and embed set none and stay light)
const darkOs = () => matchMedia("(prefers-color-scheme: dark)");
const look = (): "light" | "dark" => {
  const x = document.documentElement.dataset.theme;
  return x === "dark" || (x === "system" && darkOs().matches) ? "dark" : "light";
};
function onLook(f: () => void) {
  const mo = new MutationObserver(f), os = darkOs();
  mo.observe(document.documentElement, { attributeFilter: ["data-theme"] });
  os.addEventListener("change", f);
  return () => (mo.disconnect(), os.removeEventListener("change", f));
}
export const useTheme = () => useSyncExternalStore(onLook, look, () => "light" as const);
// Workspace UI state (UI-1, UI-2; V2 shell UI-DS.2). `tabs` are the open screens (keys in App.tsx), `active` the shown
// one; no active screen is the Chats home.
type Ui = {
  token: string | null;
  user: string | null; // the signed-in name, keying per-user browser state (inboxRead)
  tabs: string[];
  names: Record<string, string>; // tab → the object's display name, set once it has loaded (UI-2: names, not IDs)
  active: string | null;
  project: string | null; // §5 context-aware: the active tab's project ("project/table"), kept when the last tab or a workspace tab ("*/…") is active
  notice: string | null; // shown on the sign-in screen, e.g. after the session expired
  inboxRead: Record<string, number>; // "user/project" → newest inbox message ID read (AT-1 inbox, T-05.3); mirrored in localStorage
  theme: Theme; // D-77 appearance, per browser (localStorage "abi.theme")
  compact: boolean; // D-77 density, per browser (localStorage "abi.density"); App.tsx puts it on <html data-density>
  newChats: number; // bumped by the header's New chat; the Chat starts an empty chat on a change
  groups: Record<string, boolean>; // UI-DS.3 sidebar group → expanded, once someone toggled it (localStorage "abi.groups")
  showGroup: (group: string, open: boolean) => void;
  signIn: (token: string | null, user?: string) => void;
  readInbox: (project: string, id: number) => void;
  home: () => void; // the Chats screen
  startChat: () => void;
  setTheme: (theme: Theme) => void;
  setCompact: (compact: boolean) => void;
  open: (tab: string) => void;
  close: (tab: string) => void;
  name: (tab: string, name: string) => void;
  setProject: (project: string | null) => void; // the top bar's project switcher (§5 chat context); opening a tab overrides it
  restored: string | null; // PROJECT-REMEMBER: the project brought back from the last visit, until checkProject sees the list
  checkProject: (projects: string[]) => void; // drops the restored project once the list shows it's gone (deleted, role removed)
  dialog: "project" | "data" | null; // UI-2: the open New project / Add data dialog (mounted once in App)
  attach: boolean; // UI-DS.5.1: the Add data dialog came from the composer's attach, so the loaded table becomes a chip
  chatTables: string[]; // UI-DS.5.1: tables ("project/table") picked as the open chat's context, sent in /api/chat's `context`
  chat: { id: number | null; busy: boolean }; // UI-DS.5.2: the open chat's saved id and whether it streams (the Chat sets it)
  askChat: { id: number | null; busy: boolean }; // UI-DS.5.3-fix-1: the same for the Ask about this drawer's chat
  chatReq: { open: number } | { drop: number } | null; // UI-DS.5.2: the ☰ popover or ⌘K asks the Chat to open a saved chat, or to leave a deleted one
  ask: boolean; // UI-DS.5.3: the Ask about this drawer is open (UI-DS.6: a report's Ask to change opens it too)
};

const saved = (key: string) => {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null; // no sessionStorage (SSR tests, blocked storage)
  }
};
const stored = (key: string) => {
  try {
    return globalThis.window?.localStorage.getItem(key);
  } catch {
    return null;
  }
};
const read = (key: string) => {
  try {
    return JSON.parse(globalThis.window?.localStorage.getItem(key) ?? "{}"); // window: Node warns on a bare localStorage
  } catch {
    return {};
  }
};

// UI-1.3: each user's open tabs come back at the next sign-in or reload (localStorage "abi.tabs": user → tabs, active);
// only open/close/setProject write it, so a sign-out's clear keeps them. PROJECT-REMEMBER: the switcher's project too
// (entries saved before it carry none: the active tab's project then)
type Tabs = { tabs: string[]; active: string | null; project?: string | null };
const allTabs = (): Record<string, Tabs> => read("abi.tabs");
const lastTabs = (user: string | null | undefined): Tabs & { project: string | null; restored: string | null } => {
  const t = (user && allTabs()[user]) || { tabs: [], active: null };
  const project = t.project === undefined ? projectOf(t.active, null) : t.project;
  return { tabs: t.tabs, active: t.active, project, restored: project };
};
const keepTabs = ({ user, tabs, active, project }: Ui) => {
  try {
    if (user) globalThis.window?.localStorage.setItem("abi.tabs", JSON.stringify({ ...allTabs(), [user]: { tabs, active, project } }));
  } catch {}
};

const projectOf = (tab: string | null | undefined, was: string | null) => (tab && !tab.startsWith("*/") ? tab.split("/")[0] : was);

export const useUi = create<Ui>()((set, get) => ({
  token: saved("abi.token"),
  user: saved("abi.user"),
  inboxRead: read("abi.inboxRead"),
  theme: (["light", "dark"] as const).find((x) => x === stored("abi.theme")) ?? "system",
  compact: stored("abi.density") === "compact",
  newChats: 0,
  groups: read("abi.groups"),
  showGroup: (group, open) => {
    const groups = { ...get().groups, [group]: open };
    try {
      localStorage.setItem("abi.groups", JSON.stringify(groups));
    } catch {}
    set({ groups });
  },
  names: {},
  ...lastTabs(saved("abi.user")),
  notice: null,
  dialog: null,
  attach: false,
  chatTables: [],
  chat: { id: null, busy: false },
  askChat: { id: null, busy: false },
  chatReq: null,
  ask: false,
  signIn: (token, user) => {
    try {
      if (token) sessionStorage.setItem("abi.token", token);
      else sessionStorage.removeItem("abi.token");
      if (user) sessionStorage.setItem("abi.user", user);
      else sessionStorage.removeItem("abi.user");
    } catch {}
    set(token ? { token, user: user ?? null, notice: null, ...lastTabs(user) } : { token, user: null }); // a sign-in notice ends with the sign-in
  },
  readInbox: (project, id) => {
    const inboxRead = { ...get().inboxRead, [`${get().user}/${project}`]: id };
    try {
      localStorage.setItem("abi.inboxRead", JSON.stringify(inboxRead));
    } catch {}
    set({ inboxRead });
  },
  home: () => {
    set({ active: null });
    keepTabs(get());
  },
  startChat: () => (get().home(), set((s) => ({ newChats: s.newChats + 1 }))),
  setTheme: (theme) => {
    try {
      localStorage.setItem("abi.theme", theme);
    } catch {}
    set({ theme });
  },
  setCompact: (compact) => {
    try {
      localStorage.setItem("abi.density", compact ? "compact" : "comfortable");
    } catch {}
    set({ compact });
  },
  // ponytail: the 30 newest open screens are kept (there is no tab strip to close them); a list UI can lift it
  open: (tab) => {
    set((s) => ({ tabs: s.tabs.includes(tab) ? s.tabs : [...s.tabs, tab].slice(-30), active: tab, project: projectOf(tab, s.project) }));
    keepTabs(get());
  },
  close: (tab) => {
    set((s) => {
      const i = s.tabs.indexOf(tab);
      const tabs = s.tabs.filter((t) => t !== tab);
      const active = s.active === tab ? (tabs[i] ?? tabs[i - 1] ?? null) : s.active;
      return { tabs, active, project: projectOf(active, s.project) };
    });
    keepTabs(get());
  },
  name: (tab, name) => set((s) => ({ names: { ...s.names, [tab]: name } })),
  setProject: (project) => {
    set({ project });
    keepTabs(get());
  },
  checkProject: (projects) => {
    set((s) => ({ restored: null, project: s.project === s.restored && s.project && !projects.includes(s.project) ? null : s.project }));
    keepTabs(get());
  },
}));

// UI-1.2 menus as native <details> (Account menu, the composer's Database): Escape or a click outside closes one. Focus goes
// back to the summary on Escape, and on a click only when it was in the menu or fell to <body> (a clicked control keeps
// the focus it took; WCAG 2.4.3).
export function dismiss(d: HTMLDetailsElement | null, e: Pick<KeyboardEvent, "type" | "key" | "target">) {
  if (!d?.open || (e.type === "keydown" ? e.key !== "Escape" : d.contains(e.target as Node))) return;
  const back = e.type === "keydown" || d.contains(document.activeElement) || document.activeElement === document.body;
  d.open = false;
  if (back) d.querySelector("summary")?.focus();
}
export function useDismiss(d: RefObject<HTMLDetailsElement | null>) {
  useEffect(() => {
    const on = (e: Event) => dismiss(d.current, e as KeyboardEvent);
    document.addEventListener("keydown", on);
    document.addEventListener("click", on);
    return () => (document.removeEventListener("keydown", on), document.removeEventListener("click", on));
  }, []);
}

// UI-DS.5.1: add (once) or remove a table from the open chat's context
export const pickTable = (tab: string, on: boolean) =>
  useUi.setState((s) => ({ chatTables: on ? [...s.chatTables.filter((x) => x !== tab), tab] : s.chatTables.filter((x) => x !== tab) }));

// WCAG 2.4.3: when the focused control unmounts, move focus to its successor after React commits.
export const focusSoon = (id: string) => setTimeout(() => document.getElementById(id)?.focus());

// UI-2/PC-8: an editor seeds its draft from a query once, so a tab opened while a pull's refetch is running kept the old
// definition. Call this during render: when the server value changes, `changed(now, old)` runs (re-seed an untouched draft there).
export function useServerChange<T>(server: T, changed: (now: T, old: T) => void) {
  const [seen, setSeen] = useState(server);
  if (JSON.stringify(seen) !== JSON.stringify(server)) {
    setSeen(server);
    changed(server, seen);
  }
}
