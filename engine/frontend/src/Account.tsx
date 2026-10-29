import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, post } from "./api";
import { t } from "./i18n";
import { focusSoon, useUi } from "./store";

// SC-10 account screens (E-06.5). Link tokens, the TOTP key and recovery codes live in component state only (SC-4).
export type Link = { kind: "reset" | "invite" | "signup"; token: string };
const CONFIRM = { reset: "/api/password-reset/confirm", invite: "/api/invite/confirm", signup: "/api/signup/confirm" };
const field = (e: FormEvent<HTMLFormElement>, name: string) => String(new FormData(e.currentTarget).get(name));

// The emailed link's page (`/#reset=`, `/#invite=`, `/#signup=`; main.tsx takes the token out of the URL first).
export function SetPassword({ kind, token, onDone }: Link & { onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    try {
      await api(CONFIRM[kind], { method: "POST", body: JSON.stringify({ token, password: field(e, "password") }) });
      useUi.setState({ notice: t("account.passwordSet") }); // the sign-in screen shows it, its name field focused
      onDone();
    } catch (err) {
      setError((err as Error).message);
      focusSoon("new-password"); // WCAG 3.3.1: the error is announced, focus stays on the field
    }
  }
  return (
    <main className="signin">
      <h1>{t("app.name")}</h1>
      <h2>{t(`account.${kind}.title`)}</h2>
      <form onSubmit={submit}>
        <label>{t("account.newPassword")} <input id="new-password" name="password" type="password" autoComplete="new-password" minLength={8} required autoFocus aria-describedby="new-password-hint" /></label>
        <span id="new-password-hint" className="muted">{t("account.passwordHint")}</span>
        {error && <p role="alert">{error}</p>}
        <button type="submit" className="primary">{t("account.setPassword")}</button>
      </form>
    </main>
  );
}

// "Forgot password" and "Create account" on the sign-in screen: the server's answer is the same for every address.
export function Request({ kind, onBack }: { kind: "reset" | "signup"; onBack: () => void }) {
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const msg = useRef<HTMLParagraphElement>(null);
  useEffect(() => void (sent && msg.current?.focus()), [sent]); // WCAG 2.4.3 / 4.1.3
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setSent(false);
    try {
      await api(kind === "reset" ? "/api/password-reset" : "/api/signup", { method: "POST", body: JSON.stringify({ email: field(e, "email") }) });
      setError(null);
      setSent(true);
    } catch (err) {
      setError((err as Error).message);
      focusSoon("request-email");
    }
  }
  return (
    <form onSubmit={submit} aria-labelledby="request-h">
      <h2 id="request-h">{t(`account.${kind}.ask`)}</h2>
      <label>{t("account.email")} <input id="request-email" name="email" type="email" autoComplete="email" required autoFocus /></label>
      {error && <p role="alert">{error}</p>}
      {sent && <p id="request-sent" role="status" tabIndex={-1} ref={msg}>{t(`account.${kind}.sent`)}</p>}
      <button type="submit" className="primary">{t("account.send")}</button>
      <button type="button" className="link" onClick={onBack}>{t("account.back")}</button>
    </form>
  );
}

// The key for an authenticator app: typed in, or opened as the otpauth link (no QR code, E-06.md).
export const Key = ({ secret, uri }: { secret: string; uri: string }) => (
  <>
    <p>{t("mfa.key")} <code id="mfa-key">{secret}</code></p>
    <p><a href={uri}>{t("mfa.link")}</a></p>
  </>
);

// Recovery codes, shown once (after enrolment at sign-in or in Settings); the heading takes focus.
export function Codes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const h = useRef<HTMLHeadingElement>(null);
  useEffect(() => h.current?.focus(), []);
  return (
    <section aria-labelledby="codes-h" className="codes">
      <h2 id="codes-h" tabIndex={-1} ref={h}>{t("mfa.codes.title")}</h2>
      <p>{t("mfa.codes.text")}</p>
      <ol id="recovery-codes">{codes.map((c) => <li key={c}><code>{c}</code></li>)}</ol>
      <button id="codes-done" className="primary" onClick={onDone}>{t("mfa.codes.done")}</button>
    </section>
  );
}

const mfaQuery = { queryKey: ["mfa"], queryFn: () => post<{ on: boolean; required: boolean }>("mfa_status", {}) };

