import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart, CandlestickChart, FunnelChart, HeatmapChart, LineChart, MapChart, PieChart, SankeyChart, ScatterChart, TreemapChart } from "echarts/charts";
import { AriaComponent, GeoComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapContinuousComponent } from "echarts/components";
import { init, registerMap, use } from "echarts/core";
import { SVGRenderer } from "echarts/renderers";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import ReactGridLayout, { useContainerWidth, type Layout } from "react-grid-layout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
import { call, canDesign, canEdit, canShare, download, meQuery, modelQuery, post, reportQuery, reportsQuery } from "./api";
import { cellEdit, cellMode, chartOption, dims, format, interactionFor, label, mapMatch, member, picked, pivot, slice, toggle, TOTAL, totalEdit, touches, type Filter, type Fmt, type Value, type VisualData, type World } from "./charts";
import { Explanation, readSse, type How } from "./Chat";
import { Properties, type SemanticModel } from "./Properties";
import { EmbedDialog } from "./Embed";
import { ShareDialog } from "./Share";
import { changed, locale } from "./Locale";
import { SharingDialog, SubscribeDialog } from "./Sharing";
import { focusSoon, useTheme, useUi } from "./store";
import { fmtDate, fmtNumber, t, useLang } from "./i18n";
import { chatSpec } from "./md";

use([BarChart, LineChart, MapChart, PieChart, ScatterChart, CandlestickChart, FunnelChart, HeatmapChart, SankeyChart, TreemapChart, AriaComponent, GeoComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapContinuousComponent, SVGRenderer]);

// RP-1 map shapes: a separate chunk from the app's own origin, loaded on the first map visual (never the internet)
export const worldQuery = {
  queryKey: ["world"], staleTime: Infinity,
  queryFn: async () => {
    const w = (await import("./world.json")).default as unknown as World;
    registerMap("world", w as never);
    return w;
  },
};

export type Cell = { x: number; y: number; w: number; h: number };
export type Spec = { id: string; type?: string; title?: string; layout?: Cell; [k: string]: unknown };
type PageSpec = { id: string; name: string; visuals: Spec[]; [k: string]: unknown };
export type ReportSpec = { id: string; name: string; pages: PageSpec[]; [k: string]: unknown };

// UI-2 report canvas tab (RP-1..RP-5): page tabs, each page a 12-column snap grid of visuals.
export default function Report({ project, id }: { project: string; id: string }) {
  const me = useQuery(meQuery(project));
  // SH-1: a recipient (no role here) may read only this report, so it never asks for the project's list
  const q = useQuery({ ...(me.data?.role === null ? reportQuery(project, id) : reportsQuery(project)), enabled: !!me.data });
  const [page, setPage] = useState<string | null>(null);
  const [failed, setFailed] = useState("");
  const [editing, setEditing] = useState(false); // UI-DS.6 (D-120): the canvas editor is "Edit layout", off when it opens
  const qc = useQueryClient();
  const r = q.data?.find((x) => x.id === id);
  const name = useUi((s) => s.name);
  useEffect(() => void (r && name(`${project}/#report/${id}`, r.name)), [r?.name]); // UI-2: the workspace tab shows the name
  const error = me.error ?? q.error;
  if (error) return <p role="alert">{t("report.loadFailed", { project, error: error.message })}</p>;
  if (!q.data) return <p role="status">{t("report.loading", { id })}</p>;
  if (!r) return <p role="alert">{t("report.none", { id, project })}</p>;
  // RP-5/SE-13: Editors may move the per-visual caption and "How is this calculated?" into each visual's header
  const details = async (show: boolean) => {
    try {
      await post("save_report", { project, report: { ...r, hide_details: !show } });
      setFailed("");
      await qc.invalidateQueries({ queryKey: ["reports"] });
    } catch (e) {
      setFailed(t("report.detailsFailed", { error: (e as Error).message }));
    }
  };
  const p = r.pages.find((x) => x.id === page) ?? r.pages[0];
  // UI-DS.6: live data again (every visual and explanation of this project re-read through visual_data)
  const refresh = () => Promise.all(["visual", "how"].map((k) => qc.invalidateQueries({ queryKey: [k],
    predicate: (x) => (x.queryKey[1] as { project?: string } | null)?.project === project })));
  return (
    <div className="report">
      {/* UI-DS.10 (design/v2-chat-first screen head): title and description, then the report's actions, Ask to change first */}
      <div className="report-head">
        <div>
          <h2 className="report-name">{r.name}</h2>
          {!!r.description && <p className="muted">{String(r.description)}</p>}
        </div>
        <div className="row">
          {me.data?.role != null && <button type="button" id="report-ask" className="sm primary" onClick={() => (useUi.setState({ ask: true }), focusSoon("ask-input"))}>{t("report.askChange")}</button>}
          <button type="button" id="report-refresh" className="sm" onClick={refresh}>{t("report.refresh")}</button>
          {me.data?.role != null && <SubscribeDialog project={project} report={r} />}
          {canShare(me.data) && <ShareDialog project={project} report={r} />}
          {canShare(me.data) && <SharingDialog project={project} kind="report" id={r.id} name={r.name} />}
          {me.data?.role === "owner" && <EmbedDialog project={project} report={r} />}
          {canDesign(me.data) && <button type="button" id="edit-layout" className="sm" aria-pressed={editing} onClick={() => setEditing(!editing)}>{t("report.editLayout")}</button>}
        </div>
      </div>
      <div className="bar">
        <div role="tablist" aria-label={t("report.pagesOf", { name: r.name })}>
          {r.pages.map((x) => (
            <button key={x.id} role="tab" id={`page-${x.id}`} aria-selected={x.id === p.id} aria-controls="report-page" onClick={() => setPage(x.id)}>{x.name}</button>
          ))}
        </div>
        {canDesign(me.data) && (
          <label className="details"><input type="checkbox" checked={!r.hide_details} onChange={(e) => details(e.target.checked)} />{` ${t("report.showDetails")}`}</label>
        )}
        {failed && <p role="alert" className="error">{failed}</p>}
      </div>
      <div role="tabpanel" id="report-page" aria-labelledby={`page-${p.id}`} className="canvas">
        <Page key={p.id} project={project} report={r} page={p} editing={editing} />
      </div>
    </div>
  );
}

