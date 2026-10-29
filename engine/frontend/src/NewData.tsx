import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import schema from "../../schemas/v1/source.json";
import { api, call, canDesign, failure, meQuery, post, projectsQuery, tenantsQuery } from "./api";
import { focusSoon, pickTable, useUi } from "./store";
import { t } from "./i18n";

// UI-2 (D-69): create a project and load data without the chat, over the same actions the Main Agent uses
// (create_project, set_environment, add_data). Both dialogs are mounted once in App and opened through
// `useUi.dialog`, so the welcome starts and the Data tab buttons share them.
export const openDialog = (dialog: "project" | "data") => useUi.setState({ dialog });

// The projects the person may load data into: Editor or Owner, not a production environment (D-56).
export function useEditable() {
  const projects = useQuery(projectsQuery).data ?? [];
  const me = useQueries({ queries: projects.map(meQuery) });
  return projects.filter((_, i) => canDesign(me[i].data));
}

function Modal({ id, title, children }: { id: "project" | "data"; title: string; children: ReactNode }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const open = useUi((s) => s.dialog === id);
  useEffect(() => {
    if (open && !dlg.current!.open) dlg.current!.showModal();
    if (!open && dlg.current!.open) dlg.current!.close();
  }, [open]);
  return (
    // native <dialog>: modal, Escape closes, focus goes back to the opener
    <dialog ref={dlg} className="share-dialog new-data" aria-labelledby={`${id}-dialog-h`}
      onClose={() => useUi.setState((s) => ({ attach: false, dialog: s.dialog === id ? null : s.dialog }))}>
      <h2 id={`${id}-dialog-h`}>{title}</h2>
      {open && children}
      <div className="row"><button type="button" onClick={() => useUi.setState({ dialog: null })}>{t("common.close")}</button></div>
    </dialog>
  );
}

export function NewData() {
  return (
    <>
      <Modal id="project" title={t("newProject.title")}><NewProject /></Modal>
      <Modal id="data" title={t("addData.title")}><AddData /></Modal>
    </>
  );
}

function NewProject() {
  const qc = useQueryClient();
  const tenants = useQuery(tenantsQuery); // list_tenants answers workspace owners only, as create_project does
  const owner = !(tenants.error && (tenants.error as { status?: number }).status === 403);
  const [made, setMade] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const project = String(f.get("project")).trim();
    const branch = String(f.get("branch")).trim() || "main";
    setBusy(true);
    setStatus(t("newProject.creating"));
    try {
      await post("create_project", { project, name: String(f.get("name")).trim(), tenant: f.get("tenant") || null });
      if (branch !== "main") await post("set_environment", { project, branch }); // PC-9; `main` is the default
      await qc.invalidateQueries({ queryKey: ["projects"] });
      useUi.getState().setProject(project);
      setMade(project);
      setStatus("");
      focusSoon("new-project-next"); // the form unmounts
    } catch (err) {
      setStatus(t("newProject.failed", { error: (err as Error).message }));
    } finally {
      setBusy(false);
    }
  };
  if (made)
    return (
      <>
        <p role="status">{t("newProject.made", { project: made })}</p>
        <div className="row"><button id="new-project-next" className="primary" onClick={() => openDialog("data")}>{t("newProject.addData", { project: made })}</button></div>
      </>
    );
  return (
    <form className="fields" onSubmit={submit}>
      {!owner && <p role="alert">{t("newProject.ownerOnly")}</p>}
      <fieldset disabled={!owner || busy}>
        <label>{t("newProject.id")} <input id="new-project-id" name="project" required autoFocus aria-describedby="new-project-id-hint" autoComplete="off" /></label>
        <p id="new-project-id-hint" className="muted">{t("newProject.idHint")}</p>
        <label>{t("newProject.name")} <input name="name" required /></label>
        {!!tenants.data?.length && (
          <label>{t("newProject.tenant")}{" "}
            <select name="tenant" defaultValue="">
              <option value="">{t("newProject.noTenant")}</option>
              {tenants.data.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          </label>
        )}
        <label>{t("newProject.environment")} <input name="branch" defaultValue="main" pattern="[a-z0-9][a-z0-9_\-]{0,62}" aria-describedby="new-project-env-hint" /></label>
        <p id="new-project-env-hint" className="muted">{t("newProject.environmentHint")}</p>
        <p role="status" className="muted">{status}</p>
        <div className="row"><button type="submit" className="primary">{t("newProject.create")}</button></div>
      </fieldset>
    </form>
  );
}

