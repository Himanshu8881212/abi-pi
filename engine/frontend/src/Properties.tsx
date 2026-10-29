import { useState, type FormEvent } from "react";
import type { Cell, Spec } from "./Report";
import { NUMERIC } from "./api";
import { t } from "./i18n";
import { focusSoon } from "./store";

export type SemanticModel = { measures: { id: string; name: string }[]; columns: Record<string, Record<string, string>> };
type Fmt = { style?: string; decimals?: number; compact?: boolean };
type Draft = {
  type: string; title: string; text: string; measures: string[]; dimensions: string[];
  filters: { column: string; op: string; value: string }[]; format: Record<string, Fmt>; layout: Cell;
};
type Query = { measures?: string[]; dimensions?: string[]; filters?: { column: string; op: string; value: unknown }[]; [k: string]: unknown };

// names are `visual.<type>` in the locale files (`visual.auto` for "")
const TYPES = ["", "kpi", "bar", "line", "pie", "table", "matrix", "scatter", "slicer", "map", "text", "combo", "waterfall", "funnel", "heatmap", "treemap", "sankey"]; // RP-8
const OPS = ["=", "<>", "<", "<=", ">", ">=", "in"];

// A filter value typed in a text box → the JSON value of the spec: a number or boolean for such columns, a list for `in`.
export function parseValue(text: string, op: string, dtype = ""): unknown {
  const one = (s: string) => {
    s = s.trim();
    if (/BOOL/i.test(dtype)) return s.toLowerCase() === "true";
    return NUMERIC.test(dtype) && s !== "" && !isNaN(Number(s)) ? Number(s) : s;
  };
  return op === "in" ? text.split(",").map(one) : one(text);
}

export const draftOf = (v: Spec, layout: Cell): Draft => {
  const q = (v.query ?? {}) as Query;
  return {
    type: v.type ?? "", title: v.title ?? "", text: (v.text as string) ?? "", measures: q.measures ?? [], dimensions: q.dimensions ?? [],
    filters: (q.filters ?? []).map((f) => ({ column: f.column, op: f.op, value: [f.value].flat().join(", ") })),
    format: (v.format as Record<string, Fmt>) ?? {}, layout,
  };
};

// The edited visual as a report.json visual; keys the panel doesn't edit (order_by, limit, totals) are kept.
export function specOf(d: Draft, old: Spec, dtype: (column: string) => string | undefined): Spec {
  const { query, text, format, type, title, ...rest } = old;
  const x = Math.min(Math.max(d.layout.x, 0), 11);
  const v: Spec = { ...rest, layout: { x, y: Math.max(d.layout.y, 0), w: Math.min(Math.max(d.layout.w, 1), 12 - x), h: Math.max(d.layout.h, 1) } };
  if (d.type) v.type = d.type;
  if (d.title.trim()) v.title = d.title.trim();
  if (d.type === "text") return { ...v, text: d.text };
  const q: Query = { ...(query as Query), measures: d.measures, dimensions: d.dimensions,
    filters: d.filters.map((f) => ({ column: f.column, op: f.op, value: parseValue(f.value, f.op, dtype(f.column)) })) };
  if (!d.dimensions.length) delete q.dimensions;
  if (!d.filters.length) delete q.filters;
  v.query = q;
  const fmt = Object.fromEntries(d.measures.filter((m) => d.format[m] && Object.keys(d.format[m]).length).map((m) => [m, d.format[m]]));
  if (Object.keys(fmt).length) v.format = fmt;
  return v;
}

type Props = {
  spec: Spec; layout: Cell; model?: SemanticModel; readOnly: boolean;
  onSave: (v: Spec) => Promise<void>; onDelete: () => Promise<void>; onClose: () => void;
};