// RP-7: while `on`, one stream of live-table changes for the project, read with fetch and the Bearer header (EventSource
// can't send it; a token in the URL would be logged). It reconnects by hand after an error or an end; 401/403 end it.
function useLive(project: string, on: boolean, onChange: (tables: string[]) => void) {
  const cb = useRef(onChange);
  cb.current = onChange;
  useEffect(() => {
    if (!on) return;
    const stop = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const open = async () => {
      let status = 0;
      try {
        const r = await call(`/api/projects/${encodeURIComponent(project)}/live`, { signal: stop.signal });
        status = r.status;
        if (r.ok && r.body) await readSse(r.body, (event, data) => event === "changed" && cb.current(data.tables));
      } catch { /* a network error or the abort below; retried unless aborted */ }
      if (stop.signal.aborted || status === 401 || status === 403) return;
      timer = setTimeout(open, status === 503 ? 30_000 : 3_000); // 503: too many open streams, try later
    };
    open();
    return () => (stop.abort(), clearTimeout(timer));
  }, [project, on]);
}

const cells = (p: PageSpec): Layout => p.visuals.map((v, i) => ({ i: v.id, ...(v.layout ?? { x: 0, y: 1000 + i, w: 6, h: 4 }) }));
// UI-DS.4 phone (WCAG 1.4.10): under 640 px the visuals stack full width in reading order (top to bottom, left to right)
// ponytail: read-only stack on phones; layout edits need a wider screen
export const stacked = (l: Layout): Layout => {
  let y = 0;
  return [...l].sort((a, b) => a.y - b.y || a.x - b.x).map((c) => ({ ...c, x: 0, w: 12, y: (y += c.h) - c.h }));
};

