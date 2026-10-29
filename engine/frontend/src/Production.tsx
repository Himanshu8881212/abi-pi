import { useQuery } from "@tanstack/react-query";
import { meQuery } from "./api";
import { t } from "./i18n";

// D-56: the workspace header says when the active tab's project is a view-only production environment.
export function ProductionBadge({ project }: { project: string }) {
  const prod = useQuery(meQuery(project)).data?.production;
  return prod ? <span className="prod-badge badge warn" id="prod-badge" title={t("production.why")}>{t("production.badge")}</span> : null;
}

// Why an editor is read-only: production (D-56) for everyone, else the tab's own role reason.
export function ReadOnly({ project, why, id }: { project: string; why: string; id?: string }) {
  const prod = useQuery(meQuery(project)).data?.production;
  return <p id={id} className="muted">{prod ? t("production.why") : why}</p>;
}
