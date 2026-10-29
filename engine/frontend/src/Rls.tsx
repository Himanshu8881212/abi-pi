import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { confirmable, rlsQuery, type RlsRule, type RlsRules } from "./api";
import { t } from "./i18n";
import { Confirm } from "./mel";
import { focusSoon } from "./store";

type Form = { id: string; table: string; column: string; values: Record<string, string> }; // user ID -> "a, b"
const EMPTY: Form = { id: "", table: "", column: "", values: {} };
// ponytail: comma-separated text, so a value holding a comma can't be entered; per-value chips if anyone needs one
export const split = (text: string) => text.split(",").map((v) => v.trim()).filter(Boolean);
const joined = (vs: unknown[]) => vs.map(String).join(", ");
export const ruleBody = (project: string, f: Form) => ({
  project, id: f.id.trim(), table: f.table.trim(), column: f.column.trim(),
  users: Object.fromEntries(Object.entries(f.values).map(([u, v]) => [u, split(v)]).filter(([, v]) => v.length)),
});
// D-110 column rule body: the columns as comma-separated names
export const columnBody = (project: string, c: Cols) => ({ project, id: c.id.trim(), table: c.table.trim(), columns: split(c.columns) });
type Cols = { id: string; table: string; columns: string };
const NO_COLS: Cols = { id: "", table: "", columns: "" };
const formOf = (r: RlsRule): Form => ({ id: r.id, table: r.table, column: r.column, values: Object.fromEntries(r.users.map((u) => [u.id, joined(u.values)])) });

/** SC-3 / D-42 rules screen in the project's Changes tab: the Owner edits rules with a confirmation; a restricted Viewer
 *  sees that rules apply and their own values only; everyone reads the D-46 line (rules filter data, not written text). */
