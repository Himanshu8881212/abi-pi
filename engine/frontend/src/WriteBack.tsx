import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { confirmable, post, tablesQuery } from "./api";
import { Confirm } from "./mel";
import { focusSoon } from "./store";
import { t } from "./i18n";

const PAGE = 20;
const REVIEW = 1000; // edit_history's largest page
type Hist = { columns: string[]; entries: (string | null)[][] }; // D-122: `rows` blanks proposed values
type Entry = Record<string, string | null>; // an edit_history row by column name
type Proposal = { batch: string; table: string; user: string; timestamp: string; comment: string | null; changes: { row: Record<string, unknown>; column: string | null; old: string | null; new: string | null }[] };
export type Cell = { key: (string | null)[]; column: string | null; label: string }; // the grid's selected cell (column null: a key column, the whole row)

const entries = (h: Hist): Entry[] => h.entries.map((r) => Object.fromEntries(h.columns.map((c, i) => [c, r[i]])));
/** An edit's row key (JSON {column: value}) as "id 1, region EU". */
export const keyText = (row: string | Record<string, unknown>) =>
  Object.entries(typeof row === "string" ? JSON.parse(row) : row).map(([c, v]) => `${c} ${v}`).join(", ");
/** WB-8: the history rows that carry an Undo button: the first shown row of each batch that still applies. */
export const undoRows = (rows: Entry[]) => rows.map((r, i) => r.status === "active" && rows.findIndex((x) => x.batch_id === r.batch_id) === i);
const time = (ts: string | null) => (ts ? t("wb.utc", { time: ts.slice(0, 19).replace("T", " ") }) : "");
const val = (v: string | null) => v ?? t("wb.empty");
type Plan = { message: string; token: string; preview: { changes: Proposal["changes"]; skipped: Skip[] } };
type Skip = { row: Record<string, unknown>; column: string | null; error: string };
type RowError = { row: Record<string, unknown>; column: string | null; error: string; source_value: string | null };
const skipText = (s: Skip) => `${keyText(s.row)}, ${s.column ?? t("wb.row")}: ${s.error}`;
/** WB-10 sync_edits outcome (a 409 detail or the result plus a message) as text: rows that failed and edits skipped. */
export const syncReport = (d: string | { message?: string; rows?: RowError[]; skipped?: Skip[] }) => typeof d === "string"
  ? { message: d, rows: [], skipped: [] }
  : { message: d.message ?? "", skipped: (d.skipped ?? []).map(skipText),
      rows: (d.rows ?? []).map((r) => t("wb.rowError", { key: keyText(r.row), where: r.column ? t("wb.column", { column: r.column }) : t("wb.wholeRow"), error: r.error, value: val(r.source_value) })) };