function Page({ project, report, page, editing }: { project: string; report: ReportSpec; page: PageSpec; editing: boolean }) {
  const [sel, setSel] = useState<Record<string, Filter>>({}); // RP-3 selections per visual; sent as `interaction` filters
  const layout = useMemo(() => cells(page), [page]); // the saved layout; RGL keeps the in-drag one
  const [grid, setGrid] = useState(0); // remounts the grid, dropping an unsaved drag
  const [alert, setAlert] = useState("");
  const [current, setCurrent] = useState<string | null>(null); // UI-3: the visual shown in the properties panel
  const { width, containerRef, mounted } = useContainerWidth();
  const narrow = width < 640; // UI-DS.4: stacked()
  const qc = useQueryClient();
  // SC-2: only Editors and Owners change the report; everyone else (and anyone while the role loads) sees it read-only.
  const me = useQuery(meQuery(project));
  const readOnly = !canDesign(me.data) || !editing;
  useEffect(() => void (readOnly && setCurrent(null)), [readOnly]); // leaving Edit layout drops the picked visual
  const member = me.data?.role != null; // SH-1: a recipient gets no model (403) and no PDF snapshot (not granted)
  const model = useQuery({ ...modelQuery<SemanticModel>(project), enabled: member });
  // RP-7: the stream is open only while a visual on this page reads a live table; a change re-fetches just those visuals
  const [live, setLive] = useState<Record<string, boolean>>({});
  useLive(project, member && Object.values(live).some(Boolean), (tables) => qc.invalidateQueries({ queryKey: ["visual"],
    predicate: (q) => (q.queryKey[1] as { project: string }).project === project && touches(q.state.data as VisualData, tables) }));
  // Quiet notification (§5): a polite announcement, at most one per visual per 30 s; focus never moves.
  const [said, setSaid] = useState("");
  const saidAt = useRef<Record<string, number>>({});
  const announce = (id: string, text: string) => {
    if (Date.now() - (saidAt.current[id] ?? 0) < 30_000) return;
    saidAt.current[id] = Date.now();
    setSaid(text);
  };

  // Every change to the page is saved as the whole report through the action API (principle 1, Editor only).
  const saveVisuals = async (visuals: Spec[]) => {
    await post("save_report", { project, report: { ...report, pages: report.pages.map((p) => (p.id === page.id ? { ...p, visuals } : p)) } });
    await changed(qc, ["reports"]);
  };
  // RP-4: drag/resize snaps to cells.
  const save = async (l: Layout) => {
    const at = new Map(l.map((c) => [c.i, { x: c.x, y: c.y, w: c.w, h: c.h }]));
    const visuals = page.visuals.map((v) => ({ ...v, layout: at.get(v.id)! }));
    if (JSON.stringify(visuals) === JSON.stringify(page.visuals)) return;
    try {
      await saveVisuals(visuals);
      setAlert("");
    } catch (e) {
      setAlert(t("report.layoutFailed", { error: (e as Error).message }));
      setGrid((g) => g + 1);
    }
  };
  const focusPanel = useRef(false);
  const open = (id: string) => {
    setCurrent(id);
    focusPanel.current = true;
  };
  const add = async () => {
    let n = 1;
    while (page.visuals.some((v) => v.id === `visual-${n}`)) n++;
    const y = Math.max(0, ...layout.map((c) => c.y + c.h));
    const m = model.data?.measures[0];
    const v: Spec = m ? { id: `visual-${n}`, query: { measures: [m.id] }, layout: { x: 0, y, w: 6, h: 4 } } : { id: `visual-${n}`, type: "text", text: t("report.newText"), layout: { x: 0, y, w: 6, h: 2 } };
    try {
      await saveVisuals([...page.visuals, v]);
      setAlert("");
      open(v.id);
    } catch (e) {
      setAlert(t("report.addFailed", { error: (e as Error).message }));
    }
  };
  const spec = page.visuals.find((v) => v.id === current);
  const cell = layout.find((c) => c.i === current);
  useEffect(() => { // Properties button or Add visual: focus the panel once it has rendered (a new visual needs the refetch first)
    if (!focusPanel.current || !spec) return;
    focusPanel.current = false;
    document.getElementById("props-h")?.focus();
  });
  // RP-6 exports: the server writes the file (from visual_data, or the print view for PDF); the status line says how it went
  const [exported, setExported] = useState("");
  const exportAs = async (what: string, link: () => Promise<string>) => {
    setExported(t("report.exporting", { what }));
    try {
      await download(await link());
      setExported(t("report.exported", { what }));
    } catch (e) {
      setExported(t("report.exportFailed", { what, error: (e as Error).message }));
    }
  };
  // MULTI-EXPORT: the picked pages in report order as ONE file; the page's current selections filter every page (X-05.5)
  const exportPages = (pages: PageSpec[], format: Format) => exportAs(pages.length === 1
    ? t("report.pageAs", { page: pages[0].name, format: formatName(format) }) : t("report.pagesAs", { pages: pages.map((p) => p.name).join(", "), format: formatName(format) }),
  async () => "/api/artifacts/" + (await post<{ snapshot: string }>("report_snapshot",
    { project, report: report.id, pages: pages.map((p) => p.id), format, filters: Object.values(sel) })).snapshot.replace(/^artifact:/, ""));
  const active = Object.entries(sel);
  const clear = () => {
    setSel({});
    focusSoon(`page-${page.id}`); // WCAG 2.4.3: the Clear button unmounts
  };

  return (
    <>
      <div className="row caption">
        <p role="status" className="muted">
          {active.length ? t("report.filteredBy", { filters: active.map(([, f]) => `${f.column} ${f.op} ${[f.value].flat().map((x) => format(x as Value)).join(", ")}`).join("; ") }) : t("report.selectHint")}
        </p>
        {active.length > 0 && <button className="sm" onClick={clear}>{t("report.clearSelections")}</button>}
        {!readOnly && <button id="add-visual" className="sm" onClick={add}>{t("report.addVisual")}</button>}
        {member && <ExportDialog report={report} onExport={exportPages} />}
        <span role="status" className="muted">{exported}</span>
      </div>
      {alert && <p role="alert" className="error caption">{alert}</p>}
      <p className="sr-only" aria-live="polite">{said}</p>
      <div className="page-row">
        <div ref={containerRef} className="page">
          {mounted && (
            <ReactGridLayout key={`${grid}${narrow}`} width={width} layout={narrow ? stacked(layout) : layout} gridConfig={{ cols: 12, rowHeight: 60 }}
              dragConfig={{ handle: ".drag", enabled: !readOnly && !narrow }} resizeConfig={{ enabled: !readOnly && !narrow }}
              onDragStop={narrow ? undefined : save} onResizeStop={narrow ? undefined : save}>
              {page.visuals.map((v) => (
                <div key={v.id} className={v.id === current ? "visual picked" : "visual"} onClick={() => !readOnly && setCurrent(v.id)}>
                  <Visual project={project} report={report.id} page={page.id} spec={v} readOnly={readOnly} writeBack={canEdit(me.data?.role)} terse={!!report.hide_details} filters={v.type === "text" ? [] : interactionFor(sel, v.id)} own={sel[v.id]}
                    onProps={() => open(v.id)} onExport={exportAs}
                    onLive={(on) => setLive((s) => (!!s[v.id] === on ? s : { ...s, [v.id]: on }))}
                    onUpdated={(text) => announce(v.id, text)}
                    onPick={(column, value) => setSel((s) => toggle(s, v.id, column, value))}
                    onSlice={(column, values) => setSel((s) => slice(s, v.id, column, values))} />
                </div>
              ))}
            </ReactGridLayout>
          )}
        </div>
        {spec && cell && !readOnly && ( /* UI-DS.6: leaving Edit layout closes the panel */
          <Properties key={JSON.stringify(spec)} spec={spec} layout={cell}
            model={model.data} readOnly={readOnly}
            onSave={(nv) => saveVisuals(page.visuals.map((v) => (v.id === spec.id ? nv : v)))}
            onDelete={async () => {
              await saveVisuals(page.visuals.filter((v) => v.id !== spec.id));
              setCurrent(null);
              focusSoon("add-visual"); // WCAG 2.4.3: the panel and the visual unmount
            }}
            onClose={() => {
              setCurrent(null);
              focusSoon(`props-${spec.id}`);
            }} />
        )}
      </div>
    </>
  );
}

