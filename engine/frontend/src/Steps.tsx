import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import schema from "../../schemas/v1/table.json";
import { api, canDesign, meQuery, post, type Page } from "./api";
import { ReadOnly } from "./Production";
import { focusSoon } from "./store";
import TableGrid from "./TableGrid";
import { t } from "./i18n";

// TR-1 step types and their parameters come from the published schema (M-01/M-04), so the editor never drifts from it.
type Def = { required: string[]; properties: Record<string, { type?: string; enum?: unknown[] }> };
const defs = schema.$defs as unknown as Record<string, Def>;
const mapping = schema.properties.steps.items.discriminator.mapping as Record<string, string>;
export const TYPES = Object.keys(mapping).filter((t) => t !== "source"); // exactly one source step, always first
const EMPTY: Record<string, unknown> = { string: "", array: [], object: {} };

/** A new step of `type` with every required parameter present and empty, for the user to fill in. */
export function template(type: string): Record<string, unknown> {
  const d = defs[mapping[type].split("/").pop()!];
  return Object.fromEntries(d.required.map((k) => [k, k === "type" ? type : (d.properties[k].enum?.[0] ?? EMPTY[d.properties[k].type!])]));
}

type Item = { id: number; text: string };
let ids = 0;
const item = (step: unknown): Item => ({ id: ids++, text: JSON.stringify(step, null, 2) });
const parse = (text: string) => {
  try {
    const s = JSON.parse(text);
    return s && typeof s === "object" && !Array.isArray(s) ? (s as { type?: unknown }) : null;
  } catch {
    return null;
  }
};

// ponytail: each step is edited as JSON in a textarea; typed per-step forms if users struggle with JSON
// UI-2 table tab: the table's steps (TR-1) beside its grid. Editing a step or choosing one previews the first
// 1,000 rows after it (TR-3, `preview_steps`); Save stores and reloads the table (`set_steps`).
export default function TableTab({ table }: { table: string /* "project/table" */ }) {
  const [project, name] = [table.slice(0, table.indexOf("/")), table.slice(table.indexOf("/") + 1)];
  const [items, setItems] = useState<Item[] | null>(null);
  const [sel, setSel] = useState(0); // preview after this step
  const [editing, setEditing] = useState(false); // false: the grid shows the saved table
  const [preview, setPreview] = useState<Page | null>(null);
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const [adding, setAdding] = useState("filter");
  const [saved, reload] = useState(0);
  const readOnly = !canDesign(useQuery(meQuery(project)).data); // D-56: steps are design

  useEffect(() => {
    api<{ steps: unknown[] }>(`/api/projects/${encodeURIComponent(project)}/tables/${encodeURIComponent(name)}/steps`).then(
      (r) => {
        setItems(r.steps.map(item));
        setSel(r.steps.length - 1);
      },
      (e: Error) => setAlert(e.message),
    );
  }, [project, name]);

  const steps = items?.map((i) => parse(i.text)) ?? [];
  const valid = steps.every(Boolean);
  const body = JSON.stringify({ project, steps, step: sel });

  useEffect(() => {
    if (!editing || !valid) return;
    let live = true; // a slower, older preview must not overwrite a newer one
    setStatus(t("steps.updating"));
    const timer = setTimeout(
      () =>
        api<Omit<Page, "total">>("/api/actions/preview_steps", { method: "POST", body }).then(
          (p) => live && (setPreview({ ...p, total: p.rows.length }), setStatus(t("steps.previewAfter", { step: sel + 1, rows: p.rows.length }))),
          (e: Error) => live && setStatus(t("steps.previewFailed", { error: e.message })),
        ),
      300,
    );
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [editing, valid, body]);

  if (!items) return alert ? <p role="alert">{t("steps.loadFailed", { table, error: alert })}</p> : <p role="status">{t("steps.loading", { table })}</p>;
  if (!items.length) // WB-2 input table: no source and no steps, its rows are entered in the grid
    return (
      <div className="table-tab">
        <section className="steps" aria-labelledby="steps-h">
          <h2 id="steps-h">{t("steps.inputTable")}</h2>
          <p className="muted">{t("steps.inputHelp")}</p>
        </section>
        <div className="grid"><div className="grid-body"><TableGrid table={table} preview={null} /></div></div>
      </div>
    );

  const edit = (next: Item[], nextSel = sel) => {
    setItems(next);
    setSel(nextSel);
    setEditing(true);
    setAlert("");
  };
  const remove = (i: number) => {
    const next = items.filter((_, j) => j !== i);
    edit(next, sel >= i ? sel - 1 : sel);
    focusSoon(`step-${(next[i] ?? next[i - 1]).id}`); // WCAG 2.4.3: the removed step's successor
  };
  const add = () => {
    const it = item(template(adding));
    edit([...items.slice(0, sel + 1), it, ...items.slice(sel + 1)], sel + 1);
    focusSoon(`step-${it.id}`);
  };
  const save = async () => {
    try {
      await post("set_steps", { project, table: name, steps });
      setEditing(false);
      setPreview(null);
      reload((n) => n + 1);
      setStatus(t("steps.saved"));
    } catch (e) {
      setAlert(t("steps.saveFailed", { error: (e as Error).message }));
    }
  };

  return (
    <div className="table-tab">
      <section className="steps" aria-labelledby="steps-h">
        <h2 id="steps-h">{t("steps.title")}</h2>
        {readOnly && <ReadOnly project={project} why={t("steps.readOnly", { project })} />}
        <ol>
          {items.map((it, i) => (
            <li key={it.id} className={i === sel ? "sel" : undefined}>
              <label htmlFor={`step-${it.id}`}>{i + 1}. {String(steps[i]?.type ?? t("steps.invalid"))}</label>
              <textarea id={`step-${it.id}`} value={it.text} spellCheck={false} rows={Math.max(3, it.text.split("\n").length)} readOnly={readOnly}
                aria-invalid={!steps[i]} aria-describedby={steps[i] ? undefined : `step-${it.id}-err`}
                onChange={(e) => edit(items.map((x) => (x.id === it.id ? { ...x, text: e.target.value } : x)))} />
              {!steps[i] && <p id={`step-${it.id}-err`} className="error">{t("steps.notJson")}</p>}
              {!readOnly && <div className="row">
                <button aria-pressed={editing && i === sel} onClick={() => edit(items, i)}>{t("steps.preview", { step: i + 1 })}</button>
                {i > 0 && <button onClick={() => remove(i)}>{t("steps.remove", { step: i + 1 })}</button>}
              </div>}
            </li>
          ))}
        </ol>
        {!readOnly && <>
        <div className="row">
          <label>{t("steps.new")} <select value={adding} onChange={(e) => setAdding(e.target.value)}>{TYPES.map((ty) => <option key={ty}>{ty}</option>)}</select></label>
          <button onClick={add}>{t("steps.addAfter", { step: sel + 1 })}</button>
        </div>
        <button className="primary" onClick={save} disabled={!valid}>{t("steps.save")}</button>
        </>}
        {alert && <p role="alert" className="error">{alert}</p>}
      </section>
      <div className="grid">
        <p role="status" className="muted caption">{status || t("steps.savedTable")}</p>
        <div className="grid-body"><TableGrid key={saved} table={table} preview={editing ? preview : null} /></div>
      </div>
    </div>
  );
}
