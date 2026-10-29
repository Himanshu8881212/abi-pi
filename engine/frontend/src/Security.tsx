import { useQuery } from "@tanstack/react-query";
import { rlsQuery } from "./api";
import { t } from "./i18n";
import Rls from "./Rls";
import { useUi } from "./store";

const roleText = (r: string | null) => (r === "owner" ? t("security.owner") : r === "editor" ? t("security.editor") : t("security.viewer"));

// UI-DS.12 Govern › Security (D-119): the project's SC-2 roles (Owners: `rls_rules`' accounts, read-only; roles change by
// chat Confirm or security/roles.json), its SC-3/D-110 row and column rules, and links to the workspace's sender links
// (SC-7) and tenants (SE-13), screens that gate themselves to workspace owners (App.tsx's Security list has them too)
export default function Security({ project }: { project: string }) {
  const accounts = useQuery(rlsQuery(project)).data?.accounts?.filter((a) => a.role);
  const open = useUi((s) => s.open);
  return (
    <div className="security">
      {accounts && (
        <section aria-labelledby="roles-h">
          <h2 id="roles-h">{t("security.roles")}</h2>
          <div className="table-wrap" tabIndex={0} role="region" aria-labelledby="roles-h">
            <table>
              <thead><tr><th scope="col">{t("security.member")}</th><th scope="col">{t("security.role")}</th></tr></thead>
              <tbody>{accounts.map((a) => <tr key={a.id}><th scope="row">{a.name}</th><td>{roleText(a.role)}</td></tr>)}</tbody>
            </table>
          </div>
        </section>
      )}
      <Rls project={project} />
      <section aria-labelledby="security-ws-h">
        <h2 id="security-ws-h">{t("security.workspace")}</h2>
        <ul aria-labelledby="security-ws-h">
          <li><button className="link" onClick={() => open("*/#senders")}>{t("nav.senders")}</button></li>
          <li><button className="link" onClick={() => open("*/#tenants")}>{t("tenants.title")}</button></li>
        </ul>
      </section>
    </div>
  );
}