const CHUNK = 64 * 1024 ** 2; // per PATCH; the server takes up to 256 MB (IN-7)
const OCTETS = "application/offset+octet-stream";
// add_data's E-16.1 CSV options; "" = auto-detect (IN-5), so only a chosen value is sent
const CSV = {
  delimiter: [["", "addData.auto"], [",", "addData.comma"], [";", "addData.semicolon"], ["|", "addData.pipe"], ["\t", "addData.tab"]],
  quote: [["", "addData.auto"], ['"', "addData.double"], ["'", "addData.single"]],
  escape: [["", "addData.auto"], ['"', "addData.double"], ["'", "addData.single"], ["\\", "addData.backslash"]],
  header: [["", "addData.auto"], ["true", "addData.yes"], ["false", "addData.no"]],
} as const;
// a table ID from a file name (storage.TABLE: a letter, then letters, digits, _)
export const tableName = (file: string) => {
  const s = file.replace(/\.[^.]*$/, "").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return /^[a-z]/.test(s) ? s.slice(0, 63) : `t_${s}`.slice(0, 63);
};
// the upload path's file name pattern (^\w[\w .-]*$) keeping the extension, which decides the format
export const fileName = (name: string) => name.replace(/[^\w .-]/g, "_").replace(/^[^\w]/, "f");

// IN-1..IN-4: every non-upload add_data source (ingest.READERS formats; "" = from the extension or folder)
type Kind = "file" | "path" | "s3" | "database" | "clickhouse" | "api";
const FORMATS = [["", "addData.auto"], ["parquet", "addData.fmtParquet"], ["csv", "addData.fmtCsv"], ["json", "addData.fmtJson"],
  ["xlsx", "addData.fmtXlsx"], ["delta", "addData.fmtDelta"], ["iceberg", "addData.fmtIceberg"], ["arrow", "addData.fmtArrow"],
  ["avro", "addData.fmtAvro"], ["sqlite", "addData.fmtSqlite"]] as const;
const val = (f: FormData, k: string) => String(f.get(k) ?? "").trim();
const secret = (f: FormData, k: string) => (val(f, k) ? `\${secret:${val(f, k)}}` : undefined);

// the add_data payload for a non-upload kind; undefined values are dropped by JSON
export function sourcePayload(kind: Exclude<Kind, "file">, f: FormData, project: string, table: string) {
  if (kind === "database" || kind === "clickhouse") {
    const port = val(f, "port");
    const scheme = kind === "clickhouse" ? (f.get("tls") ? "clickhouses" : "clickhouse") : val(f, "kind");
    return {
      project, table, location: `${scheme}://${val(f, "host")}${port ? `:${port}` : ""}/${val(f, "database")}`,
      database: { table: val(f, "source_table"), user: secret(f, "user"), password: secret(f, "password"),
        incremental: val(f, "incremental") || undefined, sync: f.get("sync") ? true : undefined }, // database kind only (its form)
    };
  }
  const s3 = kind === "s3" ? { region: val(f, "region") || undefined, endpoint: val(f, "endpoint") || undefined,
    url_style: val(f, "url_style") || undefined, use_ssl: f.get("plain_http") ? false : undefined,
    key_id: secret(f, "key_id"), secret: secret(f, "s3_secret") } : undefined;
  return { project, table, location: val(f, "location"), format: val(f, "format") || undefined,
    sqlite: val(f, "format") === "sqlite" ? { table: val(f, "sqlite_table") } : undefined, s3, csv: csvOptions(f) ?? undefined };
}

