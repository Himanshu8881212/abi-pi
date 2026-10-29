import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { confirmable, sendersQuery, type SenderLink } from "./api";
import { t } from "./i18n";
import { focusSoon } from "./store";

// A 403 from list_senders: this user isn't a workspace owner (UI-3 read-only, with the reason).
export const notOwner = (e: unknown) => (e as { status?: number } | null)?.status === 403;

// SC-7 (X-02.7) workspace tab "*/#senders": who an inbound Slack/Teams/email message runs as. Workspace owners only.
export default function Senders() {
  const q = useQuery(sendersQuery);
  const qc = useQueryClient();
  const [kind, setKind] = useState<SenderLink["kind"]>("email");
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(null);
  const readOnly = notOwner(q.error);

  const act = async (action: "map_sender" | "unmap_sender", body: object, from: string) => {
    setNote(null);
    const r = await confirmable(action, body);
    if (!r.ok) return setNote({ error: true, text: r.error });
    await qc.invalidateQueries({ queryKey: ["senders"] });
    setNote({ error: false, text: action === "map_sender" ? t("senders.linked") : t("senders.unlinked") });
    focusSoon(from);
  };
  const link = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    act("map_sender", { kind, external_id: f.get("external_id"), user: f.get("user") }, "sender-id");
  };
  const unlink = (s: SenderLink, i: number) => {
    const rows = q.data ?? [];
    const next = rows[i + 1] ?? rows[i - 1]; // WCAG 2.4.3: the row goes away; its neighbour's Unlink, else the form
    act("unmap_sender", { kind: s.kind, external_id: s.external_id }, next ? `unlink-${next.kind}-${next.external_id}` : "sender-id");
  };

  return (
    <div className="tool senders">
      <h2 id="senders-title">{t("senders.title")}</h2>
      <p className="muted">{t("senders.explain")}</p>
      {readOnly && <p id="senders-why" className="muted">{t("senders.readOnly")}</p>}
      {q.error && !readOnly && <p role="alert">{q.error.message}</p>}
      {note && <p role={note.error ? "alert" : "status"} className={note.error ? "error" : undefined}>{note.text}</p>}
      <form aria-labelledby="senders-link-h" onSubmit={link}>
        <h3 id="senders-link-h">{t("senders.linkHeading")}</h3>
        <fieldset disabled={readOnly} aria-describedby={readOnly ? "senders-why" : undefined}>
          <legend className="sr-only">{t("senders.newLink")}</legend>
          <div className="row">
            <label>{t("senders.channel")}{" "}
              <select value={kind} onChange={(e) => setKind(e.target.value as SenderLink["kind"])}>
                {["email", "slack", "teams"].map((k) => <option key={k} value={k}>{t(`senders.kind.${k}`)}</option>)}
              </select>
            </label>
            <label>{t("senders.senderId")} <input id="sender-id" name="external_id" required maxLength={300} aria-describedby="sender-id-hint" /></label>
            <label>{t("senders.user")} <input name="user" required maxLength={100} autoComplete="off" /></label>
            <button type="submit" className="primary">{t("senders.link")}</button>
          </div>
          <p id="sender-id-hint" className="muted">{t(`senders.hint.${kind}`) /* what each channel's sender ID is; backend `inbound.KINDS` checks the format */}</p>
        </fieldset>
      </form>
      {q.data && (
        <table>
          <caption>{t("senders.linkedCaption")}</caption>
          <thead><tr><th scope="col">{t("senders.channel")}</th><th scope="col">{t("senders.senderId")}</th><th scope="col">{t("senders.user")}</th><th scope="col"><span className="sr-only">{t("senders.actions")}</span></th></tr></thead>
          <tbody>
            {q.data.length === 0 && <tr><td colSpan={4} className="muted">{t("senders.none")}</td></tr>}
            {q.data.map((s, i) => (
              <tr key={`${s.kind}/${s.external_id}`}>
                <td>{s.kind}</td><td>{s.external_id}</td><td>{s.user}</td>
                <td><button type="button" id={`unlink-${s.kind}-${s.external_id}`} aria-label={t("senders.unlinkLabel", { kind: s.kind, id: s.external_id })}
                  onClick={() => unlink(s, i)}>{t("senders.unlink")}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