type Format = "pdf" | "pptx";
const FORMATS: Format[] = ["pdf", "pptx"];
const formatName = (f: Format) => (f === "pdf" ? t("report.pdf") : t("report.pptx"));

// the ticked pages in report order (the PDF/PPTX follows this order); `off` holds the unticked ones
export const ticked = (report: ReportSpec, off: Set<string>) => report.pages.filter((p) => !off.has(p.id));

// MULTI-EXPORT (RP-6): pick pages (all ticked when it opens) and a format; <dialog> returns focus to the Export button
export function ExportDialog({ report, onExport }: { report: ReportSpec; onExport: (pages: PageSpec[], format: Format) => void }) {
  const dlg = useRef<HTMLDialogElement>(null);
  const [off, setOff] = useState<Set<string>>(new Set());
  const [format, setFormat] = useState<Format>("pdf");
  const pages = ticked(report, off);
  const go = () => {
    dlg.current!.close();
    onExport(pages, format);
  };
  return (
    <>
      <button id="export-open" className="sm" onClick={() => (setOff(new Set()), dlg.current!.showModal())}>{t("report.export")}</button>
      <dialog ref={dlg} className="share-dialog new-data" aria-labelledby="export-h" onClose={() => focusSoon("export-open")}>
        <h2 id="export-h">{t("report.exportHeading", { name: report.name })}</h2>
        <fieldset>
          <legend>{t("report.exportPages")}</legend>
          {report.pages.map((p) => (
            <label key={p.id}><input type="checkbox" checked={!off.has(p.id)}
              onChange={(e) => setOff((s) => { const n = new Set(s); if (e.target.checked) n.delete(p.id); else n.add(p.id); return n; })} /> {p.name}</label>
          ))}
        </fieldset>
        <fieldset className="choice">
          <legend>{t("report.exportFormat")}</legend>
          {FORMATS.map((f) => (
            <label key={f}><input type="radio" name="export-format" value={f} checked={format === f} onChange={() => setFormat(f)} /> {formatName(f)}</label>
          ))}
        </fieldset>
        {!pages.length && <p className="muted">{t("report.exportNone")}</p>}
        <div className="row">
          <button id="export-go" className="primary" disabled={!pages.length} onClick={go}>{t("report.export")}</button>
          <button onClick={() => dlg.current!.close()}>{t("common.cancel")}</button>
        </div>
      </dialog>
    </>
  );
}

type VisualProps = {
  project: string; report: string; page: string; spec: Spec; readOnly: boolean; writeBack: boolean; terse: boolean; filters: Filter[]; own?: Filter;
  onProps: () => void; onLive: (on: boolean) => void; onUpdated: (text: string) => void; onPick: (column: string, value: Value) => void; onSlice: (column: string, values: Value[]) => void;
  onExport: (what: string, link: () => Promise<string>) => void;
};