// Turning two-step sign-in on asks for the password first (OWASP MFA: re-authenticate before changing factors).
export function MfaPassword({ onSubmit, onCancel }: { onSubmit: (e: FormEvent<HTMLFormElement>) => void; onCancel: () => void }) {
  return (
    <form onSubmit={onSubmit}>
      <label>{t("signin.password")} <input id="mfa-start-password" name="password" type="password" autoComplete="current-password" required autoFocus aria-describedby="mfa-start-hint" /></label>
      <span id="mfa-start-hint" className="muted">{t("mfa.password")}</span>
      <button type="submit" className="primary">{t("mfa.showKey")}</button>
      <button type="button" onClick={onCancel}>{t("common.cancel")}</button>
    </form>
  );
}

// Two-step sign-in on the Settings screen (UI-DS.13): turn on (key + a code → recovery codes once), turn off (password + a code).
export function Mfa() {
  const qc = useQueryClient();
  const s = useQuery(mfaQuery).data;
  const [key, setKey] = useState<{ secret: string; uri: string } | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [off, setOff] = useState(false);
  const [ask, setAsk] = useState(false); // the password before a new key (H-01.1-fix-4)
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const msgRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => void (msg && msgRef.current?.focus()), [msg]); // the turn-off form unmounts
  const fail = (id: string) => (e: Error) => (setError(e.message), focusSoon(id)); // focus stays on the field
  const done = (next: () => void) => (setError(null), next(), qc.invalidateQueries({ queryKey: mfaQuery.queryKey }));
  const back = (id: string) => (setKey(null), setOff(false), setAsk(false), setError(null), focusSoon(id));
  const start = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    post<{ secret: string; uri: string }>("start_mfa", { password: field(e, "password") })
      .then((k) => (setMsg(null), setError(null), setAsk(false), setKey(k)), fail("mfa-start-password"));
  };
  const confirm = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    post<{ recovery_codes: string[] }>("confirm_mfa", { code: field(e, "code") })
      .then((r) => done(() => (setKey(null), setCodes(r.recovery_codes))), fail("mfa-code"));
  };
  const disable = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    post("disable_mfa", { password: field(e, "password"), code: field(e, "code") })
      .then(() => done(() => (setOff(false), setMsg(t("mfa.off")))), fail("mfa-off-code"));
  };
  return (
    <div role="group" aria-labelledby="mfa-h" className="mfa">
      <span id="mfa-h" className="muted">{t("mfa.title")}</span>
      {error && <p role="alert">{error}</p>}
      {codes ? <Codes codes={codes} onDone={() => (setCodes(null), focusSoon("mfa-state"))} />
        : key ? (
          <form onSubmit={confirm}>
            <p>{t("mfa.add")}</p>
            <Key {...key} />
            <label>{t("mfa.code")} <input id="mfa-code" name="code" autoComplete="one-time-code" required autoFocus /></label>
            <button type="submit" className="primary">{t("mfa.turnOn")}</button>
            <button type="button" onClick={() => back("mfa-start")}>{t("common.cancel")}</button>
          </form>
        ) : ask ? (
          <MfaPassword onSubmit={start} onCancel={() => back("mfa-start")} />
        ) : off ? (
          <form onSubmit={disable}>
            <label>{t("signin.password")} <input name="password" type="password" autoComplete="current-password" required autoFocus /></label>
            <label>{t("mfa.code")} <input id="mfa-off-code" name="code" autoComplete="one-time-code" required aria-describedby="mfa-off-hint" /></label>
            <span id="mfa-off-hint" className="muted">{t("mfa.codeHint")}</span>
            <button type="submit" className="primary">{t("mfa.turnOff")}</button>
            <button type="button" onClick={() => back("mfa-off")}>{t("common.cancel")}</button>
          </form>
        ) : s && (
          <>
            <p id="mfa-state" tabIndex={-1}>{t(s.on ? (s.required ? "mfa.required" : "mfa.on") : "mfa.off")}</p>
            {!s.on ? <button id="mfa-start" onClick={() => (setMsg(null), setAsk(true))}>{t("mfa.turnOn")}</button>
              : !s.required && <button id="mfa-off" onClick={() => setOff(true)}>{t("mfa.turnOff")}</button>}
          </>
        )}
      {msg && <p id="mfa-msg" role="status" tabIndex={-1} ref={msgRef}>{msg}</p>}
    </div>
  );
}
