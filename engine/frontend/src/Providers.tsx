import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { aliasesQuery, confirmable, type LlmProvider } from "./api";
import { t } from "./i18n";
import { Confirm } from "./mel";
import { focusSoon } from "./store";

type Ask = { action: string; body: object; done: string; back: string; message: string; token: string };

// "alias=model" lines <-> the provider's models map
export const modelLines = (m: Record<string, string>) => Object.entries(m).map(([a, n]) => `${a}=${n}`).join("\n");
export const parseModels = (text: string) => Object.fromEntries(text.split("\n").map((l) => l.trim()).filter(Boolean)
  .map((l) => { const [a, ...n] = l.split("="); return [a.trim(), n.join("=").trim()]; }));

// MODEL-PICKER (AG-6, D-94) workspace tab "*/#providers": the workspace LLM providers in failover order (add, edit, remove,
// reorder, test call) and the Main Agent's model. Workspace owners only (the actions refuse everyone else); keys are
// ${secret:NAME} references, never values (SC-4/D-2); adding or changing a host asks the server's confirmation.
export default function Providers() {
  const q = useQuery(aliasesQuery);
  const qc = useQueryClient();
  const [form, setForm] = useState<LlmProvider | null>(null); // the provider being edited; null: a new one
  const [note, setNote] = useState<{ error: boolean; text: string } | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [saves, setSaves] = useState(0); // a new form after each save
  const rows = q.data?.providers ?? [];

  const act = async (action: string, body: object, done: string, back: string, token?: string) => {
    setNote(null);
    const r = await confirmable(action, body, token);
    if (r.confirm) return setAsk({ action, body, done, back, message: r.confirm.message, token: r.confirm.token }); // Confirm takes focus
    setAsk(null);
    if (!r.ok) return setNote({ error: true, text: r.error });
    await qc.invalidateQueries({ queryKey: aliasesQuery.queryKey });
    setNote({ error: false, text: action === "test_llm_provider" ? t(r.json?.answered ? "providers.answered" : "providers.empty", { model: r.json?.model ?? "" }) : done });
    if (action === "set_llm_provider") setForm(null), setSaves((n) => n + 1);
    focusSoon(back);
  };
  const save = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const models = parseModels(String(f.get("models")));
    const key = String(f.get("key")).trim();
    const id = String(f.get("id")).trim();
    act("set_llm_provider", {
      id, base_url: String(f.get("base_url")).trim(), api_key: key ? `\${secret:${key}}` : null, models,
      position: form?.position ?? rows.reduce((n, p) => Math.max(n, p.position + 1), 0),
      prices: form?.prices ?? {}, efforts: Object.fromEntries(Object.entries(form?.efforts ?? {}).filter(([a]) => a in models)),
    }, t("providers.saved", { id }), "provider-id");
  };
  const move = (i: number, by: number) => {
    const ids = rows.map((p) => p.id);
    [ids[i], ids[i + by]] = [ids[i + by], ids[i]];
    act("order_llm_providers", { ids }, t("providers.ordered"), `${by < 0 ? "up" : "down"}-${rows[i].id}`);
  };
  const remove = (p: LlmProvider, i: number) => {
    const next = rows[i + 1] ?? rows[i - 1]; // WCAG 2.4.3: the row goes away; its neighbour's Remove, else the form
    act("delete_llm_provider", { id: p.id }, t("providers.removed", { id: p.id }), next ? `remove-${next.id}` : "provider-id");
  };

  return (
    <div className="tool providers">
      <h2 id="providers-title">{t("providers.title")}</h2>
      <p className="muted">{t("providers.explain")}</p>
      {q.error && <p role="alert">{q.error.message}</p>}
      {note && <p role={note.error ? "alert" : "status"} className={note.error ? "error" : undefined}>{note.text}</p>}
      {ask && <Confirm label={t("providers.confirm")} message={ask.message} onCancel={() => setAsk(null)} back={ask.back}
        onConfirm={() => act(ask.action, ask.body, ask.done, ask.back, ask.token)} />}
      {q.data && (<>
        <label>{t("providers.main")}{" "}
          <select id="main-model" value={q.data.main} aria-describedby="main-model-hint"
            onChange={(e) => act("set_main_agent_model", { model: e.target.value }, t("providers.mainSaved"), "main-model")}>
            <option value="auto">{t("providers.auto")}</option>
            {q.data.aliases.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            {q.data.main !== "auto" && !q.data.aliases.some((a) => a.id === q.data.main) && <option value={q.data.main}>{q.data.main}</option>}
          </select>
        </label>
        <p id="main-model-hint" className="muted">{t("providers.mainHint")}</p>
        <table>
          <caption>{t("providers.caption")}</caption>
          <thead><tr>
            <th scope="col">{t("providers.id")}</th><th scope="col">{t("providers.baseUrl")}</th>
            <th scope="col">{t("providers.models")}</th><th scope="col">{t("providers.key")}</th>
            <th scope="col"><span className="sr-only">{t("providers.actions")}</span></th>
          </tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={5} className="muted">{t("providers.none")}</td></tr>}
            {rows.map((p, i) => (
              <tr key={p.id}>
                <th scope="row">{p.id}</th>
                <td>{p.base_url}</td>
                <td><ul className="plain">{Object.entries(p.models).map(([a, n]) => (
                  <li key={a}>{t("providers.model", { alias: a, model: n })}{" "}
                    <button type="button" className="sm" id={`test-${p.id}-${a}`} aria-label={t("providers.testLabel", { id: p.id, alias: a })}
                      onClick={() => act("test_llm_provider", { id: p.id, alias: a }, "", `test-${p.id}-${a}`)}>{t("providers.test")}</button>
                  </li>))}</ul></td>
                <td>{p.api_key ? p.api_key.slice(9, -1) : t("providers.noKey")}</td>
                <td>
                  <button type="button" className="sm" id={`up-${p.id}`} disabled={i === 0} aria-label={t("providers.upLabel", { id: p.id })} onClick={() => move(i, -1)}>{t("providers.up")}</button>{" "}
                  <button type="button" className="sm" id={`down-${p.id}`} disabled={i === rows.length - 1} aria-label={t("providers.downLabel", { id: p.id })} onClick={() => move(i, 1)}>{t("providers.down")}</button>{" "}
                  <button type="button" className="sm" id={`edit-${p.id}`} aria-label={t("providers.editLabel", { id: p.id })} onClick={() => (setForm(p), focusSoon("provider-base"))}>{t("providers.edit")}</button>{" "}
                  <button type="button" className="sm" id={`remove-${p.id}`} aria-label={t("providers.removeLabel", { id: p.id })} onClick={() => remove(p, i)}>{t("providers.remove")}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </>)}
      <form aria-labelledby="provider-form-h" onSubmit={save} key={`${form?.id ?? ""}/${saves}`}>
        <h3 id="provider-form-h">{form ? t("providers.editHeading", { id: form.id }) : t("providers.add")}</h3>
        <div className="row">
          <label>{t("providers.id")} <input id="provider-id" name="id" required pattern="[a-z][a-z0-9_\-]{0,62}" defaultValue={form?.id} readOnly={!!form} /></label>
          <label>{t("providers.baseUrl")} <input id="provider-base" name="base_url" type="url" required maxLength={500} defaultValue={form?.base_url} placeholder={t("providers.baseUrlExample")} /></label>
          <label>{t("providers.keySecret")} <input id="provider-key" name="key" pattern="[A-Za-z0-9_]{1,100}" autoComplete="off" aria-describedby="provider-key-hint"
            defaultValue={form?.api_key?.slice(9, -1) ?? ""} /></label>
        </div>
        <p id="provider-key-hint" className="muted">{t("providers.keyHint")}</p>
        <label>{t("providers.models")}{" "}
          <textarea id="provider-models" name="models" rows={3} required aria-describedby="provider-models-hint" defaultValue={form ? modelLines(form.models) : ""} />
        </label>
        <p id="provider-models-hint" className="muted">{t("providers.modelsHint")}</p>
        <button type="submit" className="primary">{t("common.save")}</button>{" "}
        {form && <button type="button" onClick={() => (setForm(null), focusSoon("provider-id"))}>{t("common.cancel")}</button>}
      </form>
    </div>
  );
}