export function Visual({ project, report, page, spec, readOnly, writeBack, terse, filters, own, onProps, onLive, onUpdated, onPick, onSlice, onExport }: VisualProps) {
  const ref = { project, report, page, visual: spec.id, interaction: filters };
  const q = useQuery({ queryKey: ["visual", ref], queryFn: () => post<VisualData>("visual_data", ref), placeholderData: keepPreviousData });
  const [explain, setExplain] = useState(false);
  const [failed, setFailed] = useState<{ text: string; stale: boolean; cell: string } | null>(null);
  const qc = useQueryClient();
  const d = q.data;
  // Every visual reads the edited table through the semantic engine, so all of them refresh after an edit.
  const refresh = () => Promise.all(["visual", "how"].map((k) => qc.invalidateQueries({ queryKey: [k] })));
  // WB-1 in table and matrix visuals at the table key's grain (V-02 C-7), WB-11 totals of SUM measures; SC-2: Editors and Owners only,
  // in production too (D-57: write-back is data entry, not design)
  const edit: Edit | undefined = !writeBack || !d?.edit ? undefined : async (row, column, text, cell) => {
    try {
      if (cellMode(d, row, column) === "total") await post("edit_total", { project, ...totalEdit(d, row, column, text) });
      else await post("edit_cells", { project, ...cellEdit(d, row, column, text) });
      setFailed(null);
      await refresh();
    } catch (e) {
      setFailed({ text: (e as Error).message, stale: (e as { status?: number }).status === 409, cell });
    }
  };
  const reload = async () => { // WB-7: show the current values, focus back on the cell (the Reload button unmounts)
    setFailed(null);
    await refresh();
    focusSoon(failed!.cell);
  };
  // DEMO-1.4: an untitled text visual is named "Text" for the drag handle (Editors) and screen readers, never by its ID
  const untitled = spec.type === "text" && !spec.title;
  const title = untitled ? t("report.textVisual") : spec.title ?? (d?.measures ? (dims(d).length ? t("report.titleBy", { measures: Object.values(d.measures).join(", "), dims: dims(d).map((c) => label(d, c)).join(", ") }) : Object.values(d.measures).join(", ")) : spec.id);
  const hid = `v-${spec.id}-h`;
  // RP-7: a live visual says so in text and when its numbers were read; a re-read of the same query is announced
  const isLive = !!d?.live?.length;
  const at = q.dataUpdatedAt ? fmtDate(q.dataUpdatedAt, { timeStyle: "medium" }) : "…"; // 0 while a new filter's data loads
  useEffect(() => (onLive(isLive), () => onLive(false)), [isLive]);
  const seen = useRef<[string, number]>(["", 0]);
  const key = JSON.stringify(ref);
  useEffect(() => {
    const [k, was] = seen.current;
    if (isLive && k === key && was && was !== q.dataUpdatedAt) onUpdated(t("report.updatedAt", { title, at }));
    seen.current = [key, q.dataUpdatedAt];
  }, [q.dataUpdatedAt]);
  // a blank member can't be an `=` filter
  const pick = d && dims(d).length ? (x: Value) => x !== null && onPick(dims(d)[0], x) : undefined;
  return (
    <section aria-labelledby={hid}>
      <div className={untitled && readOnly ? "vhead bare" : "vhead"}>
        <h3 id={hid} className={untitled ? (readOnly ? "drag sr-only" : "drag muted") : "drag"} title={t("report.dragHint")}>{title}</h3>
        {isLive && <span className="live"><span className="badge">{t("report.live")}</span> <span className="muted">{t("report.updated", { at })}</span></span>}
        {d && d.type !== "text" && ([["csv", t("report.csv")], ["xlsx", t("report.excel")]] as const).map(([format, name]) => (
          <button key={format} className="sm ghost" aria-label={t("report.exportVisualAs", { title, format: name })}
            onClick={() => onExport(t("report.visualAs", { title, format: name }), async () => (await post<{ link: string }>("export_visual", { project, report, page, visual: spec.id, format, filters })).link)}>
            {name /* short, so narrow cards (KPI) keep one header row; the name says "Export <title> as <format>" */}
          </button>
        ))}
        {/* RP-5: with the report's hide_details on, the explanation is one click away here */}
        {terse && d && d.type !== "text" && <button className="sm ghost" aria-expanded={explain} onClick={() => setExplain(!explain)}>{t("report.how")}</button>}
        {/* SC-2/UI-3: properties are a build tool, Editors and Owners only */}
        {!readOnly && <button id={`props-${spec.id}`} className="icon ghost sliders" aria-label={t("report.propertiesOf", { title })} onClick={onProps}><span className="sr-only">{t("report.properties")}</span></button>}
      </div>
      <div className="vbody">
        {q.error && <p role="alert" className="error">{q.error.message}</p>}
        {!d && !q.error && <p role="status" className="muted">{t("common.loading")}</p>}
        {failed && <div role="alert" className="error">{failed.text} {failed.stale && <button className="sm" onClick={reload}>{t("report.reload")}</button>}</div>}
        {d && <Body d={d} own={own} pick={pick} onSlice={onSlice} edit={edit} caption={!terse} />}
        {terse && explain && <Explain {...ref} />}
        {!terse && d && d.type !== "text" && (
          <details className="how" onToggle={(e) => setExplain(e.currentTarget.open)}>
            <summary>{t("report.how")}</summary>
            {explain && <Explain {...ref} />}
          </details>
        )}
      </div>
    </section>
  );
}

