import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ChangeEvent } from "react";
import { busy, canDesign, confirmable, driftText, meQuery, mlHistoryQuery, mlQuery, post, runStatus, type MlHistoryRun, type MlModel, type MlRun, type MlTask } from "./api";
import { ReadOnly } from "./Production";
import { actionErrors, Errors, type ActionError } from "./mel";
import { useServerChange, useUi } from "./store";
import { fmtDate, fmtNumber, t } from "./i18n";

// C-15: lineage objects that have a workspace tab open it; the rest stay text (a predictions table has no source, so no table tab).
const TABS: Record<string, string> = { measure: "#measure/", report: "#report/", flow: "#flow/", tool: "#tool/", agent: "#agent/" };
export const objectTab = (project: string, obj: string) => {
  const i = obj.indexOf(":");
  return obj.slice(0, i) in TABS ? `${project}/${TABS[obj.slice(0, i)]}${obj.slice(i + 1)}` : null;
};
const num = (v: number | null | undefined) => (v == null ? "" : fmtNumber(v, { maximumFractionDigits: 3 }));

// UI-2 ML model tab ("project/#ml/<id>"): definition, latest training vs baseline, importance, drift, where predictions
// are used, Train and Score (ML-3, ML-4, ML-6).
export default function Ml({ project, id }: { project: string; id: string }) {
  const ml = useQuery(mlQuery(project));
  const me = useQuery(meQuery(project));
  const err = ml.error ?? me.error;
  if (err) return <p role="alert">{t("ml.loadFailed", { project, error: err.message })}</p>;
  if (!ml.data || !me.data) return <p role="status">{t("ml.loading", { id })}</p>;
  const m = ml.data.find((x) => x.id === id);
  if (!m) return <p role="alert">{t("ml.none", { id, project })}</p>;
  return <Model project={project} m={m} readOnly={!canDesign(me.data)} />;
}

