import { Background, Controls, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { lakeModelQuery, localeQuery, measuresQuery, modelQuery, rlsQuery, type MeasureSpec } from "./api";
import { t } from "./i18n";

type Side = { table: string; columns: string[] };
export type Model = {
  tables: Record<string, { kind: "fact" | "dimension"; key: string[] | null; description: string | null; hidden?: boolean }>;
  relationships: { id: string; from: Side; to: Side }[];
  date_table: { table: string; column: string } | null;
  columns: Record<string, Record<string, string>>; // loaded table -> column -> DuckDB type
};

const side = (s: Side) => `${s.table}.${s.columns.join(", ")}`;
const describe = (r: Model["relationships"][number]) => t("model.manyToOne", { from: side(r.from), to: side(r.to) });

/** Each table's measures: the tables a measure's MEL names (`videos.views`), through the measures it uses (`[likes]`). */
export function byTable(ms: MeasureSpec[], tables: string[]) {
  const by = new Map(ms.map((m) => [m.id, m]));
  const seen = new Map<string, Set<string>>();
  const of = (id: string): Set<string> => {
    if (seen.has(id)) return seen.get(id)!;
    const out = new Set<string>();
    seen.set(id, out); // a cycle ends here
    const e = by.get(id)?.expression ?? "";
    for (const tb of tables) if (new RegExp(`(^|[^\\w.])${tb.replace(/\W/g, "\\$&")}\\.\\w`).test(e)) out.add(tb);
    for (const [, ref] of e.matchAll(/\[([^\]]+)\]/g)) of(ref).forEach((x) => out.add(x));
    return out;
  };
  return Object.fromEntries(tables.map((tb) => [tb, ms.filter((m) => of(m.id).has(tb))]));
}

// D-74 star: facts of a relationship in the centre column, their dimensions alternately left and right of them, tables in
// no relationship (staging inputs) in a row underneath. ponytail: fixed columns, no layout library; add elk when
// snowflakes or many facts make the columns cross.
const W = 240, GAP = 120;
const height = (rows: number) => 64 + rows * 18;

/** SE-2 star layout; `ms` puts each table's measures on its card. Hidden staging tables are not drawn (MODEL-HIDE). */
export function layout(all: Model, ms: MeasureSpec[] = []): { nodes: Node[]; edges: Edge[] } {
  const m = { ...all, tables: Object.fromEntries(Object.entries(all.tables).filter(([, i]) => !i.hidden)) };
  const rels = m.relationships.filter((r) => r.from.table in m.tables && r.to.table in m.tables);
  const facts = Object.keys(m.tables).filter((x) => rels.some((r) => r.from.table === x) && !rels.some((r) => r.to.table === x));
  const linked = new Set(rels.flatMap((r) => [r.from.table, r.to.table]));
  const dims = Object.keys(m.tables).filter((x) => linked.has(x) && !facts.includes(x));
  if (!facts.length) facts.push(...dims.splice(0, dims.length ? 1 : 0)); // a ring of dimensions: one goes in the centre
  const loose = Object.keys(m.tables).filter((x) => !linked.has(x));
  const measures = byTable(ms, Object.keys(m.tables));
  const rows = (x: string) => (m.tables[x].key?.length ?? 0) + rels.filter((r) => r.from.table === x).length + measures[x].length;
  const pos: Record<string, { x: number; y: number }> = {};
  const stack = (names: string[], x: number) => names.reduce((y, n) => ((pos[n] = { x, y }), y + height(rows(n)) + 40), 0);
  const centre = stack(facts, 0);
  const [left, right] = [dims.filter((_, i) => i % 2), dims.filter((_, i) => !(i % 2))];
  const sideH = Math.max(stack(left, -(W + GAP)), stack(right, W + GAP), centre);
  [left, right, facts].forEach((col) => { // centre each column on the tallest
    const h = col.reduce((s, n) => s + height(rows(n)) + 40, 0);
    col.forEach((n) => (pos[n].y += (sideH - h) / 2));
  });
  loose.forEach((n, i) => (pos[n] = { x: (i - (loose.length - 1) / 2) * (W + 40), y: sideH + GAP }));

  const nodes = Object.entries(m.tables).map(([name, info]): Node => {
    const cols = Object.entries(m.columns[name] ?? {});
    const fks = new Set(rels.filter((r) => r.from.table === name).flatMap((r) => r.from.columns));
    const kind = t(info.kind === "fact" ? "model.fact" : "model.dimension") + (m.date_table?.table === name ? t("model.dateTable") : "")
      + (linked.has(name) ? "" : t("model.notRelated"));
    const shown = cols.filter(([c]) => info.key?.includes(c) || fks.has(c));
    return {
      id: name, type: "entity", position: pos[name],
      ariaLabel: t("model.nodeLabel", { table: name, kind, key: info.key?.join(", ") ?? t("model.none") }),
      className: linked.has(name) ? "entity" : "entity loose",
      data: { body: (
        <>
          <strong>{name}</strong> <span className="muted">{kind}</span>
          <ul>
            {shown.map(([c, type]) => (
              <li key={c}>{c} <span className="muted">{type}</span>{info.key?.includes(c) && t("model.key")}{fks.has(c) && t("model.fk")}</li>
            ))}
          </ul>
          <p className="muted">{t("model.columnCount", { n: cols.length })}</p>
          {measures[name].length > 0 && <ul className="measures">{measures[name].map((x) => <li key={x.id}>{x.name}</li>)}</ul>}
        </>
      ) },
    };
  });
  const edges = rels.map((r): Edge => {
    const right = pos[r.to.table].x >= pos[r.from.table].x;
    return { id: r.id, source: r.from.table, target: r.to.table, sourceHandle: right ? "r" : "l", targetHandle: right ? "l" : "r",
      label: "N : 1", ariaLabel: describe(r) };
  });
  return { nodes, edges };
}

