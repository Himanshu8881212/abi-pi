import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { post, tenantsQuery, type Tenant } from "./api";
import { changed, LocaleFields } from "./Locale";
import { t } from "./i18n";

// SE-13 workspace tab "*/#tenants": each tenant's time zone and display currency, its projects' defaults unless a
// project sets its own (set_tenant, workspace owners only; list_tenants refuses everyone else).
export default function Tenants() {
  const q = useQuery(tenantsQuery);
  return (
    <div className="tool">
      <h2 id="tenants-title">{t("tenants.title")}</h2>
      <p className="muted">{t("tenants.explain")}</p>
      {q.isPending && <p role="status">{t("common.loading")}</p>}
      {q.error && <p role="alert">{q.error.message}</p>}
      {q.data?.length === 0 && <p className="muted">{t("tenants.none")}</p>}
      {q.data?.map((x) => <TenantForm key={x.id} tenant={x} />)}
    </div>
  );
}

function TenantForm({ tenant }: { tenant: Tenant }) {
  const qc = useQueryClient();
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const h = `tenant-${tenant.id}-h`;
  const save = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setStatus("");
    setAlert("");
    try {
      await post("set_tenant", { id: tenant.id, time_zone: f.get("time_zone"), currency: f.get("currency") });
      await changed(qc, ["tenants"]);
      setStatus(t("locale.saved"));
    } catch (err) {
      setAlert((err as Error).message);
    }
  };
  return (
    <section aria-labelledby={h}>
      <h3 id={h}>{tenant.name} <span className="muted">({tenant.id})</span></h3>
      <form className="row" onSubmit={save}>
        <LocaleFields id={`tenant-${tenant.id}`} zone={tenant.time_zone} currency={tenant.currency} help={t("tenants.help")} />
        <button type="submit">{t("common.save")}</button>
      </form>
      <p role="status" className="muted">{status}</p>
      {alert && <p role="alert" className="error">{alert}</p>}
    </section>
  );
}