// API-SRC.ui: the API kind's choices and limits come from schemas/v1/source.json, the file add_data writes; the server
// checks the rest (pydantic, 422 with the field path)
const A = schema.properties.api.properties;
const AUTHS = A.auth.oneOf.map((o) => o.properties.kind.const), PAGES = A.pagination.properties.kind.enum, MAXP = A.pagination.properties.max_pages;
export type Col = { name: string; pointer: string; type: string };
const num = (f: FormData, k: string) => (val(f, k) ? Number(val(f, k)) : undefined);

export function apiPayload(f: FormData, project: string, table: string, columns: Col[]) {
  const kind = val(f, "auth_kind"), page = val(f, "page_kind"), json = val(f, "api_format") !== "csv";
  const oauth = { token_url: val(f, "token_url"), client_secret: secret(f, "client_secret"), scope: val(f, "scope") || undefined };
  const auth = !kind ? undefined : kind === "bearer" ? { kind, token: secret(f, "token") }
    : kind === "header" ? { kind, name: val(f, "header_name"), value: secret(f, "header_value") }
    : kind === "basic" ? { kind, username: secret(f, "username"), password: secret(f, "basic_password") }
    : kind === "oauth_client" ? { kind, ...oauth, client_id: secret(f, "client_id_secret") }
    : { kind, authorize_url: val(f, "authorize_url"), ...oauth, client_id: val(f, "client_id") };
  const cols = columns.filter((c) => c.name.trim());
  return { project, table, location: val(f, "location"), api: {
    format: json ? undefined : "csv", records: (json && val(f, "records")) || undefined, auth,
    pagination: page ? { kind: page, param: val(f, "page_param") || undefined, start: num(f, "page_start"),
      pointer: val(f, "page_pointer") || undefined, max_pages: num(f, "max_pages") } : undefined,
    columns: cols.length ? Object.fromEntries(cols.map((c) => [c.name.trim(), { type: c.type.trim(), pointer: (json && c.pointer.trim()) || undefined }])) : undefined,
    incremental: val(f, "inc_field") && val(f, "inc_param") ? { field: val(f, "inc_field"), param: val(f, "inc_param") } : undefined,
    every_hours: num(f, "every_hours"),
  } };
}