// W-04.2 write-back panel of a keyed table: conflicts and orphaned edits to review (WB-6), agent proposals to approve
// or reject (WB-9), and the edit history per table or selected cell with Undo per batch (WB-8). Editors act; Viewers read.
export default function WriteBack({ project, table, editor, owner, cell, goTo, onChanged }: {
  project: string; table: string; editor: boolean; owner: boolean; cell: Cell | null;
  goTo: (rowKey: string, column: string | null) => Promise<string | null>; onChanged: () => void;
}) {
  const [scope, setScope] = useState<"table" | "cell">("table");
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const [confirm, setConfirm] = useState<{ batch: string; message: string; token: string } | null>(null);
  const qk = ["writeback", project, table];
  const byCell = scope === "cell" && !!cell;
  const target = byCell ? { key: cell.key, ...(cell.column && { column: cell.column }) } : {};
  const history = (body: object) => post<Hist>("edit_history", { project, table, ...body }).then(entries);
  const review = useQuery({ queryKey: [...qk, "review"], queryFn: () => history({ flagged: true, limit: REVIEW }) });
  const hist = useQuery({ queryKey: [...qk, "history", target, offset], queryFn: () => history({ ...target, offset, limit: PAGE }) });
  const proposals = useQuery({
    queryKey: [...qk, "proposals"],
    queryFn: async () => (await post<{ proposals: Proposal[] }>("edit_proposals", { project })).proposals.filter((p) => p.table === table),
  });
  const sync = useQuery({ ...tablesQuery, select: (rs) => rs.some((r) => r.project === project && r.table === table && r.sync) });
  const [plan, setPlan] = useState<Plan | null>(null);
  const [report, setReport] = useState<ReturnType<typeof syncReport> | null>(null);
  const ids = { review: `wb-review-${table}`, props: `wb-proposals-${table}`, hist: `wb-history-${table}`, sync: `wb-sync-${table}`, btn: `sync-${table}` };

  const done = (text: string, focus: string) => {
    setAlert("");
    setStatus(text);
    onChanged(); // the grid, every visual and this panel refetch
    focusSoon(focus); // WCAG 2.4.3: the clicked button unmounts with its proposal or history row
  };
  const fail = (e: unknown, focus: string) => (setAlert((e as Error).message), focusSoon(focus));
  // WB-9: Approve asks the server, which answers with the preview and a confirm token; only the Confirm click sends it
  const approve = async (batch: string, token?: string) => {
    setAlert("");
    const r = await confirmable("approve_edit", { project, table, batch }, token);
    if (r.confirm) return setConfirm({ batch, ...r.confirm });
    setConfirm(null);
    if (!r.ok) return fail(new Error(r.error), `approve-${batch}`);
    done(t("wb.approved"), ids.props);
  };
  const act = async (name: "reject_edit" | "undo_edits", batch: string, text: string, focus: string, from: string) => {
    try {
      await post(name, { project, table, batch });
      done(text, focus);
    } catch (e) {
      fail(e, from);
    }
  };
  // WB-10: like Approve, the first call only returns the preview and a token; the Confirm click sends it (Owner only)
  const syncNow = async (token?: string) => {
    setReport(null);
    const r = await confirmable("sync_edits", { project, table }, token);
    if (r.confirm) return setPlan(r.confirm as Plan);
    setPlan(null);
    focusSoon(ids.btn); // the Confirm button unmounts
    if (!r.ok) return setReport(syncReport(r.json?.detail ?? r.error));
    const j = r.json;
    setReport(syncReport({ ...j, message: t("wb.wrote", { n: j.written, target: j.target, after: j.reloaded ? t("wb.reloaded") : j.note }) }));
    onChanged();
  };
  const go = async (e: Entry) => setStatus((await goTo(e.row_key!, e.column).catch((x: Error) => x.message)) ?? "");

  const flagged = review.data ?? [];
  const capped = flagged.length === REVIEW;
  const props = proposals.data ?? [];
  const rows = hist.data ?? [];
  const undo = undoRows(rows);
  const err = review.error ?? hist.error ?? proposals.error;
  return (
    <details className="writeback">
      <summary>{t("wb.summary", { flagged: flagged.length, more: capped ? "+" : "", proposals: props.length })}</summary>
      <p role="status" className="muted">{status}</p>
      {(alert || err) && <p role="alert" className="error">{alert || err!.message}</p>}

      <section aria-labelledby={ids.review}>
        <h3 id={ids.review} tabIndex={-1}>{t("wb.review")}</h3>
        {capped && <p>{t("wb.capped", { n: REVIEW })}</p>}
        {flagged.length ? (
          <table>
            <thead><tr><th>{t("wb.key")}</th><th>{t("wb.columnH")}</th><th>{t("wb.flag")}</th><th>{t("wb.base")}</th><th>{t("wb.sourceNow")}</th><th>{t("wb.old")}</th><th>{t("wb.new")}</th><th>{t("wb.user")}</th><th>{t("wb.time")}</th><th><span className="sr-only">{t("wb.cell")}</span></th></tr></thead>
            <tbody>
              {flagged.map((e) => (
                <tr key={`${e.batch_id}${e.row_key}${e.column}`}>
                  <td>{keyText(e.row_key!)}</td><td>{e.column ?? t("wb.row")}</td>
                  <td>{e.flag === "conflict" ? t("wb.conflict") : t("wb.orphaned")}</td>
                  <td>{val(e.base_value)}</td><td>{e.flag === "orphaned" ? t("wb.gone") : val(e.source_value)}</td><td>{val(e.old_value)}</td><td>{val(e.new_value)}</td><td>{e.user}</td><td>{time(e.timestamp)}</td>
                  <td><button className="link" onClick={() => go(e)}>{t("wb.goTo")}<span className="sr-only">{t("wb.goToWhich", { column: e.column ?? "", key: keyText(e.row_key!) })}</span></button></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">{review.isPending ? t("common.loading") : t("wb.noReview")}</p>}
      </section>

      {sync.data && (
        <section aria-labelledby={ids.sync}>
          <h3 id={ids.sync}>{t("wb.sync")}</h3>
          <div className="row">
            <button id={ids.btn} aria-disabled={!owner} aria-describedby={owner ? undefined : `${ids.btn}-why`} onClick={() => owner && syncNow()}>{t("wb.syncNow")}</button>
          </div>
          {!owner && <p id={`${ids.btn}-why`} className="muted">{t("wb.ownerOnly")}</p>}
          {plan && (
            <Confirm label={t("wb.confirmSync")} message={plan.message} ok={t("wb.confirmSync")} onConfirm={() => syncNow(plan.token)}
              onCancel={() => setPlan(null)} back={ids.btn}>
              <table>
                <thead><tr><th>{t("wb.key")}</th><th>{t("wb.columnH")}</th><th>{t("wb.old")}</th><th>{t("wb.new")}</th></tr></thead>
                <tbody>
                  {plan.preview.changes.map((c, i) => (
                    <tr key={i}><td>{keyText(c.row)}</td><td>{c.column ?? (c.old === null ? t("wb.rowAdded") : c.new === null ? t("wb.rowDeleted") : t("wb.row"))}</td><td>{val(c.old)}</td><td>{val(c.new)}</td></tr>
                  ))}
                </tbody>
              </table>
              {!!plan.preview.skipped.length && <ul aria-label={t("wb.notSynced")}>{plan.preview.skipped.map((s, i) => <li key={i}>{skipText(s)}</li>)}</ul>}
            </Confirm>
          )}
          <div aria-live="polite">
            {report && <p>{report.message}</p>}
            {!!report?.rows.length && <ul aria-label={t("wb.notWritten")}>{report.rows.map((s, i) => <li key={i}>{s}</li>)}</ul>}
            {!!report?.skipped.length && <ul aria-label={t("wb.notSynced")}>{report.skipped.map((s, i) => <li key={i}>{s}</li>)}</ul>}
          </div>
        </section>
      )}

      <section aria-labelledby={ids.props}>
        <h3 id={ids.props} tabIndex={-1}>{t("wb.proposals")}</h3>
        {!props.length && <p className="muted">{proposals.isPending ? t("common.loading") : t("wb.noProposals")}</p>}
        {props.map((p) => (
          <div key={p.batch} className="proposal" role="group" aria-label={t("wb.proposalBy", { user: p.user })}>
            <p>{t("wb.by", { user: p.user, time: time(p.timestamp) })}{p.comment && `: ${p.comment}`}</p>
            <table>
              <thead><tr><th>{t("wb.key")}</th><th>{t("wb.columnH")}</th><th>{t("wb.old")}</th><th>{t("wb.new")}</th></tr></thead>
              <tbody>
                {p.changes.map((c, i) => (
                  <tr key={i}><td>{keyText(c.row)}</td><td>{c.column ?? t("wb.row")}</td><td>{val(c.old)}</td><td>{val(c.new)}</td></tr>
                ))}
              </tbody>
            </table>
            {editor && (
              <div className="row">
                <button id={`approve-${p.batch}`} onClick={() => approve(p.batch)}>{t("wb.approve")}</button>
                <button id={`reject-${p.batch}`} onClick={() => act("reject_edit", p.batch, t("wb.rejected"), ids.props, `reject-${p.batch}`)}>{t("wb.reject")}</button>
              </div>
            )}
            {confirm?.batch === p.batch && (
              <Confirm label={t("wb.confirmApprove")} message={confirm.message} ok={t("wb.confirmApprove")} onConfirm={() => approve(p.batch, confirm.token)}
                onCancel={() => setConfirm(null)} back={`approve-${p.batch}`} />
            )}
          </div>
        ))}
      </section>

      <section aria-labelledby={ids.hist}>
        <h3 id={ids.hist} tabIndex={-1}>{t("wb.history")}</h3>
        <fieldset className="row">
          <legend className="sr-only">{t("wb.historyOf")}</legend>
          <label><input type="radio" name={`scope-${table}`} checked={!byCell} onChange={() => (setScope("table"), setOffset(0))} /> {t("wb.wholeTable")}</label>
          <label><input type="radio" name={`scope-${table}`} checked={byCell} disabled={!cell} onChange={() => (setScope("cell"), setOffset(0))} />
            {" "}{t("wb.selectedCell")}{cell ? `: ${cell.label}` : t("wb.selectOne")}</label>
        </fieldset>
        {rows.length ? (
          <table>
            <thead><tr><th>{t("wb.time")}</th><th>{t("wb.user")}</th><th>{t("wb.key")}</th><th>{t("wb.columnH")}</th><th>{t("wb.old")}</th><th>{t("wb.new")}</th><th>{t("wb.status")}</th><th>{t("wb.flag")}</th><th>{t("wb.reviewed")}</th><th><span className="sr-only">{t("wb.undo")}</span></th></tr></thead>
            <tbody>
              {rows.map((e, i) => (
                <tr key={`${e.batch_id}${e.row_key}${e.column}`}>
                  <td>{time(e.timestamp)}</td><td>{e.user}</td><td>{keyText(e.row_key!)}</td><td>{e.column ?? t("wb.row")}</td>
                  <td>{val(e.old_value)}</td><td>{val(e.new_value)}</td><td>{e.status}</td><td>{e.flag ?? ""}</td>
                  <td>{e.reviewed_by ? `${e.reviewed_by}, ${time(e.reviewed_at)}` : ""}</td>
                  <td>{editor && undo[i] && (
                    <button id={`undo-${e.batch_id}`} className="sm" onClick={() => act("undo_edits", e.batch_id!, t("wb.undone"), ids.hist, `undo-${e.batch_id}`)}>
                      {t("wb.undoBatch")}<span className="sr-only">{t("wb.undoWhich", { batch: e.batch_id!.slice(0, 7), time: time(e.timestamp) })}</span>
                    </button>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="muted">{hist.isPending ? t("common.loading") : t("wb.noEdits")}</p>}
        {/* aria-disabled, not disabled: a disabled button drops focus to <body> (WCAG 2.4.3) */}
        <div className="row">
          <button aria-disabled={!offset} onClick={() => offset && setOffset(Math.max(0, offset - PAGE))}>{t("wb.newer")}</button>
          <button aria-disabled={rows.length < PAGE} onClick={() => rows.length === PAGE && setOffset(offset + PAGE)}>{t("wb.older")}</button>
        </div>
      </section>
    </details>
  );
}