export function RlsView({ data, project }: { data: RlsRules; project: string }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<Form>(EMPTY);
  const [cols, setCols] = useState<Cols>(NO_COLS);
  const [confirm, setConfirm] = useState<{ name: string; body: object; from: string; message: string; token: string } | null>(null);
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const owner = !!data.accounts;
  const columns = data.columns ?? [];
  const first = !data.rules.length && !columns.length;

  const send = async (name: string, body: object, from: string, token?: string) => {
    setAlert("");
    setStatus("");
    const r = await confirmable(name, body, token);
    if (r.confirm) return setConfirm({ name, body, from, ...r.confirm }); // Confirm takes focus (autoFocus)
    setConfirm(null);
    focusSoon(!r.ok || name !== "delete_rls_rule" ? from : "rls-id"); // WCAG 2.4.3: the Confirm button (or the deleted row) is gone
    if (!r.ok) return setAlert(t("rls.failed", { error: r.error ?? "" }));
    if (name === "set_rls_rule") setForm(EMPTY);
    if (name === "set_column_rule") setCols(NO_COLS);
    setStatus(name === "delete_rls_rule" ? t("rls.deleted") : t("rls.saved"));
    qc.invalidateQueries({ queryKey: ["rls_rules", project] });
    qc.invalidateQueries({ queryKey: ["changes", project] });
  };

  return (
    <section aria-labelledby="rls-h" className="rls">
      <h2 id="rls-h">{t("rls.title")}</h2>
      <p className="muted">{t("rls.dataOnly")}</p>
      {data.restricted ? (
        <>
          <p>{t("rls.applies")}</p>
          <ul>{data.rules.map((r, i) => (
            <li key={i}>{"values" in r && r.values.length ? t("rls.yours", { table: r.table, column: r.column, values: joined(r.values) })
              : t("rls.yoursNone", { table: r.table })}</li>
          ))}{columns.map((c, i) => <li key={`c${i}`}>{t("rls.colYours", { table: c.table, columns: joined(c.columns) })}</li>)}</ul>
        </>
      ) : first ? <p>{t("rls.none")}</p> : !!data.rules.length && (
        <table className="branches">
          <caption className="muted">{t("rls.rules")}</caption>
          <thead><tr><th scope="col">{t("rls.id")}</th><th scope="col">{t("rls.table")}</th><th scope="col">{t("rls.column")}</th>
            <th scope="col">{t("rls.users")}</th>{owner && <th scope="col">{t("rls.action")}</th>}</tr></thead>
          <tbody>{(data.rules as RlsRule[]).map((r) => (
            <tr key={r.id}>
              <th scope="row">{r.id}</th><td>{r.table}</td><td>{r.column}</td>
              <td>{r.users.map((u) => t("rls.userValues", { name: u.name, values: joined(u.values) })).join("; ")}</td>
              {owner && <td className="row">
                <button onClick={() => { setForm(formOf(r)); focusSoon("rls-id"); }}>{t("rls.edit", { id: r.id })}</button>
                <button id={`rls-del-${r.id}`} onClick={() => send("delete_rls_rule", { project, id: r.id }, `rls-del-${r.id}`)}>{t("rls.delete", { id: r.id })}</button>
              </td>}
            </tr>
          ))}</tbody>
        </table>
      )}
      {!data.restricted && !!columns.length && (
        <table className="branches">
          <caption className="muted">{t("rls.colRules")}</caption>
          <thead><tr><th scope="col">{t("rls.id")}</th><th scope="col">{t("rls.table")}</th><th scope="col">{t("rls.colHidden")}</th>
            {owner && <th scope="col">{t("rls.action")}</th>}</tr></thead>
          <tbody>{columns.map((c) => (
            <tr key={c.id}>
              <th scope="row">{c.id}</th><td>{c.table}</td><td>{joined(c.columns)}</td>
              {owner && <td className="row">
                <button onClick={() => { setCols({ id: c.id ?? "", table: c.table, columns: joined(c.columns) }); focusSoon("col-id"); }}>{t("rls.edit", { id: c.id ?? "" })}</button>
                <button id={`rls-del-${c.id}`} onClick={() => send("delete_rls_rule", { project, id: c.id }, `rls-del-${c.id}`)}>{t("rls.delete", { id: c.id ?? "" })}</button>
              </td>}
            </tr>
          ))}</tbody>
        </table>
      )}
      {owner && (
        <form onSubmit={(e) => { e.preventDefault(); send("set_rls_rule", ruleBody(project, form), "rls-save"); }}>
          <h3>{t("rls.addTitle")}</h3>
          <div className="row">
            <label>{t("rls.id")} <input id="rls-id" required value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value })} /></label>
            <label>{t("rls.table")} <input id="rls-table" required value={form.table} onChange={(e) => setForm({ ...form, table: e.target.value })} /></label>
            <label>{t("rls.column")} <input id="rls-column" required value={form.column} onChange={(e) => setForm({ ...form, column: e.target.value })} /></label>
          </div>
          <fieldset>
            <legend>{t("rls.valuesLegend")}</legend>
            {data.accounts!.filter((a) => a.role === "viewer" || form.values[a.id] !== undefined).map((a) => (
              <label key={a.id} className="row">{a.name}
                <input id={`rls-user-${a.id}`} value={form.values[a.id] ?? ""} placeholder={t("rls.valuesExample")}
                  onChange={(e) => setForm({ ...form, values: { ...form.values, [a.id]: e.target.value } })} /></label>
            ))}
          </fieldset>
          <button id="rls-save" type="submit" className="primary">{t("rls.save")}</button>
        </form>
      )}
      {owner && (
        <form onSubmit={(e) => { e.preventDefault(); send("set_column_rule", columnBody(project, cols), "col-save"); }}>
          <h3>{t("rls.colAddTitle")}</h3>
          <div className="row">
            <label>{t("rls.id")} <input id="col-id" required value={cols.id} onChange={(e) => setCols({ ...cols, id: e.target.value })} /></label>
            <label>{t("rls.table")} <input id="col-table" required value={cols.table} onChange={(e) => setCols({ ...cols, table: e.target.value })} /></label>
            <label>{t("rls.colColumns")} <input id="col-columns" required value={cols.columns} placeholder={t("rls.colExample")}
              onChange={(e) => setCols({ ...cols, columns: e.target.value })} /></label>
          </div>
          <button id="col-save" type="submit" className="primary">{t("rls.colSave")}</button>
        </form>
      )}
      {confirm && (
        <Confirm label={t("rls.confirmLabel")} message={confirm.message} onConfirm={() => send(confirm.name, confirm.body, confirm.from, confirm.token)}
          onCancel={() => setConfirm(null)} back={confirm.from}>
          {first && <p>{t("rls.firstNotice")}</p>}
        </Confirm>
      )}
      <p role="status" className="muted">{status}</p>
      {alert && <p role="alert" className="error">{alert}</p>}
    </section>
  );
}

export default function Rls({ project }: { project: string }) {
  const q = useQuery(rlsQuery(project));
  if (q.error) return <p role="alert">{t("rls.loadFailed", { error: q.error.message })}</p>;
  if (!q.data) return <p role="status">{t("common.loading")}</p>;
  return <RlsView data={q.data} project={project} />;
}