export function Model({ project, m, readOnly }: { project: string; m: MlModel; readOnly: boolean }) {
  const qc = useQueryClient();
  const open = useUi((s) => s.open);
  const [error, setError] = useState("");
  const run = m.latest_run, s = m.latest_score, obj = `ml:${m.id}`;
  // §8 item 13 "where predictions are used"; refetched after each scoring (it registers the predictions table)
  const used = useQuery({
    queryKey: ["lineage", project, obj, s?.finished],
    queryFn: () => post<{ objects: string[] }>("lineage", { project, object: obj }),
  });
  const trained = run?.status === "ok";
  const act = async (action: "train_model" | "score_model") => {
    setError("");
    try {
      await post(action, { project, model: m.id });
    } catch (e) {
      setError(action === "train_model" ? t("ml.trainFailed", { error: (e as Error).message }) : t("ml.scoreFailed", { error: (e as Error).message }));
    }
    await qc.invalidateQueries({ queryKey: ["ml", project] });
  };
  // aria-disabled (not disabled) while a run is queued, so focus stays on the button (WCAG 2.4.3)
  const button = (label: string, action: "train_model" | "score_model", wait: boolean, why: string | null) => (
    <button disabled={readOnly} aria-disabled={wait || !!why || undefined} aria-describedby={readOnly ? "ml-why" : why ? `ml-why-${action}` : undefined}
      onClick={() => !wait && !why && act(action)}>{label}</button>
  );
  const scoreWhy = trained ? null : t("ml.scoreWhy");
  return (
    // read-only, the card scrolls with no focusable control in it, so the card itself takes focus (WCAG 2.1.1, as Task)
    <section aria-labelledby="ml-title" className="ml" tabIndex={readOnly ? 0 : undefined}>
      <div className="section-head">
        <h2 id="ml-title">{m.name} <code className="muted">{obj}</code></h2>
        {button(t("ml.train"), "train_model", busy(run), null)}
        {button(t("ml.score"), "score_model", busy(s), scoreWhy)}
      </div>
      {readOnly && <ReadOnly id="ml-why" project={project} why={t("ml.readOnly", { project })} />}
      {!readOnly && scoreWhy && <p id="ml-why-score_model" className="muted">{scoreWhy}</p>}
      {error && <p role="alert">{error}</p>}
      <Definition project={project} m={m} readOnly={readOnly} />
      <h3>{t("ml.latestTraining")}</h3>
      <p role="status">{runStatus(run, "training")}{run?.method ? ` (${run.method})` : ""}</p>
      {trained && (
        <>
          <p>{run.summary}</p>
          <p><strong>{run.better_than_baseline ? t("ml.betterYes") : t("ml.betterNo")}</strong></p>
          <table>
            <caption>{t("ml.accuracy")}</caption>
            <thead><tr><th scope="col">{t("ml.metric")}</th><th scope="col">{t("ml.model")}</th><th scope="col">{t("ml.baseline")}</th></tr></thead>
            <tbody>{Object.entries(run.metrics ?? {}).map(([k, v]) => (
              <tr key={k}><th scope="row">{k}</th><td>{num(v)}</td><td>{num(run.baseline?.[k])}</td></tr>
            ))}</tbody>
          </table>
          {Object.keys(run.candidates ?? {}).length > 1 && <Candidates run={run} />}
          {!!run.excluded?.length && <p>{t("ml.leakage", { columns: run.excluded.join(", ") })}</p>}
          {!!run.importance?.length && (
            <table>
              <caption>{t("ml.importance")}</caption>
              <thead><tr><th scope="col">{t("ml.feature")}</th><th scope="col">{t("ml.share")}</th></tr></thead>
              <tbody>{run.importance.map((x) => (
                <tr key={x.feature}><th scope="row">{x.name}</th><td>{fmtNumber(x.share, { style: "percent", maximumFractionDigits: 1 })}</td></tr>
              ))}</tbody>
            </table>
          )}
          {m.task === "anomaly" && run.metrics?.anomalies != null && (
            <p>{t("ml.anomalies", { n: num(run.metrics.anomalies), rows: num(run.metrics.rows), base: num(run.baseline?.anomalies) })}</p>
          )}
          {!!run.groups?.length && <Groups groups={run.groups} />}
        </>
      )}
      <History project={project} m={m} readOnly={readOnly} />
      <h3>{t("ml.latestScoring")}</h3>
      <p role="status">{runStatus(s, "scoring")}; {driftText(s)}</p>
      <h3 id="ml-used">{t("ml.used")}</h3>
      {used.error && <p role="alert">{used.error.message}</p>}
      {used.data && (used.data.objects.length === 0 ? <p className="muted">{t("ml.nowhere")}</p> : (
        <ul aria-labelledby="ml-used">
          {used.data.objects.map((o) => {
            const tab = objectTab(project, o);
            // C-19: the predictions table's dimension column (ML-5), to put in a report
            const dim = o === `table:ml_${m.id}` && DIMENSION[m.task];
            return <li key={o}>{tab ? <button className="link" onClick={() => open(tab)}>{o}</button>
              : dim ? `${o} — ${t("ml.dimension", { dimension: `ml_${m.id}.${dim}` })}` : o}</li>;
          })}
        </ul>
      ))}
    </section>
  );
}

// ML-4 cluster profiles in business terms, as the registry holds them (E-04.1 `groups`; principle 7: formatted, never computed)
function Groups({ groups }: { groups: NonNullable<MlRun["groups"]> }) {
  const means = Object.keys(groups[0].mean), common = Object.keys(groups[0].most_common);
  return (
    <table>
      <caption>{t("ml.groups")}</caption>
      <thead><tr><th scope="col">{t("ml.group")}</th><th scope="col">{t("ml.rows")}</th>
        {means.map((c) => <th scope="col" key={`m${c}`}>{t("ml.mean", { name: c })}</th>)}
        {common.map((c) => <th scope="col" key={`c${c}`}>{t("ml.mostCommon", { name: c })}</th>)}</tr></thead>
      <tbody>{groups.map((g) => (
        <tr key={g.cluster}><th scope="row">{g.cluster}</th><td>{num(g.rows)}</td>
          {means.map((c) => <td key={`m${c}`}>{num(g.mean[c])}</td>)}
          {common.map((c) => <td key={`c${c}`}>{g.most_common[c] ?? ""}</td>)}</tr>
      ))}</tbody>
    </table>
  );
}

