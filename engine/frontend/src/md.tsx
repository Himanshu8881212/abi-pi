import { Fragment, useRef, useState, type ReactNode } from "react";
import { t } from "./i18n";

// UI-1.3: the agent's Markdown as React elements (headings, lists, GFM tables, fenced and inline code, bold/italic).
// React escapes every text node, so HTML in an answer stays text; links show as "text (url)", never live (LLM text).
// ponytail: no nested lists, block quotes or images; add them when answers need them.
const INLINE = /`([^`]+)`|\*\*(.+?)\*\*|\*([^*\s][^*]*?)\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

function inline(s: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of s.matchAll(INLINE)) {
    if (m.index > last) out.push(s.slice(last, m.index));
    const k = m.index;
    if (m[1] !== undefined) out.push(<code key={k}>{m[1]}</code>);
    else if (m[2] !== undefined) out.push(<strong key={k}>{inline(m[2])}</strong>);
    else if (m[3] !== undefined) out.push(<em key={k}>{inline(m[3])}</em>);
    else out.push(`${m[4]} (${m[5]})`);
    last = k + m[0].length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

const cells = (l: string) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
// UI-DS-0: a column is numeric when its rule says `--:` or every non-empty cell is a number ("1,234", "-5.2%", "$3k")
const NUM = /^[-+−]?[$€£¥₹]?\d[\d,.\s]*\s?(%|[kKmMbB]n?)?$/;
const MAX = 12; // rows shown before "Show all N rows"

function Table({ head, rule, body }: { head: string[]; rule: string[]; body: string[][] }) {
  const [all, setAll] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const showAll = () => (setAll(true), setTimeout(() => scroll.current?.focus())); // the button goes, focus stays in the table
  const num = head.map((_, m) => rule[m]?.endsWith(":") && !rule[m].startsWith(":")
    || body.some((r) => r[m]) && body.every((r) => !r[m] || NUM.test(r[m].replace(/\*/g, ""))));
  const cls = (m: number) => (num[m] ? "num" : undefined);
  return (
    <div className="md-table">
      <div className="scroll" ref={scroll} tabIndex={0}><table>
        <thead><tr>{head.map((c, n) => <th key={n} scope="col" className={cls(n)}>{inline(c)}</th>)}</tr></thead>
        <tbody>{(all ? body : body.slice(0, MAX)).map((r, n) => <tr key={n}>{head.map((_, m) => <td key={m} className={cls(m)}>{inline(r[m] ?? "")}</td>)}</tr>)}</tbody>
      </table></div>
      {!all && body.length > MAX && <button type="button" className="sm ghost" onClick={showAll}>{t("md.showAll", { count: body.length })}</button>}
    </div>
  );
}

function Code({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  // a refused clipboard (no secure context) leaves the code selectable as before
  const copy = () => navigator.clipboard?.writeText(code).then(() => setCopied(true), () => {});
  return (
    <div className="md-code">
      <div className="bar"><span>{lang || t("md.code")}</span>
        <button type="button" className="sm ghost" onClick={copy}>{copied ? t("md.copied") : t("md.copy")}</button></div>
      <pre tabIndex={0}><code>{code}</code></pre>
    </div>
  );
}
const LIST = /^\s*([-*+]|\d+[.)])\s+/;
const RULE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const block = (l: string, next = "") => l.startsWith("```") || /^#{1,6}\s/.test(l) || LIST.test(l) || (l.includes("|") && RULE.test(next));

// UI-DS.6 (UI-DS.1 R1/R2/R6): an `abi-visual` fence holds a chat visual's spec: measure IDs, table.column dimensions,
// filters, a chart type, never a number. A spec that isn't exactly that is null (shown as a fixed text, never raw).
export type ChatSpec = { measures: string[]; dimensions?: string[]; filters?: { column: string; op: string; value: unknown }[]; type?: string };
const TYPES = ["kpi", "bar", "line", "pie", "table", "matrix", "scatter", "slicer", "map", "combo", "waterfall", "funnel", "heatmap", "treemap", "sankey"];
const OPS = ["=", "<>", "<", "<=", ">", ">=", "in"];
const strings = (x: unknown) => Array.isArray(x) && x.every((s) => typeof s === "string");
export function chatSpec(json: string): ChatSpec | null {
  let v: Record<string, unknown>;
  try {
    v = JSON.parse(json);
  } catch {
    return null;
  }
  const filter = (f: { column?: unknown; op?: unknown }) => !!f && typeof f === "object" && Object.keys(f).every((k) => ["column", "op", "value"].includes(k))
    && typeof f.column === "string" && OPS.includes(f.op as string);
  const ok = !!v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).every((k) => ["measures", "dimensions", "filters", "type"].includes(k))
    && strings(v.measures) && (v.measures as string[]).length > 0 && (v.dimensions === undefined || strings(v.dimensions))
    && (v.filters === undefined || (Array.isArray(v.filters) && (v.filters as { column?: unknown; op?: unknown }[]).every(filter))) && (v.type === undefined || TYPES.includes(v.type as string));
  return ok ? (v as ChatSpec) : null;
}
// the closed abi-visual blocks of an answer, in order (Save as report)
export const visualBlocks = (text: string) => [...text.replace(/\r\n?/g, "\n").matchAll(/^```abi-visual[ \t]*\n([\s\S]*?)\n```[ \t]*$/gm)].map((m) => m[1]);

export function Md({ text, visual }: { text: string; visual?: (json: string) => ReactNode }) {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  for (let i = 0; i < lines.length; ) {
    const l = lines[i];
    const k = i;
    const h = /^(#{1,6})\s+(.*)$/.exec(l);
    if (!l.trim()) i++;
    else if (l.startsWith("```")) {
      let j = i + 1;
      while (j < lines.length && !lines[j].startsWith("```")) j++;
      const lang = l.slice(3).trim();
      const code = lines.slice(i + 1, j).join("\n");
      // a visual exactly as the server cuts it out (reports.BLOCK); an unterminated one (a cut-off answer) shows nothing
      if (!/^```abi-visual[ \t]*$/.test(l)) out.push(<Code key={k} lang={lang} code={code} />);
      else if (/^```[ \t]*$/.test(lines[j] ?? "") && visual) out.push(<Fragment key={k}>{visual(code)}</Fragment>);
      i = j + 1;
    } else if (h) {
      const H = `h${Math.min(6, h[1].length + 2)}` as "h3"; // the chat panel's own heading is an h2
      out.push(<H key={k}>{inline(h[2])}</H>);
      i++;
    } else if (l.includes("|") && RULE.test(lines[i + 1] ?? "")) {
      const [head, rule] = [cells(l), cells(lines[i + 1])];
      const body: string[][] = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) body.push(cells(lines[i]));
      out.push(<Table key={k} head={head} rule={rule} body={body} />);
    } else if (LIST.test(l)) {
      const ordered = /^\s*\d/.test(l);
      const items: string[] = [];
      for (; i < lines.length && LIST.test(lines[i]) && /^\s*\d/.test(lines[i]) === ordered; i++) items.push(lines[i].replace(LIST, ""));
      const L = ordered ? "ol" : "ul";
      out.push(<L key={k}>{items.map((x, n) => <li key={n}>{inline(x)}</li>)}</L>);
    } else {
      const para = [l];
      for (i++; i < lines.length && lines[i].trim() && !block(lines[i], lines[i + 1]); i++) para.push(lines[i]);
      out.push(<p key={k}>{para.map((x, n) => <Fragment key={n}>{n > 0 && <br />}{inline(x)}</Fragment>)}</p>);
    }
  }
  return <>{out}</>;
}
