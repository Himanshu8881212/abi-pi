import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, chatgptQuery, confirmable, post } from "./api";
import { ModelEffort } from "./Effort";
import { t } from "./i18n";
import { Confirm } from "./mel";
import { focusSoon, useUi } from "./store";

// AG-6 / D-53 "Sign in with ChatGPT" on the Settings screen (UI-DS.13). The browser never sees a token (SC-4): only the link state.

// The callback's fixed outcome codes (E-12.3.md decision 2); anything else reads as `failed`.
const OUTCOMES = ["linked", "expired", "browser", "denied", "failed", "scope"];

// The return from OpenAI (`/#chatgpt=<outcome>`, main.tsx before the first render): clear the hash, show the notice.
export function chatgptReturn() {
  const code = location.hash.match(/^#chatgpt=(.*)$/)?.[1];
  if (code === undefined) return;
  history.replaceState(null, "", location.pathname + location.search);
  useUi.setState({ notice: t(`chatgpt.notice.${OUTCOMES.includes(code) ? code : "failed"}`) });
}

export default function ChatGpt() {
  const qc = useQueryClient();
  const { data: a, error } = useQuery(chatgptQuery);
  const [confirm, setConfirm] = useState<{ token: string; email: string } | null>(null);
  const [msg, setMsg] = useState<string | null>(null); // the Disconnect outcome; takes focus
  const [err, setErr] = useState<string | null>(null); // start/model errors; focus stays on the control
  const msgRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => void (msg && msgRef.current?.focus()), [msg]); // WCAG 2.4.3: Disconnect/Confirm unmount
  const refresh = () => qc.invalidateQueries({ queryKey: chatgptQuery.queryKey });
  const start = () => api<{ url: string }>("/api/chatgpt/start", { method: "POST" })
    .then((r) => location.assign(r.url), (e) => setErr((e as Error).message));
  // backup omitted: unchanged (D-80); effort null: the model's default (D-71)
  const pick = (model: string, effort: string | null) => post("set_chatgpt_model", { model: model || null, effort })
    .then(() => (setErr(null), refresh()), (e) => setErr((e as Error).message));
  const disconnect = async (token?: string) => {
    const r = await confirmable("unlink_chatgpt", {}, token);
    if (r.confirm) return setConfirm({ token: r.confirm.token, email: r.confirm.preview?.email ?? "" }); // Confirm takes focus
    setConfirm(null);
    setMsg(!r.ok ? r.error : r.json?.revoked === false ? t("chatgpt.notRevoked") : t("chatgpt.disconnected"));
    await refresh();
  };
  const continueButton = <button id="chatgpt-continue" onClick={start}>{t("chatgpt.continue")}</button>;
  return (
    <div role="group" aria-labelledby="chatgpt-h" className="chatgpt">
      <span id="chatgpt-h" className="muted">{t("chatgpt.title")}</span>
      {(error || err) && <p role="alert">{err ?? (error as Error).message}</p>}
      {a && !a.linked && (a.available ? continueButton : <p>{t("chatgpt.off")}</p>)}
      {a?.linked && (
        <>
          <p id="chatgpt-email">{a.email ? t("chatgpt.linkedAs", { email: a.email }) : t("chatgpt.linked")}</p>
          {a.relink && <p>{t("chatgpt.relink")}</p>}
          {a.relink && (a.available ? continueButton : <p>{t("chatgpt.off")}</p>)}
          {a.error && <p role="alert">{a.error}</p>}
          {!a.relink && (<>
            <ModelEffort id="chatgpt-model" label={t("chatgpt.model")} describedBy="chatgpt-plan" model={a.model ?? ""} effort={a.effort}
              choices={a.models.map((m) => ({ id: m.slug, name: m.name, efforts: m.efforts, default: m.default_effort }))}
              lead={[{ value: "|", text: t("chatgpt.workspace") }]} onPick={pick} />
            <span id="chatgpt-plan" className="muted">{t("chatgpt.plan")}</span>
          </>)}
          {confirm ? (
            <Confirm label={t("chatgpt.disconnect")} message={t("chatgpt.confirm", { email: confirm.email })} onConfirm={() => disconnect(confirm.token)}
              onCancel={() => (setConfirm(null), setMsg(null))} back="chatgpt-disconnect" />
          ) : (
            <button id="chatgpt-disconnect" onClick={() => disconnect()}>{t("chatgpt.disconnect")}</button>
          )}
        </>
      )}
      {msg && <p id="chatgpt-msg" role="status" tabIndex={-1} ref={msgRef}>{msg}</p>}
    </div>
  );
}