// D-99: both candidates' held-out metrics side by side, the kept one named
function Candidates({ run }: { run: MlRun }) {
  const cands = Object.entries(run.candidates ?? {}), names = [...new Set(cands.flatMap(([, ms]) => Object.keys(ms)))];
  return (
    <table>
      <caption>{t("ml.candidates", { method: run.method ?? "" })}</caption>
      <thead><tr><th scope="col">{t("ml.metric")}</th>
        {cands.map(([c]) => <th scope="col" key={c}>{c === run.method ? t("ml.kept", { method: c }) : c}</th>)}</tr></thead>
      <tbody>{names.map((k) => (
        <tr key={k}><th scope="row">{k}</th>{cands.map(([c, ms]) => <td key={c}>{num(ms[k])}</td>)}</tr>
      ))}</tbody>
    </table>
  );
}

// ML-4 the metric the trend chart follows per task (the one the candidates are compared on, D-99)
const HEADLINE: Record<MlTask, string> = { forecast: "mae", regression: "mae", classification: "balanced_accuracy", clustering: "silhouette", anomaly: "anomalies" };
const when = (r: MlRun) => fmtDate(r.created, { dateStyle: "medium", timeStyle: "short" });
const short = (run: string) => run.slice(0, 8); // runs of one minute differ by ID (distinct Pin names, WCAG 2.4.6)
const pct = (v: number, o: Intl.NumberFormatOptions = {}) => fmtNumber(v, { style: "percent", maximumFractionDigits: 1, ...o });

// ML-4 (D-99) run history: metrics vs baseline per run, a small trend chart, importance change against the previous run,
// and which run scoring uses with a pin/unpin control (pin_ml_run)
function History({ project, m, readOnly }: { project: string; m: MlModel; readOnly: boolean }) {
  const qc = useQueryClient();
  const h = useQuery(mlHistoryQuery(project, m.id, m.latest_run?.finished));
  const [error, setError] = useState("");
  if (h.error) return <p role="alert">{h.error.message}</p>;
  if (!h.data) return <p role="status">{t("ml.historyLoading")}</p>;
  const runs = h.data, a = m.active_run, active = runs.find((r) => r.run === a?.run);
  const names = [...new Set(runs.flatMap((r) => Object.keys(r.metrics ?? {})))];
  const pin = async (run: string | null) => {
    setError("");
    try {
      await post("pin_ml_run", { project, model: m.id, run });
    } catch (e) {
      setError(t("ml.pinFailed", { error: (e as Error).message }));
    }
    await qc.invalidateQueries({ queryKey: ["ml", project] });
  };
  return (
    <>
      <h3>{t("ml.runs")}</h3>
      <p>{a ? t("ml.activeRun", { run: short(a.run), date: active ? when(active) : "", method: active?.method ?? "" }) : t("ml.noActive")}
        {a && ` ${a.pinned ? t("ml.whyPinned") : t("ml.whyLatest")}`}</p>
      {error && <p role="alert">{error}</p>}
      <Trend runs={runs} metric={HEADLINE[m.task]} />
      {runs.length > 0 && (
        <table>
          <caption>{t("ml.runsCaption", { n: runs.length })}</caption>
          <thead><tr><th scope="col">{t("ml.started")}</th><th scope="col">{t("ml.runStatus")}</th><th scope="col">{t("ml.method")}</th>
            {names.map((k) => <th scope="col" key={k}>{k}</th>)}
            <th scope="col">{t("ml.importanceChange")}</th><th scope="col">{t("ml.scoringUses")}</th></tr></thead>
          <tbody>{runs.map((r) => (
            <tr key={r.run}><th scope="row">{when(r)} <code>{short(r.run)}</code></th><td>{runStatus(r, "training")}</td><td>{r.method ?? ""}</td>
              {names.map((k) => <td key={k}>{r.metrics?.[k] == null ? "" : r.baseline?.[k] == null ? num(r.metrics[k])
                : t("ml.vsBaseline", { value: num(r.metrics[k]), baseline: num(r.baseline[k]) })}</td>)}
              <td><Changes r={r} /></td>
              <td>{r.active && <>{t("ml.activeYes")} </>}
                {r.status === "ok" && (
                  <button disabled={readOnly} aria-describedby={readOnly ? "ml-why" : undefined} onClick={() => pin(r.pinned ? null : r.run)}
                    aria-label={t(r.pinned ? "ml.unpinRun" : "ml.pinRun", { run: short(r.run), date: when(r) })}>{r.pinned ? t("ml.unpin") : t("ml.pin")}</button>
                )}</td></tr>
          ))}</tbody>
        </table>
      )}
    </>
  );
}

