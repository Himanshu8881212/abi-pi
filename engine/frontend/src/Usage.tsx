import { useQuery } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { download, usageQuery } from "./api";
import { fmtDate, fmtNumber, t } from "./i18n";
import { level } from "./Effort";

const DAY = 86_400_000;
const day = (iso: string) => fmtDate(iso, { dateStyle: "medium", timeZone: "UTC" }); // ISO dates are UTC midnights

// One figure as the `usage` action returned it, in the user's locale (UI-5); the browser computes none (principle 7).
function figure(column: string, v: string | number | null): string {
  if (column === "tenant") return (v as string | null) ?? t("usage.platform");
  if (column === "project") return (v as string | null) ?? t("usage.workspace");
  if (column === "effort") return v === null ? t("usage.noEffort") : level(v as string); // D-71
  if (v === null) return t("usage.none");
  if (column === "llm_cost") return fmtNumber(v as number, { style: "currency", currency: "USD", maximumFractionDigits: 6 }); // AG-6 prices are USD
  if (column.endsWith("_bytes")) return fmtNumber(v as number, { style: "unit", unit: "byte", unitDisplay: "long" });
  return fmtNumber(v as number);
}

// SC-11 (E-07.3) workspace tab "*/#usage": usage per tenant and project for a UTC period, with the action's totals and
// the CSV billing export. Workspace owners see every tenant, a tenant admin its own (the action decides).
export default function Usage() {
  const [period, setPeriod] = useState({ from: "", to: "" }); // empty: the action's default, the current UTC month
  const q = useQuery(usageQuery(period.from, period.to));
  const efforts = useQuery(usageQuery(period.from, period.to, "effort")).data; // D-71, its own call (LLM-2.8.3-fix-3)
  const [error, setError] = useState<string | null>(null);
  const d = q.data;
  const body = d?.rows.slice(0, -1) ?? [], total = d?.rows.at(-1) ?? []; // the action's Total row comes last

  const apply = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setPeriod({ from: String(f.get("from")), to: String(f.get("to")) });
  };
  const csv = () => {
    setError(null);
    download(`/api/usage.csv?from=${d!.from}&to=${d!.to}`, `usage-${d!.from}-${d!.to}.csv`)
      .catch((e: Error) => setError(t("usage.csvFailed", { error: e.message })));
  };

  return (
    <div className="tool usage">
      <h2 id="usage-title">{t("usage.title")}</h2>
      <p className="muted">{t("usage.explain")}</p>
      {d && (
        <form className="row" aria-label={t("usage.period")} onSubmit={apply} key={`${d.from}/${d.to}`}>
          <label>{t("usage.from")} <input id="usage-from" type="date" name="from" required defaultValue={d.from} /></label>
          <label>{t("usage.to")} <input id="usage-to" type="date" name="to" required defaultValue={d.to} aria-describedby="usage-to-hint" /></label>
          <button type="submit">{t("usage.show")}</button>
          <button type="button" className="primary" onClick={csv}>{t("usage.csv")}</button>
          <p id="usage-to-hint" className="muted">{t("usage.toHint")}</p>
        </form>
      )}
      {q.isPending && <p role="status">{t("common.loading")}</p>}
      {q.error && <p role="alert">{q.error.message}</p>}
      {error && <p role="alert">{error}</p>}
      {d && (
        <div className="scroll" role="region" aria-labelledby="usage-caption" tabIndex={0}>
          <table>
            <caption id="usage-caption">{t("usage.caption", { from: day(d.from), to: day(new Date(Date.parse(d.to) - DAY).toISOString()) })}</caption>
            <thead><tr>{d.columns.map((c) => <th key={c} scope="col">{t(`usage.col.${c}`)}</th>)}</tr></thead>
            <tbody>
              {body.length === 0 && <tr><td colSpan={d.columns.length} className="muted">{t("usage.empty")}</td></tr>}
              {body.map((r) => (
                <tr key={`${r[0]}/${r[1]}`}>
                  {r.map((v, i) => i < 2 ? <th key={i} scope="row">{figure(d.columns[i], v)}</th> : <td key={i} className="num">{figure(d.columns[i], v)}</td>)}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr><th scope="row" colSpan={2}>{t("usage.total")}</th>
                {total.slice(2).map((v, i) => <td key={i} className="num">{figure(d.columns[i + 2], v)}</td>)}</tr>
            </tfoot>
          </table>
        </div>
      )}
      {efforts && (
        <div className="scroll" role="region" aria-labelledby="usage-efforts" tabIndex={0}>
          <table className="efforts">
            <caption id="usage-efforts">{t("usage.effortsCaption")}</caption>
            <thead><tr>{efforts.columns.map((c) => <th key={c} scope="col">{t(`usage.col.${c}`)}</th>)}</tr></thead>
            <tbody>
              {efforts.rows.length === 0 && <tr><td colSpan={efforts.columns.length} className="muted">{t("usage.noLlm")}</td></tr>}
              {efforts.rows.map((r) => (
                <tr key={`${r[0]}/${r[1]}/${r[2]}`}>
                  {r.map((v, i) => i < 3 ? <th key={i} scope="row">{figure(efforts.columns[i], v)}</th> : <td key={i} className="num">{figure(efforts.columns[i], v)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
