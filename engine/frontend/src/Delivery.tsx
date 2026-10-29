import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { canEdit, confirmable, meQuery, post } from "./api";
import { Confirm } from "./mel";
import { ReadOnly } from "./Production";
import { focusSoon } from "./store";
import { t } from "./i18n";

type Smtp = { host: string; port: number; from: string; security: string; username?: string; password?: string };
type Channel = { type: string; url: string; secret?: string };
type DeliverySettings = { smtp: Smtp | null; channels: Record<string, Channel> };
type Row = { name: string; type: string; url: string; secret: string };

const secretName = (ref?: string) => /\{secret:(\w+)\}/.exec(ref ?? "")?.[1] ?? "";
const ref = (name: string) => `\${secret:${name}}`;

// FL-1/AT-1 delivery targets (DEMO-1-fix-1) on the Settings screen next to the locale (UI-DS.13): the mail server send_email uses and
// the channels post_message posts to (set_delivery, Owner). Secrets are named, never typed in (SC-4); a changed
// destination comes back as a 409 the Owner confirms (old → new).
export default function Delivery({ project }: { project: string }) {
  const qc = useQueryClient();
  const me = useQuery(meQuery(project)).data;
  const q = useQuery({ queryKey: ["delivery_settings", project], queryFn: () => post<DeliverySettings>("delivery_settings", { project }),
    enabled: canEdit(me?.role) });
  const [rows, setRows] = useState<Row[] | null>(null); // null: as saved
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const [confirm, setConfirm] = useState<{ body: unknown; message: string; token: string } | null>(null);
  if (!canEdit(me?.role)) return null; // Viewers: channel URLs and servers are settings, not theirs to read
  if (q.error) return <p role="alert">{q.error.message}</p>;
  const d = q.data;
  if (!d) return <p role="status">{t("common.loading")}</p>;
  const owner = me?.role === "owner" && !me.production;
  const list = rows ?? Object.entries(d.channels).map(([name, c]) => ({ name, type: c.type, url: c.url, secret: secretName(c.secret) }));
  const edit = (i: number, patch: Partial<Row>) => setRows(list.map((r, j) => (i === j ? { ...r, ...patch } : r)));

  const send = async (body: unknown, token?: string) => {
    setAlert("");
    setStatus("");
    const r = await confirmable("set_delivery", body, token);
    if (r.confirm) return setConfirm({ body, ...r.confirm }); // Confirm takes focus (autoFocus)
    setConfirm(null);
    if (!r.ok) {
      focusSoon("delivery-save");
      return setAlert(r.error ?? "");
    }
    setRows(null);
    await qc.invalidateQueries({ queryKey: ["delivery_settings", project] });
    qc.invalidateQueries({ queryKey: ["delivery_channels", project] });
    qc.invalidateQueries({ queryKey: ["changes", project] });
    setStatus(t("delivery.saved"));
    focusSoon("delivery-save"); // WCAG 2.4.3: the Confirm button is gone
  };
  const save = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const v = (k: string) => String(f.get(k) ?? "").trim();
    const smtp = v("host") ? { host: v("host"), port: Number(v("port")), from: v("from"), security: v("security"),
      ...(v("username") && { username: v("username") }), ...(v("password") && { password: ref(v("password")) }) } : null; // one without the other: the server's 422 says which
    const channels = Object.fromEntries(list.map((r) => [r.name.trim(), { type: r.type, url: r.url.trim(),
      ...(r.type === "webhook" && r.secret.trim() && { secret: ref(r.secret.trim()) }) }]));
    send({ project, smtp, channels: list.length ? channels : null });
  };

  const s = d.smtp;
  const why = owner ? undefined : "delivery-why";
  return (
    <section aria-labelledby="delivery-h" className="remote">
      <h2 id="delivery-h">{t("delivery.title")}</h2>
      <p className="muted" id="delivery-help">{t("delivery.help")}</p>
      {!owner && <ReadOnly id="delivery-why" project={project} why={t("delivery.ownersOnly")} />}
      <form onSubmit={save} key={JSON.stringify(d)}>
        <fieldset disabled={!owner}>
          <legend>{t("delivery.smtp")}</legend>
          <div className="row">
            <label>{t("delivery.host")} <input id="smtp-host" name="host" defaultValue={s?.host ?? ""} placeholder={t("delivery.hostExample")} autoComplete="off" /></label>
            <label>{t("delivery.port")} <input id="smtp-port" name="port" type="number" min={1} max={65535} defaultValue={s?.port ?? 587} /></label>
            <label>{t("delivery.security")} <select id="smtp-security" name="security" defaultValue={s?.security ?? "starttls"}>
              <option value="starttls">{t("delivery.starttls")}</option><option value="tls">{t("delivery.tls")}</option><option value="none">{t("delivery.plain")}</option>
            </select></label>
          </div>
          <div className="row">
            <label>{t("delivery.from")} <input id="smtp-from" name="from" type="email" defaultValue={s?.from ?? ""} placeholder={t("delivery.fromExample")} /></label>
            <label>{t("delivery.username")} <input id="smtp-user" name="username" defaultValue={s?.username ?? ""} autoComplete="off" /></label>
            <label>{t("delivery.secret")} <input id="smtp-secret" name="password" defaultValue={secretName(s?.password)}
              placeholder={t("delivery.secretExample")} pattern="\w+" title={t("delivery.secretFormat")} autoComplete="off" /></label>
          </div>
          <p className="muted">{t("delivery.smtpHelp")}</p>
        </fieldset>
        <fieldset disabled={!owner}>
          <legend>{t("delivery.channels")}</legend>
          {!list.length && <p className="muted">{t("delivery.noChannels")}</p>}
          {list.map((r, i) => (
            <div className="row" key={i} role="group" aria-label={t("delivery.channelN", { n: String(i + 1) })}>
              <label>{t("delivery.name")} <input id={`channel-${i}-name`} value={r.name} required onChange={(e) => edit(i, { name: e.target.value })} /></label>
              <label>{t("delivery.type")} <select id={`channel-${i}-type`} value={r.type} onChange={(e) => edit(i, { type: e.target.value })}>
                <option value="slack">{t("delivery.slack")}</option><option value="teams">{t("delivery.teams")}</option><option value="webhook">{t("delivery.webhook")}</option>
              </select></label>
              <label>{t("delivery.url")} <input id={`channel-${i}-url`} value={r.url} required size={40} placeholder={t("delivery.urlExample")}
                onChange={(e) => edit(i, { url: e.target.value })} /></label>
              {r.type === "webhook" && <label>{t("delivery.signing")} <input id={`channel-${i}-secret`} value={r.secret} pattern="\w+"
                title={t("delivery.secretFormat")} onChange={(e) => edit(i, { secret: e.target.value })} /></label>}
              <button type="button" onClick={() => { setRows(list.filter((_, j) => j !== i)); focusSoon("channel-add"); }}>{t("delivery.remove")}</button>
            </div>
          ))}
          <p className="muted">{t("delivery.urlHelp")}</p>
          <button type="button" id="channel-add" onClick={() => { setRows([...list, { name: "", type: "slack", url: "", secret: "" }]); focusSoon(`channel-${list.length}-name`); }}>
            {t("delivery.add")}</button>
        </fieldset>
        <button id="delivery-save" type="submit" className="primary" disabled={!owner} aria-describedby={why}>{t("common.save")}</button>
      </form>
      {confirm && (
        <Confirm label={t("delivery.confirm")} message={confirm.message} onConfirm={() => send(confirm.body, confirm.token)}
          onCancel={() => setConfirm(null)} back="delivery-save" />
      )}
      <p role="status" className="muted">{status}</p>
      {alert && <p role="alert" className="error">{alert}</p>}
    </section>
  );
}