// ML-4 importance change against the previous successful run, largest first (registry figures, formatted only)
function Changes({ r }: { r: MlHistoryRun }) {
  const ch = r.importance_change?.filter((x) => x.change !== 0);
  if (!ch) return null; // the first run: nothing to compare with
  if (!ch.length) return <>{t("ml.noChange")}</>;
  const signed = (v: number) => pct(v, { signDisplay: "exceptZero" });
  return (
    <details><summary>{t("ml.changes", { name: ch[0].name, change: signed(ch[0].change) })}</summary>
      <ul>{ch.map((x) => <li key={x.feature}>{t("ml.change", { name: x.name, change: signed(x.change), previous: pct(x.previous), share: pct(x.share) })}</li>)}</ul>
    </details>
  );
}

// ML-4 the headline metric of the successful runs, oldest first: model solid, baseline dashed (WCAG 1.4.1: not by colour
// alone); the history table holds every value as text (1.1.1). Places registry values only (principle 7).
function Trend({ runs, metric }: { runs: MlRun[]; metric: string }) {
  const pts = runs.filter((r) => r.status === "ok" && r.metrics?.[metric] != null).reverse();
  if (pts.length < 2) return null;
  const vals = pts.flatMap((r) => [r.metrics![metric], r.baseline?.[metric]]).filter((v): v is number => v != null);
  const lo = Math.min(...vals), span = Math.max(...vals) - lo || 1, W = 320, H = 80, P = 6;
  const line = (v: (r: MlRun) => number | null | undefined) => pts.flatMap((r, i) => {
    const y = v(r);
    return y == null ? [] : [`${P + (i * (W - 2 * P)) / (pts.length - 1)},${H - P - ((y - lo) * (H - 2 * P)) / span}`];
  }).join(" ");
  const base = line((r) => r.baseline?.[metric]);
  return (
    <figure className="trend">
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t("ml.trend", { metric, n: pts.length,
        first: num(pts[0].metrics![metric]), last: num(pts[pts.length - 1].metrics![metric]) })}>
        {base && <polyline points={base} fill="none" stroke="var(--border-strong)" strokeWidth="2" strokeDasharray="5 4" />}
        <polyline points={line((r) => r.metrics![metric])} fill="none" stroke="var(--navy)" strokeWidth="2" />
      </svg>
      <figcaption>{t("ml.trendLegend", { metric })}</figcaption>
    </figure>
  );
}

// The tasks and their methods of schemas/v1/ml.json (C-18)
const METHODS: Record<MlTask, string[]> = { forecast: ["auto", "lightgbm", "ets"], classification: ["auto", "lightgbm", "random_forest"],
  regression: ["auto", "lightgbm", "random_forest"], clustering: ["auto", "kmeans", "gmm"], anomaly: ["auto", "isolation_forest", "seasonal"] };
