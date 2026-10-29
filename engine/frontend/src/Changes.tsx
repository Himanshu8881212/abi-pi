import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { confirmable, post } from "./api";
import { Confirm } from "./mel";
import Remote from "./Remote";
import { focusSoon } from "./store";
import { fmtDate, t } from "./i18n";

type File ={ path: string; status: "A" | "M" | "D"; patch: string };
type Commit = { id: string; message: string; author: string; by: "user" | "agent"; time: number };
// the actions this tab runs (commit, undo, revert) by name: their words are locale keys, not the action IDs
const failed = (name: string, error: string) =>
  name === "commit" ? t("changes.commitFailed", { error }) : name === "undo" ? t("changes.undoFailed", { error }) : t("changes.revertFailed", { error });
const confirmText = (name: string) => (name === "undo" ? t("changes.confirmUndo") : t("changes.confirmRevert"));

/** PC-5 readable diff: one collapsible unified patch per file, added and removed lines marked. */
export function Files({ files }: { files: File[] }) {
  if (!files.length) return <p className="muted">{t("changes.noDiff")}</p>;
  return (
    <ul className="files">
      {files.map((f) => (
        <li key={f.path}>
          <details>
            <summary>{f.path} <span className="muted">({f.status === "A" ? t("changes.added") : f.status === "M" ? t("changes.changed") : t("changes.deleted")})</span></summary>
            <pre>{f.patch.split("\n").map((l, i) => (
              <span key={i} className={/^\+(?!\+\+)/.test(l) ? "add" : /^-(?!--)/.test(l) ? "del" : undefined}>{l + "\n"}</span>
            ))}</pre>
          </details>
        </li>
      ))}
    </ul>
  );
}

// UI-4 / PC-5 Changes tab: uncommitted diff with undo and commit (message proposed from the actions, PC-6),
// then the Git history (author user or agent), a diff between any two versions, and revert with confirmation.
export default function Changes({ project }: { project: string }) {
  const qc = useQueryClient();
  const changes = useQuery({
    queryKey: ["changes", project],
    queryFn: () => post<{ files: File[]; proposed_message: string; can_undo: boolean }>("changes", { project }),
  });
  const history = useQuery({ queryKey: ["history", project], queryFn: () => post<{ commits: Commit[] }>("history", { project }) });
  const [message, setMessage] = useState<string | null>(null); // null: the proposed message
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const [range, setRange] = useState({ base: "", to: "" }); // base "": latest commit; to "": uncommitted files
  const [compared, setCompared] = useState<File[] | null>(null);
  // A pending confirmation (MA-3): `from` is the id of the button that asked, where the box shows and focus returns.
  type Pending = { name: string; body: object; from: string; text: string; message: string; token: string };
  const [confirm, setConfirm] = useState<Pending | null>(null);

  const done = (text: string) => {
    setMessage(null);
    setStatus(text);
    for (const key of [["changes", project], ["history", project], ["tables"]]) qc.invalidateQueries({ queryKey: key });
  };
  const act = async (name: string, body: object, text: string) => {
    setAlert("");
    try {
      await post(name, { project, ...body });
      done(text);
      focusSoon("history-h"); // WCAG 2.4.3: the Commit button is disabled now; the new commit heads the history
    } catch (e) {
      setAlert(failed(name, (e as Error).message));
    }
  };
  // Undo and revert may ask for a confirmation (tables removed, roles changed: §5 #3, D-1); the token goes back only on a click.
  const ask = async (name: string, body: object, from: string, text: string, token?: string) => {
    setAlert("");
    const r = await confirmable(name, { project, ...body }, token);
    if (r.confirm) return setConfirm({ name, body, from, text, ...r.confirm }); // Confirm takes focus (autoFocus)
    if (confirm) focusSoon(from); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) return setAlert(failed(name, r.error ?? ""));
    done(text);
  };
  const box = (from: string) => confirm?.from === from && (
    <Confirm label={confirmText(confirm.name)} message={confirm.message} ok={confirmText(confirm.name)}
      onConfirm={() => ask(confirm.name, confirm.body, from, confirm.text, confirm.token)} onCancel={() => setConfirm(null)} back={from} />
  );
  const compare = async () => {
    setAlert("");
    try {
      setCompared((await post<{ files: File[] }>("diff", { project, base: range.base || "HEAD", ...(range.to && { to: range.to }) })).files);
    } catch (e) {
      setAlert(t("changes.diffFailed", { error: (e as Error).message }));
    }
  };

  const err = changes.error ?? history.error;
  if (err) return <p role="alert">{t("changes.loadFailed", { project, error: err.message })}</p>;
  if (!changes.data || !history.data) return <p role="status">{t("changes.loading", { project })}</p>;
  const msg = message ?? changes.data.proposed_message;
  const commits = history.data.commits;
  const short = (c: Commit) => `${c.id.slice(0, 7)} ${c.message.split("\n")[0]}`;

  return (
    <div className="changes">
      <section aria-labelledby="uncommitted-h">
        <h2 id="uncommitted-h">{t("changes.uncommitted")}</h2>
        <Files files={changes.data.files} />
        <div className="row">
          {/* never disabled: focus would drop to <body> after the last undo; the server says "Nothing to undo." */}
          <button id="undo" onClick={() => ask("undo", {}, "undo", t("changes.undone"))}>{t("changes.undo")}</button>
        </div>
        {box("undo")}
        <label htmlFor="commit-msg">{t("changes.message")}</label>
        <textarea id="commit-msg" rows={Math.max(2, msg.split("\n").length)} value={msg} onChange={(e) => setMessage(e.target.value)} />
        <div className="row">
          <button className="primary" disabled={!changes.data.files.length || !msg.trim()}
            onClick={() => act("commit", { message: msg }, t("changes.committed"))}>{t("changes.commit")}</button>
        </div>
        <p role="status" className="muted">{status}</p>
        {alert && <p role="alert" className="error">{alert}</p>}
      </section>
      <Remote project={project} />
      <section aria-labelledby="compare-h">
        <h2 id="compare-h">{t("changes.compare")}</h2>
        <div className="row">
          <label>{t("changes.from")} <select value={range.base} onChange={(e) => setRange({ ...range, base: e.target.value })}>
            {commits.map((c, i) => <option key={c.id} value={i ? c.id : ""}>{short(c)}</option>)}
          </select></label>
          <label>{t("changes.to")} <select value={range.to} onChange={(e) => setRange({ ...range, to: e.target.value })}>
            <option value="">{t("changes.uncommittedFiles")}</option>
            {commits.map((c) => <option key={c.id} value={c.id}>{short(c)}</option>)}
          </select></label>
          <button onClick={compare}>{t("changes.showDiff")}</button>
        </div>
        {compared && <Files files={compared} />}
      </section>
      <section aria-labelledby="history-h">
        <h2 id="history-h" tabIndex={-1}>{t("changes.history")}</h2>
        <ol className="history">
          {commits.map((c) => (
            <li key={c.id}>
              <p className="msg">{c.message}</p>
              <p className="muted">
                <code>{c.id.slice(0, 7)}</code> · {c.author}{c.by === "agent" ? t("changes.byAgent") : ""} · {fmtDate(c.time * 1000)}
              </p>
              <button id={`revert-${c.id}`} className="sm" onClick={() => ask("revert", { to: c.id }, `revert-${c.id}`,
                t("changes.reverted", { id: c.id.slice(0, 7) }))}>{t("changes.revertTo", { id: c.id.slice(0, 7) })}</button>
              {box(`revert-${c.id}`)}
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}
