import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { providersQuery, startOidc, tenantsQuery } from "./api";
import { Mfa } from "./Account";
import { Language, MAP } from "./App";
import ChatGpt from "./ChatGpt";
import Delivery from "./Delivery";
import { t } from "./i18n";
import ProjectLocale from "./Locale";
import { focusSoon, useUi } from "./store";

// UI-DS.13 Settings and account ("*/#settings", D-119 foot): the person's account (sign-in links SC-1, MFA SC-10, ChatGPT
// AG-6/D-53), language (UI-5), the workspace screens for workspace owners (LLM providers AG-6, tenants SE-13), and the
// switcher's project's time zone and currency (SE-13) and delivery targets (FL-1), moved here from Changes. Each part
// gates itself by role as before; appearance and density stay in the account menu (per browser, D-77).
export default function Settings() {
  const { user, project, open } = useUi();
  const providers = useQuery(providersQuery).data ?? [];
  const owner = useQuery(tenantsQuery).isSuccess; // list_tenants answers workspace owners only
  const [error, setError] = useState<string | null>(null);
  const go = (tab: string) => (open(tab), focusSoon(`ws-${tab}`));
  return (
    <div className="settings">
      <section aria-labelledby="settings-account-h">
        <h2 id="settings-account-h">{t("settings.account")}</h2>
        {user && <p className="muted">{t("shell.signedInAs", { name: user })}</p>}
        {error && <p role="alert">{error}</p>}
        {/* SC-1: link this account to a sign-in provider; the result comes back as ?signin=linked|conflict|… */}
        {providers.map((p) => (
          <button key={p.id} onClick={() => startOidc(p.id).catch((e) => setError((e as Error).message))}>{t("shell.link", { name: p.name })}</button>
        ))}
        <Mfa />
        <ChatGpt />
        <Language />
        {/* UI-DS.14: every capability and its by-hand path */}
        <p><button id="settings-map" className="link" onClick={() => go(MAP)}>{t("tab.map")}</button></p>
      </section>
      {owner && (
        <section aria-labelledby="settings-ws-h">
          <h2 id="settings-ws-h">{t("settings.workspace")}</h2>
          <ul>
            <li><button id="providers-open" className="link" onClick={() => go("*/#providers")}>{t("providers.title")}</button></li>
            <li><button id="tenants-open" className="link" onClick={() => go("*/#tenants")}>{t("tenants.title")}</button></li>
          </ul>
        </section>
      )}
      <section aria-labelledby="settings-project-h">
        <h2 id="settings-project-h">{project ? t("settings.project", { project }) : t("settings.noProject")}</h2>
        {project ? <><ProjectLocale key={project} project={project} /><Delivery key={`d-${project}`} project={project} /></>
          : <p className="muted">{t("settings.pick")}</p>}
      </section>
    </div>
  );
}