// UI-3 properties panel at the right edge of the canvas: edits the selected visual; every save goes through save_report.
export function Properties({ spec, layout, model, readOnly, onSave, onDelete, onClose }: Props) {
  const [d, setD] = useState(() => draftOf(spec, layout));
  const [error, setError] = useState("");
  const set = (p: Partial<Draft>) => setD((s) => ({ ...s, ...p }));
  const cols = Object.entries(model?.columns ?? {}).flatMap(([t, cs]) => Object.keys(cs).map((c) => `${t}.${c}`));
  const dtype = (c: string) => model?.columns[c.split(".")[0]]?.[c.slice(c.indexOf(".") + 1)];
  const flip = (list: string[], x: string, on: boolean) => (on ? [...list, x] : list.filter((y) => y !== x));
  const run = async (f: () => Promise<void>) => {
    try {
      await f();
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    run(() => onSave(specOf(d, spec, dtype)));
  };
  const setFilter = (i: number, p: Partial<Draft["filters"][number]>) => set({ filters: d.filters.map((f, j) => (j === i ? { ...f, ...p } : f)) });
  const setFmt = (m: string, p: Fmt) => {
    const f: Fmt = { ...d.format[m], ...p };
    for (const k of Object.keys(f) as (keyof Fmt)[]) if (f[k] === undefined || f[k] === false) delete f[k];
    set({ format: { ...d.format, [m]: f } });
  };
  return (
    <aside className="props" aria-labelledby="props-h">
      <h3 id="props-h" tabIndex={-1}>{t("props.heading", { title: spec.title ?? spec.id })}</h3>
      {readOnly && <p className="muted">{t("props.readOnly")}</p>}
      <form onSubmit={submit}>
        <fieldset disabled={readOnly} className="plain">
          <legend className="sr-only">{t("props.visual", { id: spec.id })}</legend>
          <label>{t("props.type")} <select value={d.type} onChange={(e) => set({ type: e.target.value })}>
            {TYPES.map((v) => <option key={v} value={v}>{t(`visual.${v || "auto"}`)}</option>)}
          </select></label>
          <label>{t("props.title")} <input value={d.title} placeholder={t("props.automatic")} onChange={(e) => set({ title: e.target.value })} /></label>
          {d.type === "text" ? (
            <label>{t("props.text")} <textarea value={d.text} onChange={(e) => set({ text: e.target.value })} /></label>
          ) : (
            <>
              <fieldset>
                <legend>{t("report.measures")}</legend>
                {model?.measures.map((m) => (
                  <label key={m.id}><input type="checkbox" checked={d.measures.includes(m.id)} onChange={(e) => set({ measures: flip(d.measures, m.id, e.target.checked) })} /> {m.name}</label>
                ))}
              </fieldset>
              <fieldset>
                <legend>{t("props.dimensions")}</legend>
                {cols.map((c) => (
                  <label key={c}><input type="checkbox" checked={d.dimensions.includes(c)} onChange={(e) => set({ dimensions: flip(d.dimensions, c, e.target.checked) })} /> {c}</label>
                ))}
              </fieldset>
              <fieldset>
                <legend>{t("props.filters")}</legend>
                {d.filters.map((f, i) => (
                  <div key={i} className="row">
                    <select aria-label={t("props.filterColumn", { n: i + 1 })} value={f.column} onChange={(e) => setFilter(i, { column: e.target.value })}>
                      {[...new Set([f.column, ...cols])].map((c) => <option key={c}>{c}</option>)}
                    </select>
                    <select aria-label={t("props.filterOperator", { n: i + 1 })} value={f.op} onChange={(e) => setFilter(i, { op: e.target.value })}>
                      {OPS.map((o) => <option key={o}>{o}</option>)}
                    </select>
                    <input aria-label={f.op === "in" ? t("props.filterValues", { n: i + 1 }) : t("props.filterValue", { n: i + 1 })} value={f.value} onChange={(e) => setFilter(i, { value: e.target.value })} />
                    <button type="button" className="sm" onClick={() => (set({ filters: d.filters.filter((_, j) => j !== i) }), focusSoon("add-filter"))}>{t("props.removeFilter", { n: i + 1 })}</button>
                  </div>
                ))}
                <button type="button" id="add-filter" className="sm" disabled={!cols.length} onClick={() => set({ filters: [...d.filters, { column: cols[0], op: "=", value: "" }] })}>{t("props.addFilter")}</button>
              </fieldset>
              {d.measures.length > 0 && (
                <fieldset>
                  <legend>{t("props.numberFormat")}</legend>
                  {d.measures.map((m) => {
                    const f = d.format[m] ?? {};
                    const n = model?.measures.find((x) => x.id === m)?.name ?? m;
                    return (
                      <div key={m} className="row">
                        <label>{n} <select value={f.style ?? ""} onChange={(e) => setFmt(m, { style: e.target.value || undefined })}>
                          <option value="">{t("props.automatic")}</option><option value="number">{t("props.number")}</option>
                          <option value="integer">{t("props.integer")}</option><option value="percent">{t("props.percent")}</option>
                        </select></label>
                        <label>{t("props.decimals")} <input type="number" min={0} max={6} className="num" value={f.decimals ?? ""} onChange={(e) => setFmt(m, { decimals: e.target.value === "" ? undefined : Number(e.target.value) })} /></label>
                        <label><input type="checkbox" checked={!!f.compact} onChange={(e) => setFmt(m, { compact: e.target.checked })} /> {t("props.compact")}</label>
                      </div>
                    );
                  })}
                </fieldset>
              )}
            </>
          )}
          <fieldset>
            <legend>{t("props.layout")}</legend>
            <div className="row">
              {(["x", "y", "w", "h"] as const).map((k) => (
                <label key={k}>{{ x: t("props.column"), y: t("props.row"), w: t("props.width"), h: t("props.height") }[k]} <input type="number" className="num" required
                  min={k === "w" || k === "h" ? 1 : 0} max={k === "x" ? 11 : k === "w" ? 12 : undefined} value={d.layout[k]}
                  onChange={(e) => set({ layout: { ...d.layout, [k]: e.target.valueAsNumber || 0 } })} /></label>
              ))}
            </div>
          </fieldset>
        </fieldset>
        {error && <p role="alert" className="error">{t("props.saveFailed", { error })}</p>}
        <div className="row">
          {!readOnly && <button type="submit">{t("common.save")}</button>}
          {!readOnly && <button type="button" onClick={() => run(onDelete)}>{t("props.delete")}</button>}
          <button type="button" onClick={onClose}>{t("common.close")}</button>
        </div>
      </form>
    </aside>
  );
}
