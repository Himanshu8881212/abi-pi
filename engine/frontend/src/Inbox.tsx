import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { approvalsQuery, confirmable, download, inboxQuery, unread, type InboundApproval, type InboxMessage } from "./api";
import { Confirm } from "./mel";
import { focusSoon, useUi } from "./store";
import { fmtDate, t } from "./i18n";

// AT-1 / V-02 IB-1 (T-05.3): a project's in-app inbox, messages posted to channel `inbox` by people, flows and agents.
// Quiet: the unread count is plain text (here and in the Data tab), nothing opens itself or takes focus.
export default function Inbox({ project }: { project: string }) {
  const q = useQuery(inboxQuery(project));
  const { user, inboxRead, readInbox } = useUi();
  const read = inboxRead[`${user}/${project}`] ?? 0;
  const [error, setError] = useState<string | null>(null);
  const n = unread(q.data, read);
  return (
    <section aria-labelledby="inbox-title" className="inbox">
      <div className="section-head">
        <h2 id="inbox-title">{n > 0 ? t("inbox.titleUnread", { project, n }) : t("inbox.title", { project })}</h2>
        {/* always rendered, so a click never drops focus to <body> (WCAG 2.4.3) */}
        <button onClick={() => q.data?.[0] && readInbox(project, q.data[0].id)}>{t("notes.markAll")}</button>
      </div>
      <Approvals project={project} />
      {error && <p role="alert">{error}</p>}
      {q.isPending && <p role="status">{t("common.loading")}</p>}
      {q.error && <p role="alert">{q.error.message}</p>}
      {q.data?.length === 0 && <p className="muted">{t("inbox.none")}</p>}
      <ol aria-labelledby="inbox-title">
        {q.data?.map((m) => <Message key={m.id} m={m} unread={m.id > read} onError={setError} />)}
      </ol>
    </section>
  );
}

function Message({ m, unread, onError }: { m: InboxMessage; unread: boolean; onError: (e: string) => void }) {
  const save = () => download(m.snapshot!).catch((e: Error) => onError(t("inbox.snapshotFailed", { error: e.message })));
  return (
    <li>
      <time dateTime={m.at}>{fmtDate(m.at)}</time> · {m.sender}
      {unread && <strong>{t("notes.unreadMark")}</strong>}
      <p>{m.text}</p>
      {m.data && (
        <table>
          <caption className="sr-only">{t("inbox.attached")}</caption>
          <thead><tr>{m.data.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
          <tbody>
            {m.data.rows.map((r, i) => <tr key={i}>{m.data!.columns.map((c) => <td key={c}>{String(r[c] ?? "")}</td>)}</tr>)}
          </tbody>
        </table>
      )}
      {m.snapshot && <button className="sm" onClick={save}>{t("inbox.download", { format: m.snapshot.split(".").pop()?.toUpperCase() ?? "" })}</button>}
    </li>
  );
}

// The queued call's input as people read it (the project is implied by the tab).
export const inputText = (input: Record<string, unknown>) =>
  JSON.stringify(Object.fromEntries(Object.entries(input).filter(([k]) => k !== "project")), null, 2);
// WCAG 2.4.3: a decided item goes away; focus the next item's Approve, else the previous one's, else the heading.
export const nextFocus = (ids: number[], id: number, h = "approvals-h") => {
  const i = ids.indexOf(id);
  const next = ids[i + 1] ?? ids[i - 1];
  return next === undefined ? h : `inbound-approve-${next}`;
};

// AG-5 / D-32 (X-02.7): action tool calls an agent made for this user's Slack/Teams/email message wait here. Approve shows the
// server's preview and runs the action only after Confirm; the queued action may then ask its own confirmation, shown the same way.
// Also on the Notifications & approvals screen (UI-DS.13), one section per project: `h` keeps heading ids unique.
export function Approvals({ project, h = "approvals-h", title = t("inbox.approvals") }: { project: string; h?: string; title?: string }) {
  const q = useQuery(approvalsQuery(project));
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<{ id: number; message: string; token: string } | null>(null);
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(null);
  if (!q.data?.length && !note && !q.error) return null;
  const ids = q.data?.map((a) => a.id) ?? [];

  const decide = async (a: InboundApproval, action: "approve_inbound_action" | "reject_inbound_action", token?: string) => {
    setNote(null);
    const r = await confirmable(action, { project, id: a.id }, token);
    if (r.confirm) return setConfirm({ id: a.id, message: r.confirm.message, token: r.confirm.token }); // Confirm takes focus
    setConfirm(null);
    qc.invalidateQueries({ queryKey: ["inbound_approvals", project] });
    if (!r.ok) {
      focusSoon(`inbound-approve-${a.id}`);
      return setNote({ error: true, text: r.error });
    }
    setNote({ error: false, text: action === "approve_inbound_action" ? t("inbox.approved", { action: a.action }) : t("inbox.rejected", { action: a.action }) });
    focusSoon(nextFocus(ids, a.id, h));
    qc.invalidateQueries({ queryKey: ["inbox", project] }); // an approved post to the inbox shows at once
  };

  return (
    <section aria-labelledby={h} className="approvals">
      <h3 id={h} tabIndex={-1}>{title}</h3>
      {q.error && <p role="alert">{q.error.message}</p>}
      {note && <p role={note.error ? "alert" : "status"} className={note.error ? "error" : undefined}>{note.text}</p>}
      {q.data?.length === 0 && <p className="muted">{t("inbox.nothingWaiting")}</p>}
      <ol aria-labelledby={h}>
        {q.data?.map((a) => {
          const c = confirm?.id === a.id ? confirm : null;
          return (
            <li key={a.id}>
              <time dateTime={a.at}>{fmtDate(a.at)}</time> · {t("inbox.asks", { agent: a.agent, action: a.action })}
              {a.status === "approved" && t("inbox.approvedOnce")}
              <pre>{inputText(a.input)}</pre>
              <div className="row">
                <button type="button" id={`inbound-approve-${a.id}`} onClick={() => decide(a, "approve_inbound_action")}>{t("inbox.approve")}</button>
                <button type="button" onClick={() => decide(a, "reject_inbound_action")}>{t("inbox.reject")}</button>
              </div>
              {c && (
                <Confirm label={t("inbox.confirm", { action: a.action })} message={c.message} onConfirm={() => decide(a, "approve_inbound_action", c.token)}
                  onCancel={() => setConfirm(null)} back={`inbound-approve-${a.id}`} />
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
