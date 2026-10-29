import Editor from "@monaco-editor/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { canDesign, confirmable, measuresQuery, meQuery, type MeasureSpec } from "./api";
import { ReadOnly } from "./Production";
import { actionErrors, Confirm, Errors, errorSpan, MEL, suggestions, type ActionError } from "./mel";
import { monaco } from "./monaco";
import { focusSoon, useServerChange, useUi } from "./store";
import { t } from "./i18n";

monaco.languages.register({ id: "mel" });
monaco.languages.setMonarchTokensProvider("mel", MEL);
monaco.languages.registerCompletionItemProvider("mel", {
  provideCompletionItems: (model, position) => {
    const w = model.getWordUntilPosition(position);
    const range = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: w.startColumn, endColumn: w.endColumn };
    return { suggestions: suggestions().map((s) => ({ ...s, range, kind: monaco.languages.CompletionItemKind.Function,
      insertTextRules: monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet })) };
  },
});

// UI-2 measure editor tab ("project/#measure/<id>"; an empty id is a new measure). Saves through set_measure (PC-3 checks).
export default function Measure({ project, id }: { project: string; id: string }) {
  const q = useQuery(measuresQuery(project));
  const me = useQuery(meQuery(project));
  if (q.error) return <p role="alert">{t("measure.loadFailed", { project, error: q.error.message })}</p>;
  if (!q.data) return <p role="status">{t("measure.loading", { id })}</p>;
  const m = id ? q.data.find((x) => x.id === id) : { id: "", name: "", expression: "" };
  if (!m) return <p role="alert">{t("measure.notFound", { id, project })}</p>;
  return <Form project={project} m={m} readOnly={!canDesign(me.data)} />;
}

function Form({ project, m, readOnly }: { project: string; m: MeasureSpec; readOnly: boolean }) {
  const tab = `${project}/#measure/${m.id}`;
  const [draft, setDraft] = useState(m);
  useServerChange(m, (now, old) => { // "" is no description (save sends undefined), so a saved draft still follows later pulls
    if (JSON.stringify({ ...draft, description: draft.description || undefined }) === JSON.stringify(old)) setDraft(now);
  });
  const [errors, setErrors] = useState<ActionError[]>([]);
  const [status, setStatus] = useState("");
  const [confirm, setConfirm] = useState<{ message: string; token: string } | null>(null);
  const ed = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const qc = useQueryClient();

  useEffect(() => { // compile errors as markers (squiggle + hover) on the expression
    const model = ed.current?.getModel();
    if (!model) return;
    // a delete refusal's path is the expression of the measure that uses this one, not this editor's
    monaco.editor.setModelMarkers(model, "mel", errors.filter((e) => e.path?.endsWith(".expression") && !e.error.includes(" is used by ")).map((e) => {
      const [s, end] = errorSpan(e, model.getValue()).map((o) => model.getPositionAt(o));
      return { severity: monaco.MarkerSeverity.Error, message: `${e.error} ${e.fix ?? ""}`,
        startLineNumber: s.lineNumber, startColumn: s.column, endLineNumber: end.lineNumber, endColumn: end.column };
    }));
  }, [errors]);

  const send = async (action: string, body: object, token?: string) => {
    setErrors([]);
    setStatus("");
    const r = await confirmable(action, { project, ...body }, token);
    if (r.confirm) return setConfirm(r.confirm); // Confirm takes focus
    if (confirm) focusSoon("measure-delete"); // WCAG 2.4.3: the Confirm button is gone
    setConfirm(null);
    if (!r.ok) return setErrors(actionErrors(r.status, r.json?.detail));
    qc.invalidateQueries({ queryKey: ["semantic_model", project] });
    return r.json;
  };

  const save = async () => {
    const out = await send("set_measure", { ...draft, description: draft.description || undefined });
    if (!out) return;
    const gaps = (out.readiness?.missing ?? []).filter((g: string) => g.includes(`'${draft.id}'`));
    setStatus(t("measure.saved", { id: draft.id }) + (gaps.length ? " " + gaps.join(" ") : ""));
    if (!m.id) { // the new-measure tab becomes the measure's tab
      const { close, open } = useUi.getState();
      close(tab);
      open(`${project}/#measure/${draft.id}`);
      focusSoon(`ws-${project}/#measure/${draft.id}`);
    }
  };

  const remove = async (token?: string) => {
    if (!(await send("delete_measure", { id: m.id }, token))) return;
    const { close } = useUi.getState();
    close(tab);
    const next = useUi.getState().active;
    focusSoon(next ? `ws-${next}` : "chat-input"); // no screen left: the Chats home
  };

  return (
    <form className="measure" aria-labelledby="measure-title" onSubmit={(e) => { e.preventDefault(); save(); }}>
      <h2 id="measure-title">{m.id ? t("measure.title", { name: m.name }) : t("measure.new")}</h2>
      {readOnly && <ReadOnly project={project} why={t("measure.readOnly", { project })} />}
      <fieldset className="plain" disabled={readOnly}>
      {!m.id && <label>{t("measure.id")} <input required pattern="[a-z][a-z0-9_\-]{0,62}" value={draft.id} onChange={(e) => setDraft({ ...draft, id: e.target.value })}
        aria-describedby="measure-id-help" /></label>}
      {!m.id && <p id="measure-id-help" className="muted">{t("measure.idHelp", { id: draft.id || "id" })}</p>}
      <label>{t("measure.name")} <input required value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>
      <label>{t("measure.description")} <input value={draft.description ?? ""} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
      <span id="measure-expr-label">{t("measure.expression")}</span>
      <p className="muted" id="measure-expr-help">{t("measure.exprHelp")}</p>
      <div className="mel">
        <Editor height="160px" language="mel" value={draft.expression} onChange={(v) => setDraft((d) => ({ ...d, expression: v ?? "" }))}
          onMount={(e) => { ed.current = e; }} loading={<p role="status">{t("common.loadingEditor")}</p>}
          options={{ readOnly, ariaLabel: t("measure.exprAria"), tabFocusMode: true, minimap: { enabled: false },
            lineNumbers: "off", scrollBeyondLastLine: false, wordWrap: "on", fontSize: 13, automaticLayout: true }} />
      </div>
      </fieldset>
      <Errors errors={errors} />
      {status && <p role="status">{status}</p>}
      {!readOnly && <div className="row">
        <button type="submit" className="primary">{t("common.save")}</button>
        {m.id && <button type="button" id="measure-delete" className="danger" onClick={() => remove()}>{t("measure.delete")}</button>}
      </div>}
      {confirm && (
        <Confirm label={t("measure.confirmLabel")} message={confirm.message} ok={t("common.confirmDelete")} onConfirm={() => remove(confirm.token)}
          onCancel={() => setConfirm(null)} back="measure-delete" />
      )}
    </form>
  );
}