// API-SRC.3's connect flow leaves the app (provider consent, then `/#source-oauth=<outcome>`), so the form is kept in
// this tab's sessionStorage across it: field values and secret NAMES only, never a secret (SC-4)
const DRAFT = "abi.sourceDraft";
const OUTCOMES = ["connected", "expired", "browser", "denied", "role", "failed", "no_refresh"]; // oauth._callback's codes
const TTL = 10 * 60_000; // oidc.FLOW_TTL: a connection no add_data took within it is dropped (API-SRC.3)
type Draft = Record<string, string>;
const stored = (): Draft | null => { try { return JSON.parse(sessionStorage.getItem(DRAFT) ?? "null"); } catch { return null; } };
const store = (d: Draft | null) => {
  try { if (d) sessionStorage.setItem(DRAFT, JSON.stringify(d)); else sessionStorage.removeItem(DRAFT); } catch { /* blocked storage: the form starts empty */ }
};
// main.tsx, before the first render: clear the hash, reopen Add data with the kept form and the outcome
export function sourceOauthReturn() {
  const code = location.hash.match(/^#source-oauth=(.*)$/)?.[1];
  if (code === undefined) return;
  history.replaceState(null, "", location.pathname + location.search);
  const d = stored();
  if (!d) return;
  store({ ...d, outcome: OUTCOMES.includes(code) ? code : "failed", at: String(Date.now()) });
  useUi.setState({ dialog: "data" });
}

export function csvOptions(f: FormData) {
  const o: Record<string, string | boolean> = {};
  for (const k of ["delimiter", "quote", "escape"] as const) if (f.get(k)) o[k] = String(f.get(k));
  if (f.get("header")) o.header = f.get("header") === "true";
  if (f.get("lenient")) o.strict_mode = false;
  return Object.keys(o).length ? o : null;
}

function AddData() {
  const qc = useQueryClient();
  const editable = useEditable();
  const picked = useUi((s) => s.project);
  const [draft] = useState(() => { const d = stored(); return d?.outcome ? d : null; }); // back from the provider
  useEffect(() => () => store(null), []); // a closed dialog forgets it
  const [source, setSource] = useState<Kind>(draft ? "api" : "file");
  const [table, setTable] = useState(draft?.table ?? "");
  const [project, setProject] = useState(draft?.project ?? "");
  const [auth, setAuth] = useState(draft?.auth_kind ?? "");
  const [paging, setPaging] = useState(draft?.page_kind ?? "");
  const [apiFormat, setApiFormat] = useState(draft?.api_format ?? "json");
  const [columns, setColumns] = useState<Col[]>(() => (draft?.columns ? JSON.parse(draft.columns) : []));
  const [, tick] = useState(0);
  const left = draft?.outcome === "connected" ? Number(draft.at) + TTL - Date.now() : -1;
  useEffect(() => {
    if (left <= 0) return;
    const h = setTimeout(() => tick((n) => n + 1), left); // shows "expired" when the unsaved connection is dropped
    return () => clearTimeout(h);
  }, [left > 0]);
  const oauth = !draft ? "none" : draft.outcome === "connected" && left <= 0 ? "stale" : draft.outcome;
  const [done, setDone] = useState<number | null>(null); // upload percent
  const [status, setStatus] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const kept = useRef<{ key: string; url: string } | null>(null); // a finished upload whose load failed: retry without re-sending
  const target = project || (picked && editable.includes(picked) ? picked : editable[0]) || "";
  const secrets = useQuery({ queryKey: ["list_secrets", target], queryFn: () => post<{ secrets: string[] }>("list_secrets", { project: target }),
    enabled: !!target && ["database", "clickhouse", "s3", "api"].includes(source) });
  const [format, setFormat] = useState("");

  if (!editable.length)
    return (
      <>
        <p>{t("addData.noProject")}</p>
        <div className="row"><button className="primary" autoFocus onClick={() => openDialog("project")}>{t("newProject.title")}</button></div>
      </>
    );

  const upload = async (file: File, csv: object | null) => {
    const q = csv ? `?csv=${encodeURIComponent(JSON.stringify(csv))}` : "";
    const key = [target, table, file.name, file.size, file.lastModified].join("/");
    let url = kept.current?.key === key ? kept.current.url : null;
    let offset = url ? file.size : 0;
    if (!url) {
      const r = await call(`/api/uploads/${target}/${table}/${encodeURIComponent(fileName(file.name))}`, { method: "POST", headers: { "Upload-Length": String(file.size) } });
      if (!r.ok) throw failure(r, await r.json().catch(() => null));
      url = r.headers.get("Location")!;
    }
    for (;;) {
      const end = Math.min(offset + CHUNK, file.size);
      const r = await call(url + q, { method: "PATCH", headers: { "Content-Type": OCTETS, "Upload-Offset": String(offset) }, body: file.slice(offset, end) });
      if (r.status === 404 && kept.current) { // the kept upload is gone (swept): send the file again
        kept.current = null;
        return upload(file, csv);
      }
      if (!r.ok) {
        if (end === file.size && r.status !== 409) kept.current = { key, url }; // all bytes are in; only the load failed
        throw failure(r, await r.json().catch(() => null));
      }
      if (end === file.size) return (kept.current = null);
      setDone(Math.floor((end / file.size) * 100));
      offset = end;
    }
  };

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true);
    setFailed(false);
    setStatus(t("addData.loading"));
    try {
      if (source === "file") {
        setDone(0);
        await upload(f.get("file") as File, csvOptions(f));
      } else await post("add_data", source === "api" ? apiPayload(f, target, table, columns) : sourcePayload(source, f, target, table));
      store(null);
      await Promise.all(["tables", "projects", "data_quality"].map((k) => qc.invalidateQueries({ queryKey: [k] }))); // UI-DS.8: Sources' status
      const tab = `${target}/${table}`;
      if (useUi.getState().attach) { // UI-DS.5.1: the composer's attach: the table joins the chat's context
        pickTable(tab, true);
        useUi.setState({ dialog: null, attach: false });
        return focusSoon("chat-input");
      }
      useUi.setState({ dialog: null });
      useUi.getState().open(tab);
      focusSoon(`ws-${tab}`);
    } catch (err) {
      setStatus(t("addData.failed", { error: (err as Error).message }));
      setFailed(true);
    } finally {
      setBusy(false);
      setDone(null);
    }
  };

  // API-SRC.3: open the provider's consent page; the form waits in sessionStorage (DRAFT)
  const connect = async (form: HTMLFormElement) => {
    if (!form.reportValidity()) return;
    const f = new FormData(form);
    store({ ...(Object.fromEntries(f) as Draft), project: target, table, columns: JSON.stringify(columns) });
    try {
      const p = apiPayload(f, target, table, columns); // `location`: its access tokens go to that host only
      const r = await api<{ url: string }>("/api/sources/oauth/start", { method: "POST",
        body: JSON.stringify({ project: target, source: table, location: p.location, auth: p.api.auth }) });
      window.location.assign(r.url);
    } catch (err) {
      store(null);
      setStatus(t("addData.failed", { error: (err as Error).message }));
      setFailed(true);
    }
  };
  const d = (k: string) => draft?.[k];
  const secretSelect = (name: string, label: string, optional = false) => (
    <label>{label}{" "}
      <select name={name} required={!optional} defaultValue={d(name)}>
        {optional && <option value="">{t("addData.noSecret")}</option>}
        {(secrets.data?.secrets ?? []).map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
    </label>
  );
  const kinds = [["file", "addData.file"], ["path", "addData.path"], ["s3", "addData.s3"], ["database", "addData.database"],
    ["clickhouse", "addData.clickhouse"], ["api", "addData.api"]] as const;
  const pattern = "[A-Za-z_][A-Za-z0-9_]{0,62}";
  const setCol = (i: number, k: keyof Col, v: string) => setColumns((c) => c.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  const apiForm = (
    <>
      <label>{t("addData.apiUrl")} <input name="location" required pattern="https://.+" autoComplete="off" defaultValue={d("location")} aria-describedby="add-data-api-hint" /></label>
      <p id="add-data-api-hint" className="muted">{t("addData.apiHint")}</p>
      <label>{t("addData.format")}{" "}
        <select name="api_format" value={apiFormat} onChange={(e) => setApiFormat(e.target.value)}>
          {A.format.enum.map((v) => <option key={v} value={v}>{t(v === "csv" ? "addData.fmtCsv" : "addData.fmtJson")}</option>)}
        </select>
      </label>
      {apiFormat === "json" && <>
        <label>{t("addData.apiRecords")} <input name="records" autoComplete="off" defaultValue={d("records")} aria-describedby="add-data-records-hint" /></label>
        <p id="add-data-records-hint" className="muted">{t("addData.apiRecordsHint")}</p>
      </>}
      <label>{t("addData.apiAuth")}{" "}
        <select name="auth_kind" value={auth} onChange={(e) => setAuth(e.target.value)}>
          <option value="">{t("addData.apiAuthNone")}</option>
          {AUTHS.map((k) => <option key={k} value={k}>{t(`addData.apiAuth.${k}`)}</option>)}
        </select>
      </label>
      {auth && secrets.data && !secrets.data.secrets.length && <p role="alert">{t("addData.noSecrets")}</p>}
      {auth === "bearer" && secretSelect("token", t("addData.apiToken"))}
      {auth === "header" && <>
        <label>{t("addData.apiHeader")} <input name="header_name" required pattern="[A-Za-z0-9\-]{1,100}" autoComplete="off" defaultValue={d("header_name")} /></label>
        {secretSelect("header_value", t("addData.apiHeaderValue"))}
      </>}
      {auth === "basic" && <>{secretSelect("username", t("addData.user"))}{secretSelect("basic_password", t("addData.password"))}</>}
      {auth.startsWith("oauth") && <>
        {auth === "oauth_code" && <label>{t("addData.apiAuthorizeUrl")} <input name="authorize_url" required pattern="https://.+" autoComplete="off" defaultValue={d("authorize_url")} /></label>}
        <label>{t("addData.apiTokenUrl")} <input name="token_url" required pattern="https://.+" autoComplete="off" defaultValue={d("token_url")} /></label>
        {auth === "oauth_code" ? <>
          <label>{t("addData.apiClientId")} <input name="client_id" required autoComplete="off" defaultValue={d("client_id")} aria-describedby="add-data-client-hint" /></label>
          <p id="add-data-client-hint" className="muted">{t("addData.apiClientIdHint")}</p>
        </> : secretSelect("client_id_secret", t("addData.apiClientId"))}
        {secretSelect("client_secret", t("addData.apiClientSecret"), auth === "oauth_code")}
        <label>{t("addData.apiScope")} <input name="scope" autoComplete="off" defaultValue={d("scope")} /></label>
      </>}
      {auth && <p className="muted">{t("addData.secretsHint")}</p>}
      {auth === "oauth_code" && <div className="row">
        <button type="button" id="add-data-connect" onClick={(e) => connect(e.currentTarget.form!)}>{oauth === "none" ? t("addData.apiConnect") : t("addData.apiReconnect")}</button>
        <p role="status" id="add-data-oauth">{t(`addData.oauth.${oauth}`)}</p>
      </div>}
      <label>{t("addData.apiPaging")}{" "}
        <select name="page_kind" value={paging} onChange={(e) => setPaging(e.target.value)}>
          <option value="">{t("addData.apiPagingNone")}</option>
          {PAGES.map((k) => <option key={k} value={k}>{t(`addData.apiPaging.${k}`)}</option>)}
        </select>
      </label>
      {paging && paging !== "next_link" && <label>{t("addData.apiPageParam")} <input name="page_param" required autoComplete="off" defaultValue={d("page_param")} /></label>}
      {(paging === "page" || paging === "offset") && <label>{t("addData.apiPageStart")} <input name="page_start" type="number" min={0} defaultValue={d("page_start")} /></label>}
      {(paging === "cursor" || paging === "next_link") && <label>{t("addData.apiPagePointer")} <input name="page_pointer" required={paging === "cursor"} autoComplete="off" defaultValue={d("page_pointer")} /></label>}
      {paging && <label>{t("addData.apiMaxPages")} <input name="max_pages" type="number" min={MAXP.minimum} max={MAXP.maximum} defaultValue={d("max_pages") ?? MAXP.default} /></label>}
      <fieldset>
        <legend>{t("addData.apiIncremental")}</legend>
        <p className="muted" id="add-data-inc-hint">{t("addData.apiIncrementalHint")}</p>
        <label>{t("addData.apiIncField")} <input name="inc_field" pattern={pattern} autoComplete="off" defaultValue={d("inc_field")} aria-describedby="add-data-inc-hint" /></label>
        <label>{t("addData.apiIncParam")} <input name="inc_param" autoComplete="off" defaultValue={d("inc_param")} /></label>
      </fieldset>
      <label>{t("addData.apiEvery")} <input name="every_hours" type="number" min={A.every_hours.minimum} max={A.every_hours.maximum} defaultValue={d("every_hours")} aria-describedby="add-data-every-hint" /></label>
      <p id="add-data-every-hint" className="muted">{t("addData.apiEveryHint")}</p>
      <fieldset>
        <legend>{t("addData.apiColumns")}</legend>
        <p className="muted">{t("addData.apiColumnsHint")}</p>
        {columns.map((c, i) => (
          <div className="row" key={i}>
            <label>{t("addData.apiColumnName")} <input value={c.name} required pattern={pattern} autoComplete="off" onChange={(e) => setCol(i, "name", e.target.value)} /></label>
            {apiFormat === "json" && <label>{t("addData.apiColumnPointer")} <input value={c.pointer} autoComplete="off" onChange={(e) => setCol(i, "pointer", e.target.value)} /></label>}
            <label>{t("addData.apiColumnType")} <input value={c.type} required autoComplete="off" onChange={(e) => setCol(i, "type", e.target.value)} /></label>
            <button type="button" onClick={() => setColumns((x) => x.filter((_, j) => j !== i))}
              aria-label={t("addData.apiColumnRemove", { n: String(i + 1) })}>{t("addData.apiRemove")}</button>
          </div>
        ))}
        <div className="row"><button type="button" id="add-data-column" onClick={() => setColumns((c) => [...c, { name: "", pointer: "", type: "" }])}>{t("addData.apiAddColumn")}</button></div>
      </fieldset>
    </>
  );
  const csvAdvanced = (
    <details>
      <summary>{t("addData.advanced")}</summary>
      <p className="muted">{t("addData.advancedHint")}</p>
      {(Object.keys(CSV) as (keyof typeof CSV)[]).map((k) => (
        <label key={k}>{t(`addData.${k}`)}{" "}
          <select name={k} defaultValue="">{CSV[k].map(([v, l]) => <option key={l} value={v}>{t(l)}</option>)}</select>
        </label>
      ))}
      <label><input type="checkbox" name="lenient" /> {t("addData.lenient")}</label>
    </details>
  );
  const formatSelect = (
    <>
      <label>{t("addData.format")}{" "}
        <select name="format" value={format} onChange={(e) => setFormat(e.target.value)} aria-describedby="add-data-format-hint">
          {FORMATS.map(([v, l]) => <option key={v} value={v}>{t(l)}</option>)}
        </select>
      </label>
      <p id="add-data-format-hint" className="muted">{t("addData.formatHint")}</p>
      {format === "sqlite" && <label>{t("addData.sqliteTable")} <input name="sqlite_table" required pattern="[A-Za-z_][A-Za-z0-9_]{0,62}" autoComplete="off" /></label>}
      {(format === "" || format === "csv") && csvAdvanced}
    </>
  );
  return (
    <form className="fields" onSubmit={submit}>
      <fieldset disabled={busy}>
        <label>{t("addData.project")}{" "}
          <select id="add-data-project" value={target} onChange={(e) => (setProject(e.target.value), (kept.current = null))} autoFocus>
            {editable.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <fieldset className="choice">
          <legend>{t("addData.source")}</legend>
          {kinds.map(([k, l]) => <label key={k}><input type="radio" name="source" value={k} checked={source === k} onChange={() => setSource(k)} /> {t(l)}</label>)}
        </fieldset>
        {source === "file" ? (
          <>
            <label>{t("addData.fileLabel")}{" "}
              <input id="add-data-file" type="file" name="file" required accept=".csv,.tsv,.txt,.parquet,.json,.ndjson,.jsonl,.xlsx"
                aria-describedby="add-data-file-hint" onChange={(e) => { const n = e.target.files?.[0]?.name; if (n && !table) setTable(tableName(n)); }} />
            </label>
            <p id="add-data-file-hint" className="muted">{t("addData.fileHint")}</p>
            {csvAdvanced}
          </>
        ) : source === "path" ? (
          <>
            <label>{t("addData.location")} <input name="location" required autoComplete="off" aria-describedby="add-data-location-hint" /></label>
            <p id="add-data-location-hint" className="muted">{t("addData.locationHint")}</p>
            {formatSelect}
          </>
        ) : source === "api" ? apiForm : source === "s3" ? (
          <>
            <label>{t("addData.s3Location")} <input name="location" required pattern="s3://.+" autoComplete="off" aria-describedby="add-data-s3-hint" /></label>
            <p id="add-data-s3-hint" className="muted">{t("addData.s3Hint")}</p>
            {formatSelect}
            <label>{t("addData.region")} <input name="region" pattern="[a-z0-9\-]{1,50}" autoComplete="off" /></label>
            <label>{t("addData.endpoint")} <input name="endpoint" pattern="[A-Za-z0-9.\-]+(:[0-9]+)?" autoComplete="off" aria-describedby="add-data-endpoint-hint" /></label>
            <p id="add-data-endpoint-hint" className="muted">{t("addData.endpointHint")}</p>
            <label>{t("addData.urlStyle")}{" "}
              <select name="url_style" defaultValue=""><option value="">{t("addData.auto")}</option><option value="path">{t("addData.urlPath")}</option><option value="vhost">{t("addData.urlVhost")}</option></select>
            </label>
            <label><input type="checkbox" name="plain_http" /> {t("addData.plainHttp")}</label>
            {secretSelect("key_id", t("addData.keyId"), true)}{secretSelect("s3_secret", t("addData.s3Secret"), true)}
            <p className="muted">{t("addData.s3SecretsHint")}</p>
          </>
        ) : (
          <>
            {source === "clickhouse" ? (
              <>
                <p className="muted">{t("addData.clickhouseHint")}</p>
                <label><input type="checkbox" name="tls" /> {t("addData.tls")}</label>
              </>
            ) : <label>{t("addData.kind")}{" "}
              <select name="kind" defaultValue="postgres"><option value="postgres">{t("addData.postgres")}</option><option value="mysql">{t("addData.mysql")}</option></select>
            </label>}
            <label>{t("addData.host")} <input name="host" required autoComplete="off" /></label>
            <label>{t("addData.port")} <input name="port" inputMode="numeric" pattern="[0-9]{1,5}" aria-describedby="add-data-port-hint" /></label>
            <p id="add-data-port-hint" className="muted">{source === "clickhouse" ? t("addData.chPortHint") : t("addData.portHint")}</p>
            <label>{t("addData.databaseName")} <input name="database" required autoComplete="off" /></label>
            <label>{source === "clickhouse" ? t("addData.chTable") : t("addData.sourceTable")} <input name="source_table" required autoComplete="off"
              pattern={source === "clickhouse" ? "[A-Za-z_][A-Za-z0-9_]{0,62}" : "([A-Za-z_][A-Za-z0-9_]{0,62}\\.)?[A-Za-z_][A-Za-z0-9_]{0,62}"} /></label>
            {secrets.data && !secrets.data.secrets.length ? <p role="alert">{t("addData.noSecrets")}</p> : (
              <>{secretSelect("user", t("addData.user"))}{secretSelect("password", t("addData.password"))}</>
            )}
            <p className="muted">{t("addData.secretsHint")}</p>
            {source === "database" && (
              <>
                <label>{t("addData.incremental")} <input name="incremental" autoComplete="off" aria-describedby="add-data-incremental-hint" /></label>
                <p id="add-data-incremental-hint" className="muted">{t("addData.incrementalHint")}</p>
                <label><input type="checkbox" name="sync" /> {t("addData.sync")}</label>
              </>
            )}
          </>
        )}
        <label>{t("addData.table")}{" "}
          <input id="add-data-table" required value={table} pattern="[A-Za-z_][A-Za-z0-9_]{0,62}" aria-describedby="add-data-table-hint"
            onChange={(e) => (setTable(e.target.value), (kept.current = null))} />
        </label>
        <p id="add-data-table-hint" className="muted">{t("addData.tableHint")}</p>
        {done !== null && <progress max={100} value={done} aria-label={t("addData.progress")} />}
        <p role="status" className={failed ? "error" : "muted"}>{status}</p>
        <div className="row"><button id="add-data-submit" type="submit" className="primary">{t("addData.load")}</button></div>
      </fieldset>
    </form>
  );
}
