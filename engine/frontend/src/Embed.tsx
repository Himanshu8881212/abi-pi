import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { confirmable, copyText, post } from "./api";
import type { ReportSpec } from "./Report";
import { Confirm } from "./mel";
import { focusSoon } from "./store";
import { fmtDate, t } from "./i18n";

type Key = { kid: string; name: string; origins: string[]; creator: string; created: string; revoked: string | null; active: boolean };

// What the host site's backend signs with the key (E-08.1: HS256, header `kid`, exactly these claims). Never the key.
export function snippet(origin: string, project: string, report: ReportSpec, kid: string) {
  const c = (k: string) => `<${t(`embed.claim.${k}`)}>`;
  // the report name is Editor-authored: HTML-escape it so it can't inject markup into the host site
  const title = report.name.replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
  return `<iframe src="${origin}/embed/${kid}#embed=<token>" title="${title}"></iframe>\n\n${t("embed.signWith", { kid })}\n` +
    JSON.stringify({ project, tenant: c("tenant"), viewer: c("viewer"), report: report.id, page: c("page"), visual: c("visual"), iat: c("iat"), exp: c("exp") }, null, 2);
}

// RP-9 Embed dialog of the report bar (Owners): create a signing key for 1-10 origins after the confirmation (the key
// is shown once and kept in component state only, SC-4), copy a host snippet, list and revoke keys.
export function EmbedDialog({ project, report }: { project: string; report: ReportSpec }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [origins, setOrigins] = useState("");
  const [confirm, setConfirm] = useState<{ message: string; token: string } | null>(null);
  const [made, setMade] = useState<{ kid: string; key: string } | null>(null);
  const [status, setStatus] = useState("");
  const qc = useQueryClient();
  const qk = ["embed_keys", project];
  const keys = useQuery({ queryKey: qk, enabled: open, queryFn: async () => (await post<{ keys: Key[] }>("list_embed_keys", { project })).keys });
  const create = async (token?: string) => {
    const list = origins.split(/\s+/).filter(Boolean);
    const r = await confirmable("create_embed_key", { project, name, origins: list }, token);
    if (r.confirm) return setConfirm(r.confirm); // Confirm takes focus (autoFocus)
    setConfirm(null);
    if (!r.ok) {
      setStatus(t("embed.createFailed", { error: r.error }));
      return focusSoon("embed-create");
    }
    setStatus("");
    setMade({ kid: r.json.kid, key: r.json.key }); // its input takes focus (autoFocus)
    qc.invalidateQueries({ queryKey: qk });
  };
  const copy = async (text: string) => setStatus((await copyText(text)) ? t("embed.copied") : t("embed.copyFailed"));
  const revoke = async (k: Key) => {
    try {
      await post("revoke_embed_key", { project, kid: k.kid });
      setStatus(t("embed.revoked", { name: k.name }));
      await qc.invalidateQueries({ queryKey: qk });
    } catch (e) {
      setStatus(t("embed.revokeFailed", { error: (e as Error).message }));
    }
    focusSoon("embed-keys-h"); // WCAG 2.4.3: the Revoke button unmounts
  };
  const show = () => {
    setOpen(true);
    setMade(null);
    setConfirm(null);
    setStatus("");
    dlg.current!.showModal();
  };
  const hostSnippet = made && snippet(location.origin, project, report, made.kid);
  return (
    <>
      <button id="embed-open" className="sm" onClick={show}>{t("embed.open")}</button>
      <dialog ref={dlg} className="share-dialog" aria-labelledby="embed-h" onClose={() => (setOpen(false), setMade(null), focusSoon("embed-open"))}>
        <h2 id="embed-h">{t("embed.heading", { name: report.name })}</h2>
        <p className="muted">{t("embed.explain")}</p>
        {open && (
          <>
            <div className="row">
              <label>{t("embed.name")} <input value={name} maxLength={100} onChange={(e) => setName(e.target.value)} /></label>
              <label>{t("embed.origins")} <textarea rows={2} cols={40} value={origins} aria-describedby="embed-origins-help" onChange={(e) => setOrigins(e.target.value)} /></label>
              <button id="embed-create" className="primary" onClick={() => create()}>{t("embed.create")}</button>
            </div>
            <p id="embed-origins-help" className="muted">{t("embed.originsHelp")}</p>
            {confirm && (
              <Confirm label={t("embed.confirmLabel")} message={confirm.message} onConfirm={() => create(confirm.token)}
                onCancel={() => setConfirm(null)} back="embed-create" />
            )}
            {made && (
              <>
                <div className="row">
                  <label>{t("embed.keyOnce")} <input key={made.key} autoFocus readOnly size={50} value={made.key} onFocus={(e) => e.target.select()} /></label>
                  <button onClick={() => copy(made.key)}>{t("embed.copyKey")}</button>
                </div>
                <div className="row">
                  <label>{t("embed.snippet")} <textarea readOnly rows={12} cols={70} value={hostSnippet!} /></label>
                  <button onClick={() => copy(hostSnippet!)}>{t("embed.copySnippet")}</button>
                </div>
              </>
            )}
            <p role="status" className="muted">{status}</p>
            <h3 id="embed-keys-h" tabIndex={-1}>{t("embed.keys")}</h3>
            {keys.error && <p role="alert" className="error">{keys.error.message}</p>}
            {keys.data?.length === 0 && <p className="muted">{t("embed.noKeys")}</p>}
            {!!keys.data?.length && (
              <table>
                <thead><tr><th scope="col">{t("embed.keyName")}</th><th scope="col">{t("embed.origins")}</th><th scope="col">{t("embed.createdBy")}</th><th scope="col">{t("embed.created")}</th><th scope="col">{t("embed.status")}</th></tr></thead>
                <tbody>
                  {keys.data.map((k) => (
                    <tr key={k.kid}>
                      <th scope="row">{k.name}</th>
                      <td>{k.origins.join(" ")}</td>
                      <td>{k.creator}</td>
                      <td>{fmtDate(k.created)}</td>
                      <td>{k.active ? <button className="sm" aria-label={t("embed.revokeLabel", { name: k.name, kid: k.kid })} onClick={() => revoke(k)}>{t("embed.revoke")}</button> : t("embed.isRevoked")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
        <div className="row"><button onClick={() => dlg.current!.close()}>{t("common.close")}</button></div>
      </dialog>
    </>
  );
}
