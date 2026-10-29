import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { confirmable, copyText, post } from "./api";
import type { VisualData } from "./charts";
import { Grid, ordered } from "./Print";
import { BrandBar, useBrand, type Branding } from "./Brand";
import type { ReportSpec } from "./Report";
import { Confirm } from "./mel";
import { focusSoon } from "./store";
import { fmtDate, t } from "./i18n";

// RP-6 share view (X-05.6, D-35): `/#share=<token>`; RP-9 embed view (E-08.2): `/embed/<kid>#embed=<token>`. The
// fragment never reaches a server or a Referer; the token goes only in the X-ABI-Share/X-ABI-Embed header of
// /api/share|embed calls, which return the report layout and visual_data, nothing else. No session: plain fetch, no
// Bearer, no cookies (E-08.md decision 7). Read-only: no selections, edits, "How is this calculated?" or exports.
export type Kind = "share" | "embed";
export async function shared<T>(kind: Kind, path: string, token: string, body: unknown = {}): Promise<T> {
  const header = kind === "share" ? "X-ABI-Share" : "X-ABI-Embed";
  const r = await fetch(`/api/${kind}/${path}`, { method: "POST", credentials: "omit", body: JSON.stringify(body), headers: { "Content-Type": "application/json", [header]: token } });
  const j = await r.json().catch(() => null);
  if (!r.ok) throw new Error(typeof j?.detail === "string" ? j.detail : (j?.detail?.message ?? r.statusText));
  return j as T;
}

export default function Share({ token, kind = "share" }: { token: string; kind?: Kind }) {
  const r = useQuery({ queryKey: [kind], queryFn: () => shared<ReportSpec & { branding: Branding | null }>(kind, "report", token), retry: false });
  useBrand(r.data?.branding); // SC-9: the project's tenant (logo inline as a data: URL; this view has no session)
  const [page, setPage] = useState<string | null>(null);
  const pages = r.data?.pages ?? [];
  const p = pages.find((x) => x.id === page) ?? pages[0];
  const visuals = ordered(p?.visuals ?? []);
  const data = useQueries({
    queries: visuals.map((v) => ({ queryKey: [kind, p!.id, v.id], retry: false,
      queryFn: () => shared<VisualData>(kind, "visual_data", token, { page: p!.id, visual: v.id }) })),
  });
  const error = r.error?.message ?? data.find((q) => q.error)?.error?.message;
  return (
    <>
    {r.data?.branding && <BrandBar b={r.data.branding} />}
    <main className="print share">
      <h1>{r.data?.name ?? t(kind === "share" ? "share.title" : "embed.title")}{p && pages.length > 1 && ` · ${p.name}`}</h1>
      <p className="muted">{t(kind === "share" ? "share.readOnly" : "embed.readOnly")}</p>
      {r.isPending && <p role="status">{t("common.loading")}</p>}
      {error && <p role="alert">{error}</p>}
      {pages.length > 1 && (
        <nav aria-label={t("share.pages")} className="row">
          {pages.map((x) => <button key={x.id} className="link" aria-current={x.id === p?.id ? "page" : undefined} onClick={() => setPage(x.id)}>{x.name}</button>)}
        </nav>
      )}
      {p && <Grid visuals={visuals} data={data.map((q) => q.data)} />}
    </main>
    </>
  );
}

type Link = { id: number; report: string; page: string | null; creator: string; created: string; expires: string; revoked: string | null; active: boolean };

