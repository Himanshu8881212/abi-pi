import type { languages } from "monaco-editor/editor/editor.api";
import type { ReactNode } from "react";
import { focusSoon } from "./store";
import { t } from "./i18n";

// SE-3 MEL for Monaco: a Monarch tokenizer mirroring backend/abi/semantic/mel.py's grammar.
export const MEL: languages.IMonarchLanguage = {
  ignoreCase: true,
  keywords: ["SUM", "AVG", "MIN", "MAX", "COUNT", "DISTINCT_COUNT", "MEDIAN", "DIVIDE", "IF", "ROUND", "ABS", "WHERE", "IGNORE",
    "YTD", "QTD", "MTD", "PREVIOUS_YEAR", "PREVIOUS_PERIOD", "ROLLING", "CONVERT", "AND", "OR", "NOT", "IN", "IS", "NULL", "TRUE", "FALSE"],
  tokenizer: {
    root: [
      [/\[[^\]]*\]/, "type"], // [measure-id]
      [/'([^']|'')*'/, "string"],
      [/\d+(\.\d+)?/, "number"],
      [/"([^"]|"")+"/, "identifier"],
      [/[A-Za-z_]\w*/, { cases: { "@keywords": "keyword", "@default": "identifier" } }],
      [/[-+*/<>=(),.]/, "operator"],
    ],
  },
};

// UI-2 completion: each MEL function with its usage (mirrors backend mel.USAGE); CONVERT (SE-13) also explains itself.
const AGG = ["SUM", "AVG", "MIN", "MAX", "COUNT", "DISTINCT_COUNT", "MEDIAN"].map((f) => [f, `${f}(table.column)`]);
const TIME = ["YTD", "QTD", "MTD", "PREVIOUS_YEAR", "PREVIOUS_PERIOD"].map((f) => [f, `${f}([Measure])`]);
const USAGE: Record<string, string> = Object.fromEntries([...AGG, ...TIME, ["DIVIDE", "DIVIDE(numerator, denominator)"],
  ["CONVERT", "SUM(CONVERT(table.amount, table.currency, table.date))"], ["IF", "IF(condition, then, else)"], ["ROUND", "ROUND(value, digits)"],
  ["ABS", "ABS(value)"], ["WHERE", "WHERE([Measure], table.column = 'value')"], ["IGNORE", "IGNORE([Measure], table.column, ...)"],
  ["ROLLING", "ROLLING([Measure], 3, month)"]]);

// The suggestions as Monaco snippets (kind and insert rule are set by the caller, which owns the monaco import).
export const suggestions = () => Object.entries(USAGE).map(([f, usage]) => ({
  label: f, detail: usage, documentation: f === "CONVERT" ? t("mel.convertHelp") : undefined,
  insertText: f === "CONVERT" ? "CONVERT(${1:table.amount}, ${2:table.currency}, ${3:table.date})" : `${f}($0)`,
}));

export type ActionError = { file?: string; path?: string; error: string; fix?: string };

// Where a compile error goes in the text as [start, end) offsets: the syntax error's column, the end when the
// expression ends early, else the whole expression (reference and column errors carry no position).
export function errorSpan(e: ActionError, text: string): [number, number] {
  const col = /at column (\d+)/.exec(e.error);
  if (col) return [+col[1] - 1, +col[1]];
  if (e.error.includes("ends early")) return [Math.max(text.length - 1, 0), text.length];
  return [0, text.length];
}

// The action API's refusal as a list: PC-3 errors {file, path, error, fix}, pydantic's {loc, msg}, or just the message.
export function actionErrors(status: number, detail: unknown): ActionError[] {
  const d = detail as { message?: string; errors?: { file?: string; path?: string; error?: string; fix?: string; loc?: unknown[]; msg?: string }[] } | string | null;
  if (typeof d === "string") return [{ error: d }];
  const errs = (d?.errors ?? []).map((e) => (e.error ? (e as ActionError) : { path: e.loc?.join("."), error: e.msg ?? "" }));
  return errs.length ? errs : [{ error: d?.message ?? `HTTP ${status}` }];
}

// The refusal list under a form: one alert region, each error with its file, JSON path and suggested fix.
export function Errors({ errors }: { errors: ActionError[] }) {
  if (!errors.length) return null;
  return (
    <div role="alert">
      <ul className="errors">{errors.map((e, i) => (
        <li key={i}><span className="error">{[e.file, e.path].filter(Boolean).join(" ")}{(e.file || e.path) && ": "}{e.error}</span>{e.fix && <> {e.fix}</>}</li>
      ))}</ul>
    </div>
  );
}

// §5 #3 confirmation group: Confirm takes focus when it appears; Cancel closes it and hands focus back to `back`
// (WCAG 2.4.3). `children` (a preview) goes between the message and the buttons.
export function Confirm({ label, message, ok = t("common.confirm"), onConfirm, onCancel, back, children }: {
  label: string; message: string; ok?: string; onConfirm: () => void; onCancel: () => void; back: string; children?: ReactNode;
}) {
  return (
    <div className="confirm" role="group" aria-label={label}>
      <p>{message}</p>
      {children}
      <button type="button" autoFocus className="primary" onClick={onConfirm}>{ok}</button>
      <button type="button" onClick={() => { onCancel(); focusSoon(back); }}>{t("common.cancel")}</button>
    </div>
  );
}