type Edit = (row: Value[], column: string, text: string, cell: string) => Promise<void>;

type BodyProps = { d: VisualData; own?: Filter; pick?: (v: Value) => void; onSlice: VisualProps["onSlice"]; edit?: Edit; caption?: boolean };

// Every visual's content (report, print, share, embed) with its SE-13 caption; text visuals have no numbers. The
// report canvas drops the caption under hide_details: its "How is this calculated?" shows the zone and currency.
export function Body({ caption = true, ...p }: BodyProps) {
  // UI-1.4.3: a slicer or cross-filter this visual's measures can't reach leaves it unfiltered, and it says so
  const off = p.d.unfiltered ?? [];
  return <>{off.length > 0 && <p className="muted locale">{t("report.notFiltered", { columns: off.map((c) => label(p.d, c)).join(", ") })}</p>}
    {/* ML-SEASONAL.3: in words (not colour only) on every view, hide_details or not */}
    {(p.d.warnings ?? []).map((w) => <p key={w.model} role="note" className="warn">{forecastWarning(w)}</p>)}
    <Content {...p} />{caption && p.d.time_zone && <p className="muted locale">{locale(p.d)}</p>}</>;
}

const pct = (x: number | null) => (x == null ? t("chart.blank") : fmtNumber(x, { style: "percent", maximumFractionDigits: 1 }));
const amount = (x: number) => fmtNumber(x, { maximumFractionDigits: 2 });

function forecastWarning(w: NonNullable<VisualData["warnings"]>[number]): string {
  return [w.weak && t("report.weakForecast", { model: w.model, wape: pct(w.wape), baseline: pct(w.baseline_wape) }),
    w.forecast_total != null && t("report.implausibleForecast", { model: w.model, total: amount(w.forecast_total), last: amount(w.last_year_total!) })]
    .filter(Boolean).join(" ");
}

function Content({ d, own, pick, onSlice, edit }: BodyProps) {
  const lang = useLang((s) => s.lang);
  const look = useTheme();
  const [width, setWidth] = useState(0); // the chart's width decides how category labels fit (RP-1)
  const option = useMemo(() => chartOption(d, undefined, [], width, look), [d, lang, width, look]); // a language change reformats axes and tooltips
  const selected = own ? [own.value].flat() : [];
  if (d.type === "text") return <p className="text">{d.text /* ponytail: plain text, a Markdown renderer when text visuals need formatting */}</p>;
  if (d.type === "kpi")
    return (
      <dl className="kpi">
        {Object.entries(d.measures!).map(([m, name]) => (
          // RP-1: one measure under its own name as the card's heading: the name is said, not shown twice (the footer fits)
          <div key={m}><dt className={Object.keys(d.measures!).length === 1 && (d.title ?? name) === name ? "sr-only" : undefined}>{name}</dt><dd>{format(d.rows?.[0]?.[d.columns!.indexOf(m)] ?? null, d.format?.[m])}</dd></div>
        ))}
      </dl>
    );
  if (d.type === "slicer") {
    const dim = dims(d)[0];
    const members = [...new Set((d.rows ?? []).map((r) => r[d.columns!.indexOf(dim)]))].filter((x) => x !== null);
    const set = (x: Value, on: boolean) => onSlice(dim, on ? [...selected, x] : selected.filter((y) => y !== x));
    return (
      // RP-1/RP-5: a short card (h 2) scrolls its members so the caption and "How is this calculated?" stay in view
      <fieldset className="slicer" style={{ minHeight: "1.5em", overflow: "auto" }}>
        <legend className="sr-only">{label(d, dim)}</legend>
        {members.map((x) => (
          <label key={String(x)}><input type="checkbox" checked={selected.includes(x)} onChange={(e) => set(x, e.target.checked)} /> {format(x, d.format?.[dim])}</label>
        ))}
      </fieldset>
    );
  }
  if (d.type === "table") return <DataTable d={d} selected={selected} pick={pick} edit={edit} />;
  if (d.type === "matrix") return <Matrix d={d} selected={selected} pick={pick} edit={edit} />;
  if (d.type === "map") return <MapVisual d={d} selected={selected} pick={pick} />;
  return (
    <>
      {option && <Chart option={option} onWidth={setWidth} onClick={(i, item) => pick?.(picked(d, i, undefined, item))} />}
      <details>
        <summary>{t("report.dataTable")}</summary>
        <DataTable d={d} selected={selected} pick={pick} />
      </details>
    </>
  );
}

