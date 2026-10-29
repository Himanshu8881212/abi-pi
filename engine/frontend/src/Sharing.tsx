import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { canShare, confirmable, meQuery, post, sharedQuery, type Shared } from "./api";
import { format, type Value } from "./charts";
import { Confirm } from "./mel";
import type { ReportSpec } from "./Report";
import { focusSoon } from "./store";
import { fmtDate, t } from "./i18n";

// SE-8 Subscribe dialog of a report (any project member, Viewers too): subscribe to one data visual's saved query with
// its report, page and visual filters (as visual_data applies them, without selections); the worker re-runs it as you
// and a changed result arrives in your Inbox. Lists and removes your subscriptions in the project.
type Query = { measures: string[]; dimensions?: string[]; filters?: unknown[] };
type Sub = { id: number; name: string; checked: string | null; error: string | null };
const fs = (x: object) => (x as { filters?: unknown[] }).filters ?? [];
export const subscribable = (report: ReportSpec) => report.pages.flatMap((p) => p.visuals.filter((v) => v.query).map((v) => {
  const q = v.query as Query;
  const what = q.dimensions?.length ? t("report.titleBy", { measures: q.measures.join(", "), dims: q.dimensions.join(", ") }) : q.measures.join(", ");
  const name = t("subscribe.option", { report: report.name, page: p.name, visual: v.title ?? what });
  return { key: `${p.id}/${v.id}`, name, query: { ...q, filters: [...fs(report), ...fs(p), ...fs(q)] } };
}));
export function SubscribeDialog({ project, report }: { project: string; report: ReportSpec }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const opts = subscribable(report);
  const [pick, setPick] = useState(0);
  const [status, setStatus] = useState("");
  const qc = useQueryClient();
  const key = ["subscriptions", project];
  const subs = useQuery({ queryKey: key, enabled: open, queryFn: async () => (await post<{ subscriptions: Sub[] }>("list_subscriptions", { project })).subscriptions });
  const add = async () => {
    const o = opts[pick];
    try {
      await post("subscribe", { project, name: o.name, query: o.query });
      setStatus(t("subscribe.done", { name: o.name }));
      await qc.invalidateQueries({ queryKey: key });
    } catch (e) {
      setStatus(t("subscribe.failed", { error: (e as Error).message }));
    }
  };
  const remove = async (s: Sub) => {
    try {
      await post("unsubscribe", { project, id: s.id });
      setStatus(t("subscribe.removed", { name: s.name }));
      await qc.invalidateQueries({ queryKey: key });
    } catch (e) {
      setStatus(t("subscribe.failed", { error: (e as Error).message }));
    }
    focusSoon("subscribe-list-h"); // WCAG 2.4.3: the Unsubscribe button unmounts
  };
  return (
    <>
      <button id="subscribe-open" className="sm" onClick={() => (setOpen(true), setStatus(""), dlg.current!.showModal())}>{t("subscribe.open")}</button>
      <dialog ref={dlg} className="share-dialog" aria-labelledby="subscribe-h" onClose={() => (setOpen(false), focusSoon("subscribe-open"))}>
        <h2 id="subscribe-h">{t("subscribe.heading", { name: report.name })}</h2>
        <p className="muted">{t("subscribe.explain")}</p>
        {open && (
          <>
            {opts.length === 0 ? <p className="muted">{t("subscribe.noVisuals")}</p> : (
              <div className="row">
                <label>{t("subscribe.visual")} <select id="subscribe-visual" value={pick} onChange={(e) => setPick(Number(e.target.value))}>
                  {opts.map((o, i) => <option key={o.key} value={i}>{o.name}</option>)}
                </select></label>
                <button id="subscribe-add" className="primary" onClick={add}>{t("subscribe.add")}</button>
              </div>
            )}
            <p role="status" className="muted">{status}</p>
            <h3 id="subscribe-list-h" tabIndex={-1}>{t("subscribe.mine")}</h3>
            {subs.error && <p role="alert" className="error">{subs.error.message}</p>}
            {subs.data?.length === 0 && <p className="muted">{t("subscribe.none")}</p>}
            {!!subs.data?.length && (
              <ul aria-labelledby="subscribe-list-h">
                {subs.data.map((s) => (
                  <li key={s.id}>
                    {s.name}{" "}
                    <span className={s.error ? "error" : "muted"}>{s.error ? t("subscribe.error", { error: s.error }) : s.checked ? t("subscribe.checked", { at: fmtDate(s.checked) }) : t("subscribe.notYet")}</span>{" "}
                    <button className="sm" aria-label={t("subscribe.removeLabel", { name: s.name })} onClick={() => remove(s)}>{t("subscribe.remove")}</button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        <div className="row"><button onClick={() => dlg.current!.close()}>{t("common.close")}</button></div>
      </dialog>
    </>
  );
}

// SH-1 (D-4, D-51): a report, dataset or custom agent shared with named accounts or workspace-wide reaches them
// view-only without a project role (row-level security applies). Not the RP-6 link dialog (Share.tsx).
type Sharing = { users: string[]; workspace: boolean };
export const names = (s: string) => [...new Set(s.split(",").map((x) => x.trim()).filter(Boolean))];

// Owner/Editor: who else may view it. set_sharing replaces the list, so the dialog starts from the stored one
// (get_sharing); widening shows the server's FL-6 preview, and only the Confirm click sends its token.
export function SharingDialog({ project, kind, id, name }: { project: string; kind: Shared["kind"]; id: string; name: string }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [current, setCurrent] = useState<Sharing | null>(null);
  const [users, setUsers] = useState("");
  const [workspace, setWorkspace] = useState(false);
  const [confirm, setConfirm] = useState<{ message: string; token: string } | null>(null);
  const [status, setStatus] = useState("");
  const shown = (s: Sharing) => (setCurrent(s), setUsers(s.users.join(", ")), setWorkspace(s.workspace));
  const show = async () => {
    setCurrent(null);
    setConfirm(null);
    setStatus("");
    dlg.current!.showModal();
    try {
      shown(await post<Sharing>("get_sharing", { project, kind, id }));
    } catch (e) {
      setStatus(t("sharing.loadFailed", { error: (e as Error).message }));
    }
  };
  const save = async (token?: string) => {
    setStatus("");
    const r = await confirmable("set_sharing", { project, kind, id, users: names(users), workspace }, token);
    if (r.confirm) return setConfirm(r.confirm); // Confirm takes focus (autoFocus)
    setConfirm(null);
    if (!r.ok) {
      setStatus(t("sharing.failed", { error: r.error }));
      return focusSoon("sharing-users");
    }
    shown(r.json as Sharing);
    setStatus(t("sharing.saved"));
    focusSoon("sharing-current-h"); // WCAG 2.4.3: the Confirm button unmounts
  };
  return (
    <>
      <button id="sharing-open" className="sm" onClick={show}>{t("sharing.open")}</button>
      <dialog ref={dlg} className="share-dialog" aria-labelledby="sharing-h" onClose={() => (setConfirm(null), focusSoon("sharing-open"))}>
        <h2 id="sharing-h">{t("sharing.heading", { name })}</h2>
        <p className="muted">{t("sharing.explain")}</p>
        <h3 id="sharing-current-h" tabIndex={-1}>{t("sharing.current")}</h3>
        {!current ? <p role="status">{t("common.loading")}</p> : !current.users.length && !current.workspace ? <p className="muted">{t("sharing.nobody")}</p> : (
          <ul aria-labelledby="sharing-current-h">
            {current.workspace && <li>{t("sharing.everyone")}</li>}
            {current.users.map((u) => <li key={u}>{u}</li>)}
          </ul>
        )}
        <form onSubmit={(e) => (e.preventDefault(), save())}>
          <label>{t("sharing.users")} <input id="sharing-users" size={40} value={users} onChange={(e) => setUsers(e.target.value)} aria-describedby="sharing-users-hint" /></label>
          <p id="sharing-users-hint" className="muted">{t("sharing.usersHint")}</p>
          <label><input id="sharing-workspace" type="checkbox" checked={workspace} onChange={(e) => setWorkspace(e.target.checked)} /> {t("sharing.workspace")}</label>
          <div className="row"><button id="sharing-save" type="submit" className="primary" disabled={!current}>{t("common.save")}</button></div>
        </form>
        {confirm && (
          <Confirm label={t("sharing.confirmLabel")} message={confirm.message} onConfirm={() => save(confirm.token)}
            onCancel={() => setConfirm(null)} back="sharing-save" />
        )}
        <p role="status" className="muted">{status}</p>
        <div className="row"><button onClick={() => dlg.current!.close()}>{t("common.close")}</button></div>
      </dialog>
    </>
  );
}

// UI-1 Data tab: what others shared with you (also with no role anywhere); opens the object's own tab.
export function SharedList({ onOpen }: { onOpen: (tab: string) => void }) {
  const q = useQuery(sharedQuery);
  if (q.error) return <p role="alert">{t("sharing.listFailed", { error: q.error.message })}</p>;
  if (!q.data?.length) return null;
  return (
    <>
      <h2 id="shared-h">{t("sharing.withMe")}</h2>
      <ul aria-labelledby="shared-h">
        {q.data.map((s) => (
          <li key={`${s.project}/${s.kind}/${s.id}`}>
            <button className="link" onClick={() => onOpen(`${s.project}/#${s.kind}/${s.id}`)}>{t("sharing.item", { project: s.project, kind: t(`tab.${s.kind}`), name: s.name })}</button>{" "}
            {s.owner && <span className="muted">{t("sharing.by", { owner: s.owner })}</span>}
          </li>
        ))}
      </ul>
    </>
  );
}

// SE-1 dataset tab ("project/#dataset/<id>"): its rows through the semantic engine as you (RLS applies); Share for Editors.
type Rows = { name: string; columns: string[]; measures: Record<string, string>; rows: Value[][] };
export function Dataset({ project, id }: { project: string; id: string }) {
  const q = useQuery({ queryKey: ["dataset_data", project, id], queryFn: () => post<Rows>("dataset_data", { project, dataset: id }) });
  const me = useQuery(meQuery(project));
  if (q.error) return <p role="alert">{t("dataset.loadFailed", { id, error: q.error.message })}</p>;
  if (!q.data) return <p role="status">{t("common.loading")}</p>;
  const d = q.data;
  return (
    <section className="panel" aria-labelledby="dataset-h">
      {/* UI-DS.10 (design/v2-chat-first Datasets): title, what it is, then its rows in a raised table */}
      <div className="report-head">
        <div>
          <h2 id="dataset-h">{d.name}</h2>
          <p className="muted">{t("dataset.sub")}</p>
        </div>
        <div className="row">{canShare(me.data) && <SharingDialog project={project} kind="dataset" id={id} name={d.name} />}</div>
      </div>
      {d.rows.length === 0 ? <p className="muted">{t("dataset.noRows")}</p> : (
        <div className="table-wrap" tabIndex={0} role="region" aria-labelledby="dataset-h">
          <table>
            <thead><tr>{d.columns.map((c) => <th key={c} scope="col">{d.measures[c] ?? c}</th>)}</tr></thead>
            <tbody>{d.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={typeof v === "number" ? "num" : undefined}>{format(v)}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}
