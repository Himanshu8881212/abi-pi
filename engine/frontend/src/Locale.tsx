import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { canDesign, localeQuery, meQuery, post, tablesQuery } from "./api";
import { ReadOnly } from "./Production";
import { t } from "./i18n";

// The browser's IANA zones (Chrome leaves out "UTC") as suggestions; the server's zoneinfo check decides (422).
export const ZONES = [...new Set(["UTC", ...Intl.supportedValuesOf("timeZone")])];

// SE-13 caption: the time zone and display currency numbers are in, as the action returned them (currency omitted unset)
export const locale = (d: { time_zone?: string; currency?: string | null }) =>
  d.currency ? t("report.localeCurrency", { zone: d.time_zone!, currency: d.currency }) : t("report.locale", { zone: d.time_zone! });

// SE-13 time zone and display currency inputs, shared by the project and tenant forms; empty removes the value.
export function LocaleFields({ id, zone, currency, help }: { id: string; zone: string | null; currency: string | null; help: string }) {
  return (
    <>
      <label>{t("locale.zone")} <input id={`${id}-zone`} name="time_zone" list={`${id}-zones`} defaultValue={zone ?? ""}
        autoComplete="off" aria-describedby={`${id}-help`} /></label>
      <datalist id={`${id}-zones`}>{ZONES.map((z) => <option key={z} value={z} />)}</datalist>
      <label>{t("locale.currency")} <input id={`${id}-currency`} name="currency" defaultValue={currency ?? ""} size={4} maxLength={3}
        pattern="[A-Z]{3}" title={t("locale.currencyFormat")} aria-describedby={`${id}-help`} /></label>
      <p id={`${id}-help`} className="muted">{help}</p>
    </>
  );
}

// Every query result carries the zone and currency (SE-13): visuals and their explanations read again after a change.
export const changed = (qc: ReturnType<typeof useQueryClient>, key: unknown[]) =>
  Promise.all([key, ["visual"], ["how"]].map((k) => qc.invalidateQueries({ queryKey: k })));

// SE-13 project settings on the Settings screen (UI-DS.13): the project's own time zone and display currency over
// its tenant's (set_project_locale) and the model's rates table (set_rates_table). Editors only (SC-2).
export default function ProjectLocale({ project }: { project: string }) {
  const qc = useQueryClient();
  const editor = canDesign(useQuery(meQuery(project)).data);
  const q = useQuery(localeQuery(project));
  const tables = useQuery({ ...tablesQuery, select: (rows: Parameters<typeof tablesQuery.select>[0]) =>
    rows.filter((r) => r.project === project).map((r) => r.table) });
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const d = q.data;

  const save = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setStatus("");
    setAlert("");
    try {
      await post("set_project_locale", { project, time_zone: f.get("time_zone"), currency: f.get("currency") });
      const rates = String(f.get("rates_table"));
      if (rates !== (d!.rates_table ?? "")) await post("set_rates_table", { project, table: rates });
      await changed(qc, ["project_locale", project]);
      setStatus(t("locale.saved"));
    } catch (err) {
      setAlert((err as Error).message);
    }
  };

  if (q.error) return <p role="alert">{q.error.message}</p>;
  if (!d) return <p role="status">{t("common.loading")}</p>;
  const now = d.effective ? locale(d.effective) : t("locale.invalid");
  return (
    <section aria-labelledby="locale-h" className="remote">
      <h2 id="locale-h">{t("locale.title")}</h2>
      <p>{t("locale.now", { now })}</p>
      {!editor && <ReadOnly id="locale-why" project={project} why={t("locale.editorsOnly")} />}
      <form className="row" onSubmit={save} key={JSON.stringify(d)}>
        <LocaleFields id="project-locale" zone={d.time_zone} currency={d.currency} help={t("locale.projectHelp")} />
        <label>{t("locale.rates")} <select id="project-rates" name="rates_table" defaultValue={d.rates_table ?? ""}>
          <option value="">{t("locale.noRates")}</option>
          {[...new Set([...(tables.data ?? []), ...(d.rates_table ? [d.rates_table] : [])])].map((x) => <option key={x}>{x}</option>)}
        </select></label>
        <button type="submit" disabled={!editor} aria-describedby={editor ? undefined : "locale-why"}>{t("common.save")}</button>
      </form>
      <p role="status" className="muted">{status}</p>
      {alert && <p role="alert" className="error">{alert}</p>}
    </section>
  );
}