// RP-1 map: members that match no country are listed with their values (none silently dropped); the data table is
// the text alternative and the keyboard path (its member buttons cross-filter like a click on a country).
function MapVisual({ d, selected, pick }: { d: VisualData; selected: Value[]; pick?: (v: Value) => void }) {
  const w = useQuery(worldQuery);
  const lang = useLang((s) => s.lang);
  const look = useTheme();
  const option = useMemo(() => w.data && chartOption(d, w.data, selected, 0, look), [d, w.data, String(selected), lang, look]);
  if (w.error) return <p role="alert" className="error">{t("report.mapFailed", { error: w.error.message })}</p>;
  if (!option || !w.data) return <p role="status" className="muted">{t("report.mapLoading")}</p>;
  const off = mapMatch(d, w.data);
  return (
    <>
      <Chart option={option} onClick={(i) => pick?.(picked(d, i, w.data))} />
      {off.unmatched.length > 0 && (
        <p className="muted">{t("report.notOnMap", { members: off.unmatched.map((x) => `${format(x.member)} (${format(x.value, d.format?.[off.measure])})`).join(", ") })}</p>
      )}
      <details>
        <summary>{t("report.dataTable")}</summary>
        <DataTable d={d} selected={selected} pick={pick} />
      </details>
    </>
  );
}

// A member cell: a toggle button that cross-filters the page (the keyboard path for chart clicks too).
function Member({ v, f, selected, pick }: { v: Value; f?: Fmt; selected: Value[]; pick?: (v: Value) => void }) {
  return pick && v !== null && v !== TOTAL ? <button className="member" aria-pressed={selected.includes(v)} onClick={() => pick(v)}>{format(v, f)}</button> : <>{format(v, f)}</>;
}

// An editable visual cell (C-7): a native input holding the raw value; Enter or leaving it saves, Escape reverts.
function CellInput({ d, row, ri, column, edit }: { d: VisualData; row: Value[]; ri: number; column: string; edit: Edit }) {
  const raw = row[d.columns!.indexOf(column)];
  const was = raw == null ? "" : String(raw);
  const [text, setText] = useState(was);
  const sent = useRef<string | null>(null); // Enter then blur (or a blur after a refusal) must not resend the same text
  useEffect(() => {
    setText(was);
    sent.current = null;
  }, [was]);
  const id = `cell-${d.id}-${ri}-${column}`;
  const save = () => {
    if (text === was || text === sent.current) return;
    sent.current = text;
    edit(row, column, text, id);
  };
  const at = cellMode(d, row, column) === "row" ? d.edit!.key! : dims(d); // a total: every member, "Total" where rolled up
  const where = at.map((k) => `${label(d, k)} ${format(member(d, row, k))}`).join(", ");
  return <input id={id} className="cell" aria-label={`${label(d, column)}, ${where}`} value={text} onChange={(e) => setText(e.target.value)} onBlur={save}
    onKeyDown={(e) => (e.key === "Enter" ? save() : e.key === "Escape" && setText(was))} />;
}