// RP-6 Share dialog of the report bar (Editors): create a link (whole report or one page, 1-30 days) after the
// confirmation, copy it (shown once, kept in component state only), list and revoke links.
export function ShareDialog({ project, report }: { project: string; report: ReportSpec }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState("");
  const [days, setDays] = useState(7);
  const [confirm, setConfirm] = useState<{ message: string; token: string } | null>(null);
  const [made, setMade] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const qc = useQueryClient();
  const key = ["share_links", project];
  const links = useQuery({ queryKey: key, enabled: open, queryFn: async () => (await post<{ links: Link[] }>("list_share_links", { project })).links });
  const name = (l: Link) => (l.page ? `${l.report} · ${l.page}` : l.report);
  const create = async (token?: string) => {
    const r = await confirmable("create_share_link", { project, report: report.id, days, ...(page ? { page } : {}) }, token);
    if (r.confirm) return setConfirm(r.confirm); // Confirm takes focus (autoFocus)
    setConfirm(null);
    if (!r.ok) {
      setStatus(t("share.createFailed", { error: r.error }));
      return focusSoon("share-create");
    }
    setStatus("");
    setMade(location.origin + r.json.url); // its input takes focus (autoFocus)
    qc.invalidateQueries({ queryKey: key });
  };
  const copy = async () => setStatus((await copyText(made!)) ? t("share.copied") : t("share.copyFailed"));
  const revoke = async (l: Link) => {
    try {
      await post("revoke_share_link", { project, id: l.id });
      setStatus(t("share.revoked", { name: name(l) }));
      await qc.invalidateQueries({ queryKey: key });
    } catch (e) {
      setStatus(t("share.revokeFailed", { error: (e as Error).message }));
    }
    focusSoon("share-links-h"); // WCAG 2.4.3: the Revoke button unmounts
  };
  const show = () => {
    setOpen(true);
    setMade(null);
    setConfirm(null);
    setStatus("");
    dlg.current!.showModal();
  };
  return (
    <>
      <button id="share-open" className="sm" onClick={show}>{t("share.open")}</button>
      <dialog ref={dlg} className="share-dialog" aria-labelledby="share-h" onClose={() => (setOpen(false), setMade(null), focusSoon("share-open"))}>
        <h2 id="share-h">{t("share.heading", { name: report.name })}</h2>
        <p className="muted">{t("share.explain")}</p>
        {open && (
          <>
            <div className="row">
              <label>{t("share.show")} <select value={page} onChange={(e) => setPage(e.target.value)}>
                <option value="">{t("share.wholeReport")}</option>
                {report.pages.map((p) => <option key={p.id} value={p.id}>{t("share.page", { name: p.name })}</option>)}
              </select></label>
              <label>{t("share.expiresAfter")} <input type="number" min={1} max={30} value={days} onChange={(e) => setDays(Number(e.target.value))} /> {t("share.days")}</label>
              <button id="share-create" className="primary" onClick={() => create()}>{t("share.create")}</button>
            </div>
            {confirm && (
              <Confirm label={t("share.confirmLabel")} message={confirm.message} onConfirm={() => create(confirm.token)}
                onCancel={() => setConfirm(null)} back="share-create" />
            )}
            {made && (
              <div className="row">
                <label>{t("share.linkOnce")} <input key={made} autoFocus readOnly size={50} value={made} onFocus={(e) => e.target.select()} /></label>
                <button onClick={copy}>{t("share.copy")}</button>
              </div>
            )}
            <p role="status" className="muted">{status}</p>
            <h3 id="share-links-h" tabIndex={-1}>{t("share.links")}</h3>
            {links.error && <p role="alert" className="error">{links.error.message}</p>}
            {links.data?.length === 0 && <p className="muted">{t("share.noLinks")}</p>}
            {!!links.data?.length && (
              <table>
                <thead><tr><th scope="col">{t("share.shows")}</th><th scope="col">{t("share.createdBy")}</th><th scope="col">{t("share.expires")}</th><th scope="col">{t("share.status")}</th></tr></thead>
                <tbody>
                  {links.data.map((l) => (
                    <tr key={l.id}>
                      <th scope="row">{name(l)}</th>
                      <td>{l.creator}</td>
                      <td>{fmtDate(l.expires)}</td>
                      <td>{l.active ? <button className="sm" aria-label={t("share.revokeLabel", { id: String(l.id), name: name(l), creator: l.creator })} onClick={() => revoke(l)}>{t("share.revoke")}</button>
                        : l.revoked ? t("share.isRevoked") : t("share.isExpired")}</td>
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