// a table card with a handle on each side, so edges leave towards the dimension's side of the star
function Entity({ data }: NodeProps<Node<{ body: ReactNode }>>) {
  return (
    <>
      {(["l", "r"] as const).map((h) => <Handle key={`s${h}`} type="source" id={h} position={h === "l" ? Position.Left : Position.Right} isConnectable={false} />)}
      {(["l", "r"] as const).map((h) => <Handle key={`t${h}`} type="target" id={h} position={h === "l" ? Position.Left : Position.Right} isConnectable={false} />)}
      {data.body}
    </>
  );
}
const TYPES = { entity: Entity };

export type Glossary = { glossary: { id: string; term: string; definition: string; measures?: string[]; columns?: { table: string; column: string }[] }[] };

// UI-2 model tab (D-74): the semantic model as a star diagram (React Flow) and a side panel with what the diagram can't
// show: relationships as text, measures with their MEL, glossary terms, the date table and time zone, row-level rules.
export default function ModelDiagram({ project }: { project: string }) {
  const q = useQuery(lakeModelQuery(project));
  const ms = useQuery(measuresQuery(project));
  const g = useQuery({ ...modelQuery<Glossary>(project), select: (x: Glossary) => x.glossary });
  const loc = useQuery(localeQuery(project));
  const rls = useQuery(rlsQuery(project));
  if (q.error) return <p role="alert">{t("model.loadFailed", { project, error: q.error.message })}</p>;
  if (!q.data) return <p role="status">{t("model.loading", { project })}</p>;
  if (!Object.keys(q.data.tables).length) return <p className="muted empty">{t("model.empty")}</p>;
  const { nodes, edges } = layout(q.data, ms.data);
  const dt = q.data.date_table;
  const section = (id: string, title: string, items: ReactNode[] | undefined) => (
    <section aria-labelledby={id}>
      <h3 id={id}>{title}</h3>
      {items?.length ? <ul>{items}</ul> : <p className="muted">{t("model.noneYet")}</p>}
    </section>
  );
  return (<>
    <h2 className="screen-title">{t("model.title", { project })}</h2>{/* UI-DS.9 V2 screen title */}
    <div className="model">
      <div className="diagram" aria-label={t("model.diagram", { project })} role="region">
        <ReactFlow key={`${q.dataUpdatedAt}-${ms.dataUpdatedAt}`} nodeTypes={TYPES} defaultNodes={nodes} defaultEdges={edges} nodesConnectable={false} fitView>
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <aside className="rels" aria-label={t("model.panel", { project })} tabIndex={0} /* scrolls: keyboard users reach it (axe scrollable-region-focusable) */>
        {section("rels-h", t("model.relationships"), q.data.relationships.filter((r) => edges.some((e) => e.id === r.id)).map((r) => <li key={r.id}>{describe(r)}</li>))}
        {section("model-measures-h", t("model.measures"), ms.data?.map((x) => <li key={x.id}>{x.name} <code>{x.expression}</code></li>))}
        {section("model-glossary-h", t("model.glossary"), g.data?.map((x) => <li key={x.id}>{x.term}</li>))}
        <section aria-labelledby="model-time-h">
          <h3 id="model-time-h">{t("model.time")}</h3>
          <p>{dt ? t("model.dateTableIs", { table: dt.table, column: dt.column }) : t("model.noDateTable")}</p>
          {loc.data?.effective && <p>{t("model.timeZone", { zone: loc.data.effective.time_zone })}</p>}
        </section>
        {section("model-rls-h", t("model.rules"), rls.data?.rules.map((r, i) => <li key={i}>{`${r.table}.${r.column}`}</li>))}
        {section("model-hidden-h", t("model.hidden"), Object.keys(q.data.tables).filter((x) => q.data.tables[x].hidden).map((x) => <li key={x}>{x}</li>))}
      </aside>
    </div>
  </>);
}