// The text alternative of every chart (WCAG 1.1.1) and the table visual itself.
function DataTable({ d, selected, pick, edit }: { d: VisualData; selected: Value[]; pick?: (v: Value) => void; edit?: Edit }) {
  const first = dims(d)[0];
  const input = (r: Value[], i: number, c: string) => edit && cellMode(d, r, c) ? <CellInput d={d} row={r} ri={i} column={c} edit={edit} /> : null;
  const cols = d.columns!.filter((c) => c !== "grouping_id"); // RP-1 totals: rolled-up members read "Total" instead
  const shown = (r: Value[], c: string) => (c in d.measures! ? r[d.columns!.indexOf(c)] : member(d, r, c));
  return (
    <table>
      <thead><tr>{cols.map((c) => <th key={c} scope="col">{label(d, c)}</th>)}</tr></thead>
      <tbody>
        {d.rows!.map((r, i) => (
          <tr key={i}>
            {cols.map((c) => (c === first
              ? <th key={c} scope="row">{input(r, i, c) ?? <Member v={shown(r, c)} f={d.format?.[c]} selected={selected} pick={pick} />}</th>
              : <td key={c} className={c in d.measures! ? "num" : undefined}>{input(r, i, c) ?? format(shown(r, c), d.format?.[c])}</td>))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// Matrix: first dimension down, second dimension (or the measures) across.
function Matrix({ d, selected, pick, edit }: { d: VisualData; selected: Value[]; pick?: (v: Value) => void; edit?: Edit }) {
  const { dim, cats, series } = pivot(d);
  return (
    <table>
      <thead><tr><th scope="col">{dim ? label(d, dim) : ""}</th>{series.map((s) => <th key={s.name} scope="col">{s.name}</th>)}</tr></thead>
      <tbody>
        {cats.map((c, i) => (
          <tr key={String(c)}>
            <th scope="row"><Member v={c} f={dim ? d.format?.[dim] : undefined} selected={selected} pick={pick} /></th>
            {series.map((s) => {
              const ri = s.rows[i];
              return (
                <td key={s.name} className="num">
                  {edit && ri != null && cellMode(d, d.rows![ri], s.measure)
                    ? <CellInput d={d} row={d.rows![ri]} ri={ri} column={s.measure} edit={edit} />
                    : s.data[i] == null ? "" : format(s.data[i], d.format?.[s.measure])}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Chart({ option, onClick, onWidth }: { option: Record<string, unknown>; onClick: (dataIndex: number, item: unknown) => void; onWidth?: (w: number) => void }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<ReturnType<typeof init>>(null);
  const click = useRef(onClick);
  click.current = onClick;
  const width = useRef(onWidth);
  width.current = onWidth;
  useEffect(() => {
    const c = (chart.current = init(el.current!, null, { renderer: "svg" }));
    c.on("click", (p) => click.current(p.dataIndex, p.data));
    // A hidden tab (display:none) reports 0×0; ECharts' map series throws resizing to that. Showing the tab
    // again fires the observer with the real size, which redraws the chart.
    const ro = new ResizeObserver(([e]) => void (e.contentRect.width && e.contentRect.height && (c.resize(), width.current?.(Math.round(e.contentRect.width / 16) * 16))));
    ro.observe(el.current!);
    return () => (ro.disconnect(), c.dispose());
  }, []);
  useEffect(() => void chart.current!.setOption(option, true), [option]);
  return <div ref={el} className="chart" />;
}

type Explained = { time_zone: string; currency: string | null; visuals: How[] };

// UI-DS.6 (UI-DS.1 R4/R5/R7): a chat answer's visual. Its spec carries no number; the numbers come from visual_data at
// render and show through Body, like every report visual. A failure shows a fixed text by status, never the server's
// detail (it would quote what the model wrote, e.g. an unknown measure ID).
export function ChatVisual({ project, json }: { project: string | null; json: string }) {
  const spec = chatSpec(json);
  const ref = spec && project ? { project, query: { measures: spec.measures, dimensions: spec.dimensions ?? [], filters: spec.filters ?? [] }, ...(spec.type && { type: spec.type }) } : null;
  const q = useQuery({ queryKey: ["visual", ref], queryFn: () => post<VisualData>("visual_data", ref!), enabled: !!ref, retry: false });
  const [explain, setExplain] = useState(false);
  const hid = `cv-${useId()}`;
  const d = q.data;
  const status = (q.error as { status?: number } | null)?.status;
  const problem = !ref ? t("chat.visual.invalid") : !q.error ? "" : t(status === 400 || status === 422 ? "chat.visual.unknown" : status === 403 ? "chat.visual.denied" : "chat.visual.failed");
  const title = d?.measures ? (dims(d).length ? t("report.titleBy", { measures: Object.values(d.measures).join(", "), dims: dims(d).map((c) => label(d, c)).join(", ") }) : Object.values(d.measures).join(", ")) : t("chat.visual.chart");
  return (
    <section className="chat-visual" aria-labelledby={hid}>
      <h3 id={hid}>{title}</h3>
      {problem ? <p role="alert" className="error">{problem}</p> : !d ? <p role="status" className="muted">{t("common.loading")}</p> : <>
        <Body d={d} onSlice={() => {}} />
        <details className="how" onToggle={(e) => setExplain(e.currentTarget.open)}>
          <summary>{t("report.how")}</summary>
          {explain && <Explain {...ref!} />}
        </details>
      </>}
    </section>
  );
}

// RP-5 "How is this calculated?": measures (with the ones they reference), filters with their level, the SQL.
function Explain(ref: { project: string; query?: unknown }) {
  const q = useQuery({ queryKey: ["how", ref], queryFn: () => post<Explained>("how_calculated", ref) });
  // a chat visual's error would quote what the model wrote (UI-DS.1 R5)
  if (q.error) return <p role="alert" className="error">{ref.query ? t("chat.visual.failed") : q.error.message}</p>;
  const v = q.data?.visuals[0];
  return v ? <Explanation v={v} loc={q.data!} /> : <p role="status" className="muted">{t("common.loading")}</p>;
}