const DIMENSION: Partial<Record<MlTask, string>> = { classification: "prediction", clustering: "cluster", anomaly: "anomaly" };
const supervised = (task: MlTask) => task !== "clustering" && task !== "anomaly";
type Draft = Record<"name" | "description" | "dataset" | "target" | "time" | "features" | "horizon" | "k" | "method", string> & { task: MlTask };
const draftOf = (m: MlModel): Draft => ({ name: m.name, description: m.description ?? "", task: m.task, dataset: m.dataset, target: m.target ?? "",
  time: m.time ?? "", features: m.features?.join(", ") ?? "", horizon: String(m.horizon ?? ""), k: String(m.k ?? ""), method: m.method ?? "" });

// C-18: the file keeps only what its task takes, so switching the task never saves a field the schema refuses
export function mlFile(id: string, d: Draft) {
  const features = d.features.split(",").map((x) => x.trim()).filter(Boolean);
  return { id, name: d.name, description: d.description || undefined, task: d.task, dataset: d.dataset,
    target: supervised(d.task) ? d.target : undefined, features: d.task !== "forecast" && features.length ? features : undefined,
    time: d.time || undefined, horizon: d.task === "forecast" && d.horizon ? Number(d.horizon) : undefined,
    k: d.task === "clustering" && d.k ? Number(d.k) : undefined, method: METHODS[d.task].includes(d.method) ? d.method : undefined };
}

// C-17 the model editor: saves ml/<id>.json through save_ml_model (PC-3 checks; refusals with file, JSON path and fix)
export function Definition({ project, m, readOnly }: { project: string; m: MlModel; readOnly: boolean }) {
  const qc = useQueryClient();
  const [d, setD] = useState(() => draftOf(m));
  useServerChange(m, (now, old) => { if (JSON.stringify(d) === JSON.stringify(draftOf(old))) setD(draftOf(now)); });
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [status, setStatus] = useState("");
  const set = (k: keyof Draft) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setD({ ...d, [k]: e.target.value });
  const save = async () => {
    setErrors([]);
    setStatus("");
    const r = await confirmable("save_ml_model", { project, ml: mlFile(m.id, d) });
    if (!r.ok) return setErrors(r.status ? actionErrors(r.status, r.json?.detail) : [{ error: r.error }]); // status 0: no answer
    setStatus(t("ml.saved", { id: m.id }));
    await qc.invalidateQueries({ queryKey: ["ml", project] });
  };
  const field = (k: keyof Draft, extra: object = {}, help?: string) => (
    <>
      <label>{t(`ml.${k}`)} <input value={d[k]} onChange={set(k)} aria-describedby={help && `ml-${k}-help`} {...extra} /></label>
      {help && <p id={`ml-${k}-help`} className="muted">{help}</p>}
    </>
  );
  return (
    <form aria-labelledby="ml-def" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <fieldset disabled={readOnly} aria-describedby={readOnly ? "ml-why" : undefined}>
        <legend id="ml-def">{t("ml.definition")}</legend>
        {field("name", { required: true })}
        {field("description")}
        <label>{t("ml.task")} <select value={d.task} onChange={(e) => {
          const task = e.target.value as MlTask; // a method the new task lacks falls back to its default
          setD({ ...d, task, method: METHODS[task].includes(d.method) ? d.method : "" });
        }}>{Object.keys(METHODS).map((x) => <option key={x} value={x}>{x}</option>)}</select></label>
        {field("dataset", { required: true })}
        {supervised(d.task) && field("target", { required: true })}
        {field("time", { required: d.task === "forecast" || d.method === "seasonal" }, t("ml.timeHelp"))}
        {d.task !== "forecast" && field("features", {}, t("ml.featuresHelp"))}
        {d.task === "forecast" && field("horizon", { type: "number", min: 1, max: 60 })}
        {d.task === "clustering" && field("k", { type: "number", min: 2, max: 10 }, t("ml.kHelp"))}
        <label>{t("ml.method")} <select value={d.method} onChange={set("method")}>
          <option value="">{t("ml.methodDefault")}</option>
          {METHODS[d.task].map((x) => <option key={x} value={x}>{x}</option>)}
        </select></label>
        <button type="submit" className="primary">{t("common.save")}</button>
      </fieldset>
      <Errors errors={errors} />
      {status && <p role="status">{status}</p>}
    </form>
  );
}
