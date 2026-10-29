import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, post, projectsQuery } from "./api";
import { Icon } from "./icons";
import { Approvals } from "./Inbox";
import { focusSoon, useUi } from "./store";
import { fmtDate, fmtNumber, t } from "./i18n";

// §5 proactive help, MA-7 (F-10): the feed of system events, quiet: it never opens itself and never takes focus.
export type Note = { id: number; at: string; project: string | null; kind: string; message: string; ref: string | null; read: boolean };

const KINDS = ["refresh", "writeback", "tool", "flow", "agent", "readiness", "docs", "quality"]; // names: notes.kind.<kind>

/** The workspace tab (App.tsx tab keys) a notification is about, with the link text; null when it names no object. */
export function target(n: Note): [string, string] | null {
  const p = n.project;
  if (!p) return null;
  if (n.kind === "readiness") return [`${p}/#model`, t("notes.openModel", { project: p })];
  if (n.kind === "docs") return [`${p}/#docs`, t("notes.openDocs", { project: p })];
  if (n.kind === "quality") return [`${p}/#quality`, t("notes.openQuality", { project: p })]; // IN-8, ref = the table
  if (!n.ref) return null;
  if (n.kind === "refresh") return [`${p}/${n.ref}`, t("notes.openTable", { table: n.ref })];
  if (n.kind === "writeback") return [`${p}/${n.ref}`, t("notes.reviewEdits", { table: n.ref })]; // the edit review sits under the table grid
  return ["tool", "flow", "agent"].includes(n.kind) ? [`${p}/#${n.kind}/${n.ref}`, t(`notes.open.${n.kind}`, { id: n.ref })] : null;
}

// one query object for the foot item and the screen (one key with two queryFn shapes crashes both, T-04.2); quiet
// polling, which TanStack Query skips while the browser tab is hidden
export const notesQuery = { queryKey: ["notifications"], queryFn: () => api<Note[]>("/api/notifications"), refetchInterval: 30_000 };
export const NOTES = "*/#notifications"; // UI-DS.13 the foot's Notifications & approvals screen

// UI-DS.13 foot item: opens the screen. The count is a CSS badge with empty alt text, so the accessible name stays
// "Notifications & approvals (n unread)".
export function NotesButton({ current }: { current: boolean }) {
  const open = useUi((s) => s.open);
  const unread = useQuery(notesQuery).data?.filter((n) => !n.read).length ?? 0;
  return (
    <>
      <button id="notifications-button" data-nav="notifications" className="nav-item bell" aria-current={current ? "page" : undefined}
        data-unread={unread > 0 ? fmtNumber(unread) : undefined} onClick={() => (open(NOTES), focusSoon(`ws-${NOTES}`))}>
        <Icon name="bell" /><span>{t("nav.notifications")}</span>
        {unread > 0 && <span className="sr-only">{t("notes.unreadSuffix", { n: unread })}</span>}
      </button>
      {/* WCAG 4.1.3: polite, and its text only changes when the count does */}
      <span role="status" className="sr-only">{unread > 0 ? t("notes.unread", { n: unread }) : ""}</span>
    </>
  );
}

// The screen (UI-DS.13): approvals waiting on this person, then the feed, newest first, with a link to each note's object
// and Mark read. ponytail: one inbound_approvals read per project every 30 s while the screen is open; one workspace
// read if projects grow into the hundreds.
export default function Notifications() {
  const openTab = useUi((s) => s.open);
  const [error, setError] = useState<string | null>(null);
  const feed = useQuery(notesQuery);
  const projects = useQuery(projectsQuery).data ?? [];
  const mark = async (ids: number[], next: string) => {
    try {
      await post("mark_notifications_read", { ids });
      setError(null);
      await feed.refetch();
      focusSoon(next); // WCAG 2.4.3: a clicked "Mark read" unmounts
    } catch (e) {
      setError((e as Error).message);
    }
  };
  return (
    <section id="notifications-panel" aria-labelledby="notifications-h" className="notifications">
      <div className="section-head">
        <h2 id="notifications-h">{t("notes.title")}</h2>
        <button id="notifications-all" onClick={() => mark([], "notifications-all")}>{t("notes.markAll")}</button>
      </div>
      {error && <p role="alert">{error}</p>}
      {feed.error && <p className="error">{t("notes.loadFailed", { error: feed.error.message })}</p>}
      {feed.data?.length === 0 && <p className="muted">{t("notes.none")}</p>}
      {/* approvals waiting on this person (AG-5/D-32 inbound agent calls), per project; a section shows only when one waits */}
      {projects.map((p) => <Approvals key={p} project={p} h={`approvals-${p}-h`} title={t("notes.approvals", { project: p })} />)}
      <ol aria-labelledby="notifications-h">
        {feed.data?.map((n) => {
          const to = target(n);
          return (
            <li key={n.id} className={n.read ? "muted" : undefined}>
              <span><time dateTime={n.at}>{fmtDate(n.at)}</time> · {n.project ?? t("notes.workspace")} · {KINDS.includes(n.kind) ? t(`notes.kind.${n.kind}`) : n.kind}
                {!n.read && <strong>{t("notes.unreadMark")}</strong>}</span>
              <p>{n.message}</p>
              {to && <button id={`note-open-${n.id}`} className="link" onClick={() => (openTab(to[0]), focusSoon(`ws-${to[0]}`))}>{to[1]}</button>}{" "}
              {!n.read && <button className="sm" onClick={() => mark([n.id], to ? `note-open-${n.id}` : "notifications-all")}>{t("notes.markRead")}</button>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
