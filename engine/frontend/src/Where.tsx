import { useQuery } from "@tanstack/react-query";
import { usageQuery } from "./api";
import { navFor, screenOf, SENDERS, SETTINGS, PROVIDERS, TENANTS, USAGE, useShell, WORKSPACE_DOCS, type NavItem, type ShellRole } from "./App";
import { t } from "./i18n";
import { NOTES } from "./Notifications";
import { focusSoon, useUi } from "./store";

// UI-DS.14 "Where everything lives" (D-119 piece 4, principle 2: everything by hand too): every capability with its
// by-hand path, built from the sidebar registry (`navFor`, plus the foot), so a moved or removed item takes its rows with
// it. Per item: the Main Agent areas (backend `agent.AREAS`) its screens cover, and the capabilities inside its screen:
// [key, who may use it ("v" everyone, "e" Editors and Owners, "o" Owners), its own screen]; texts `where.<key>` (name)
// and `where.<key>.how` (the clicks after the item). The UI-PARITY checklist later.
type Cap = [string, "v" | "e" | "o", string?];
export const WHERE: Record<string, [string[], Cap[]]> = {
  chat: [[], [["ask", "v"], ["saveReport", "e"]]],
  sources: [["data"], [["addData", "e"], ["newProject", "o"], ["steps", "e"]]],
  inputs: [["writeback"], [["editCells", "e"], ["editHistory", "e"]]],
  quality: [["data"], [["alerts", "e"]]],
  model: [["model"], [["relationships", "e"]]],
  measures: [["model"], [["newMeasure", "e"]]],
  glossary: [["model"], []],
  reports: [["reports"], [["newReport", "e"], ["how", "v"], ["export", "v"], ["subscribe", "v"], ["share", "e"], ["editLayout", "e"]]],
  datasets: [["model"], []],
  ml: [["ml"], [["train", "e"]]],
  flows: [["flows"], [["newTask", "e"], ["newFlow", "e"], ["runNow", "e"]]],
  agents: [["agents"], [["askAgent", "v"], ["newAgent", "e"], ["evals", "e"]]],
  tools: [["tools"], [["newTool", "e"]]],
  sharing: [["reports"], []],
  inbox: [["flows"], []],
  security: [["admin", "model"], [["roles", "o"], ["rules", "o"], ["senders", "o", SENDERS], ["tenants", "o", TENANTS]]],
  changes: [["project"], [["commit", "e"], ["revert", "e"], ["remote", "e"]]],
  docs: [["docs"], [["generateDocs", "e"], ["workspaceDocs", "e", WORKSPACE_DOCS]]],
  notifications: [["flows"], []],
  usage: [["admin"], []],
  settings: [["admin", "project"], [["mfa", "v"], ["chatgpt", "v"], ["language", "v"], ["locale", "e"], ["delivery", "e"], ["providers", "o", PROVIDERS]]],
};
// the sidebar foot's screens (the account menu holds Settings and account)
const FOOT: NavItem[] = [{ id: "notifications", icon: "bell", screen: NOTES }, { id: "usage", icon: "gauge", screen: USAGE }, { id: "settings", icon: "settings", screen: SETTINGS }];
const RANK = { v: 0, e: 1, o: 2 };

export type Row = { key: string; name: string; path: string[]; item: NavItem; tab?: string };
// the map for a shell role: one section per sidebar heading (the top items, each group, the foot); `usage` = the person
// may see Usage & billing (the shell shows its foot item only then)
export function sections(role: ShellRole, usage: boolean): [string, Row[]][] {
  const { top, groups } = navFor(role);
  const r = role === "owner" ? 2 : role === "editor" ? 1 : 0;
  const rows = (items: NavItem[], group?: string) => items.flatMap((i): Row[] => {
    const path = [...(group ? [t(`nav.group.${group}`)] : []), t(i.label ?? `nav.${i.id}`)];
    return [{ key: i.id, name: path.at(-1)!, path, item: i }, ...WHERE[i.id][1].filter(([, who]) => RANK[who] <= r).map(([k, , tab]) =>
      ({ key: k, name: t(`where.${k}`), path: [...path, ...t(`where.${k}.how`).split(" › ")], item: i, tab }))];
  });
  return [
    ...(top.length ? [[t("where.start"), rows(top)] as [string, Row[]]] : []),
    ...groups.map(([g, items]): [string, Row[]] => [t(`nav.group.${g}`), rows(items, g)]),
    [t("where.foot"), rows(FOOT.filter((i) => i.id !== "usage" || usage))],
  ];
}

export default function Where() {
  const { mine, role } = useShell();
  const { project, open, home } = useUi();
  const usage = useQuery({ ...usageQuery(), enabled: false }).isSuccess; // asked by the account menu (a refusal is audited)
  if (!role) return <p role="status">{t("common.loading")}</p>;
  const go = (row: Row) => {
    if (row.item.id === "chat") return (home(), focusSoon("chat-input"));
    const tab = row.tab ?? screenOf(row.item, project, mine);
    open(tab);
    focusSoon(`ws-${tab}`);
  };
  return (
    <div className="where">
      <p className="sub muted">{t("where.sub")}</p>
      {sections(role, usage).map(([heading, rows], n) => (
        <section key={heading} className="panel list" aria-labelledby={`where-${n}-h`}>
          <h2 id={`where-${n}-h`}>{heading}</h2>
          <div className="table-wrap" tabIndex={0} role="region" aria-labelledby={`where-${n}-h`}>
            <table>
              <thead><tr><th scope="col">{t("where.what")}</th><th scope="col">{t("where.path")}</th><th scope="col"><span className="sr-only">{t("where.open")}</span></th></tr></thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.key}>
                    <th scope="row">{row.name}</th>
                    <td>{row.path.join(" › ")}</td>
                    <td><button id={`where-${row.key}`} className="sm" aria-label={t("where.openName", { name: row.name })} onClick={() => go(row)}>{t("where.open")}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  );
}
