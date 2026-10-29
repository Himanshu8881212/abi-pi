import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { canDesign, confirmable, flowsQuery, flowStatus, meQuery, qualityQuery, triggerText, type QualityRow } from "./api";
import { ReadOnly } from "./Production";
import { actionErrors, Confirm, Errors, type ActionError } from "./mel";
import { focusSoon, useUi } from "./store";
import { fmtDate, t } from "./i18n";

// IN-8 (E-10.3): a project's data quality per table, as the `data_quality` action delivers it (principle 7: nothing
// computed here), and the "Email alerts" setup (D-41: email only).
const ALERT = "quality-email"; // the one flow this setup writes; finer alerts (per table) are the flow editor's job

function Row({ r }: { r: QualityRow }) {
  const [table, refresh, monitored, last, rows, f, period, missing, , , , problems] = r;
  if (!monitored) return <tr><th scope="row">{table}</th><td>{refresh}</td><td colSpan={5}>{t("quality.notMonitored")}</td></tr>;
  return (
    <tr>
      <th scope="row">{table}</th>
      <td>{refresh}</td>
      <td>{last ? <time dateTime={last}>{fmtDate(last)}</time> : t("quality.never")}</td>
      <td>{rows ?? ""}</td>
      <td>{f === null ? t("quality.noSchedule") : f ? t("quality.fresh") : t("quality.stale")}</td>
      <td>{period ? t("quality.missingBy", { missing: missing ?? "", period }) : t("quality.noDateColumn")}</td>
      <td>{problems.length ? <ul>{[problems].flat().map((p) => <li key={p} className="error">{p}</li>)}</ul> : <span className="status ok">{t("quality.ok")}</span>}</td>
    </tr>
  );
}

export default function Quality({ project }: { project: string }) {
  const q = useQuery(qualityQuery(project));
  const me = useQuery(meQuery(project)).data;
  return (
    // without Alerts (Viewers) the card may scroll with no focusable control in it, so it takes focus itself (WCAG 2.1.1)
    <section aria-labelledby="quality-title" className="quality" tabIndex={canDesign(me) ? undefined : 0}>
      <h2 id="quality-title">{t("quality.title", { project })}</h2>
      {q.isPending && <p role="status">{t("common.loading")}</p>}
      {q.error && <p role="alert">{q.error.message}</p>}
      {q.data?.length === 0 && <p className="muted">{t("quality.noTables")}</p>}
      {!!q.data?.length && (
        <div className="table-wrap" tabIndex={0} role="region" aria-label={t("quality.caption")}>
          <table>
            <caption className="sr-only">{t("quality.caption")}</caption>
            <thead><tr>{[t("quality.table"), t("quality.refresh"), t("quality.lastLoad"), t("quality.rows"), t("quality.freshness"), t("quality.missing"), t("quality.problems")]
              .map((h) => <th key={h} scope="col">{h}</th>)}</tr></thead>
            <tbody>{q.data.map((r) => <Row key={r[0]} r={r} />)}</tbody>
          </table>
        </div>
      )}
      {canDesign(me) ? <Alerts project={project} /> : me && <ReadOnly project={project} why={t("quality.needEditor")} />}
    </section>
  );
}

// The flow `save_flow` writes: once per new problem of any table (FL-2), one email with the server's problem text.
export const alertFlow = (project: string, to: string[]) => ({
  id: ALERT, name: `${project} data-quality email alerts`, enabled: true, trigger: { type: "data_quality" },
  steps: [{ id: "mail", type: "action", action: "send_email",
    input: { to, subject: "Data quality of {{ data.trigger.table }}", body: "{{ data.trigger.message }}" } }],
});

function Alerts({ project }: { project: string }) {
  const flows = useQuery(flowsQuery(project));
  const qc = useQueryClient();
  const openTab = useUi((s) => s.open);
  const alerts = flows.data?.flows.filter((f) => f.trigger.type === "data_quality") ?? [];
  const saved = (alerts.find((f) => f.id === ALERT)?.steps[0]?.input as { to?: string[] } | undefined)?.to;
  const [to, setTo] = useState<string | null>(null); // null: the saved recipients
  const [confirm, setConfirm] = useState<{ message: string; token: string; action: string; body: object } | null>(null);
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [done, setDone] = useState("");

  // save_flow (§5 #3 enable confirmation), then one setup email by hand: FL-6 confirms a new domain there, since an
  // unattended alert run can't click a confirmation. Each 409 shows Confirm, which re-sends with the token.
  async function step(action: string, body: object, token?: string) {
    const r = await confirmable(action, { project, ...body }, token);
    if (r.confirm) return setConfirm({ ...r.confirm, action, body }); // Confirm takes focus
    if (confirm) focusSoon("alert-save"); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) return setErrors(actionErrors(r.status, r.json?.detail));
    setErrors([]);
    if (action === "save_flow") {
      await qc.invalidateQueries({ queryKey: flowsQuery(project).queryKey });
      const list = (body as { flow: ReturnType<typeof alertFlow> }).flow.steps[0].input.to;
      return step("send_email", { to: list, subject: `Data-quality alerts for ${project}`,
        body: `Data-quality alerts of project ${project} are set up: this address gets an email for each new problem.` });
    }
    setDone(t("quality.saved", { to: (body as { to: string[] }).to.join(", ") }));
  }

  function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setDone("");
    const list = String(new FormData(e.currentTarget).get("to")).split(",").map((a) => a.trim()).filter(Boolean);
    step("save_flow", { flow: alertFlow(project, list) });
  }

  return (
    <section aria-labelledby="quality-alerts-h">
      <h3 id="quality-alerts-h">{t("quality.alerts")}</h3>
      {alerts.length > 0 && (
        <ul aria-label={t("quality.flows")}>
          {alerts.map((f) => (
            <li key={f.id}>
              <button className="link" onClick={() => openTab(`${project}/#flow/${f.id}`)}>{f.name}</button>{" "}
              <span className="muted">{`${triggerText(f.trigger)}, ${flowStatus(f, flows.data?.last_runs[f.id])}`}</span>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={save}>
        <label htmlFor="alert-to">{t("quality.sendTo")}</label>
        <input id="alert-to" name="to" type="email" multiple required autoComplete="email"
          value={to ?? saved?.join(", ") ?? ""} onChange={(e) => setTo(e.target.value)} />
        <button id="alert-save" className="primary">{t("quality.save")}</button>
      </form>
      {confirm && (
        <Confirm label={confirm.action === "save_flow" ? t("quality.confirmSaveFlow") : t("quality.confirmSendEmail")} message={confirm.message}
          onConfirm={() => step(confirm.action, confirm.body, confirm.token)} onCancel={() => setConfirm(null)} back="alert-save" />
      )}
      <Errors errors={errors} />
      <p role="status">{done}</p>
    </section>
  );
}
