import { CompactSelection, DataEditor, GridCellKind, type DataEditorRef, type EditableGridCell, type GridCell, type GridSelection, type Item, type Rectangle } from "@glideapps/glide-data-grid";
import "@glideapps/glide-data-grid/dist/index.css";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, canEdit, lakeModelQuery, meQuery, NUMERIC, post, type Page } from "./api";
import { Confirm } from "./mel";
import { focusSoon, useTheme } from "./store";
import WriteBack, { type Cell } from "./WriteBack";
import { t } from "./i18n";

const PAGE = 100;
// UI-DS.8 (D-77): Glide paints on a canvas, so it gets the design tokens' current values, light or dark
const TOKENS = { accentColor: "--accent", accentLight: "--accent-weak", textDark: "--text", textMedium: "--text-muted", textLight: "--text-muted",
  textBubble: "--text", bgIconHeader: "--text-muted", fgIconHeader: "--bg", textHeader: "--heading", textHeaderSelected: "--bg", bgCell: "--bg",
  bgCellMedium: "--surface", bgHeader: "--surface", bgHeaderHasFocus: "--surface-2", bgHeaderHovered: "--surface-2", bgBubble: "--surface",
  bgBubbleSelected: "--bg", bgSearchResult: "--warn-weak", borderColor: "--border", horizontalBorderColor: "--border",
  drilldownBorder: "--border-strong", linkColor: "--accent", fontFamily: "--font" };
const gridTheme = () => {
  const css = getComputedStyle(document.documentElement);
  return Object.fromEntries(Object.entries(TOKENS).map(([k, v]) => [k, css.getPropertyValue(v).trim()]));
};
const NONE: GridSelection = { columns: CompactSelection.empty(), rows: CompactSelection.empty() };
type Row = (string | null)[];

/** WB-1 `edit_cells` body for a grid cell: the row's key and, as `old`, the value the grid showed (WB-7). An empty
 * input clears the cell. */
// ponytail: "" means null, so a text cell can't be set to an empty string; add a "clear" action if users need both
export const cellBody = (cols: string[], key: string[], row: Row, col: number, text: string) =>
  ({ edits: [{ key: key.map((k) => row[cols.indexOf(k)]), column: cols[col], value: text === "" ? null : text, old: row[col] }] });

/** WB-1 `edit_cells` body deleting rows by key. */
export const deleteBody = (cols: string[], key: string[], rows: Row[]) => ({ delete: rows.map((r) => key.map((k) => r[cols.indexOf(k)])) });

type Alert = { text: string; stale: boolean };

// UI-2 table grid of a model table, loaded page by page as the user scrolls, or of a step preview (TR-3) when
// `preview` is given. Editors edit cells, add and delete rows of a keyed table (WB-1) through `edit_cells`; a keyless
// table is read-only and offers to declare its key (WB-5). Viewers and previews are read-only.
export default function TableGrid({ table, preview }: { table: string /* "project/table" */; preview?: Page | null }) {
  const pages = useRef(new Map<number, Row[]>());
  const [head, setHead] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, redraw] = useState(0); // new getCellContent identity makes Glide repaint
  const slash = table.indexOf("/");
  const [project, name] = [table.slice(0, slash), table.slice(slash + 1)];
  const url = `/api/projects/${encodeURIComponent(project)}/tables/${encodeURIComponent(name)}/rows`;
  const qc = useQueryClient();
  const me = useQuery(meQuery(project));
  const model = useQuery(lakeModelQuery(project));
  const editor = !preview && canEdit(me.data?.role); // SC-2
  const key = model.data ? (model.data.tables[name]?.key ?? null) : undefined; // undefined: still loading
  const editable = editor && !!key;
  const look = useTheme();
  const theme = useMemo(() => (typeof document === "undefined" ? undefined : gridTheme()), [look]);
  const grid = useRef<DataEditorRef>(null);
  const visible = useRef<Rectangle>({ x: 0, y: 0, width: 1, height: PAGE });
  const [sel, setSel] = useState<GridSelection>(NONE);
  const [alert, setAlert] = useState<Alert | null>(null);
  const [status, setStatus] = useState("");
  const [adding, setAdding] = useState(false);
  const [deleting, setDeleting] = useState<Row[] | null>(null);
  const ids = { add: `add-row-${name}`, del: `delete-rows-${name}` };

  const load = useCallback(
    (p: number) => {
      if (pages.current.has(p)) return;
      pages.current.set(p, []); // mark in flight
      api<Page>(`${url}?offset=${p * PAGE}&limit=${PAGE}`).then(
        (page) => {
          pages.current.set(p, page.rows);
          if (p === 0) setHead(page);
          redraw((n) => n + 1);
        },
        (e: Error) => {
          pages.current.delete(p);
          setError(e.message);
        },
      );
    },
    [url],
  );

  useEffect(() => load(0), [load]);

  const onVisible = useCallback(
    (r: Rectangle) => {
      visible.current = r;
      for (let p = Math.floor(r.y / PAGE); p <= Math.floor((r.y + r.height - 1) / PAGE); p++) load(p);
    },
    [load],
  );
  // Refetch the rows in view (and page 0 for the row count) after an edit or a Reload (WB-7).
  const reload = () => {
    pages.current.clear();
    load(0);
    onVisible(visible.current);
  };

  const rowAt = (row: number): Row | undefined => pages.current.get(Math.floor(row / PAGE))?.[row % PAGE];
  const getCellContent = useCallback(
    ([col, row]: Item): GridCell => {
      const value = preview ? preview.rows[row]?.[col] : rowAt(row)?.[col];
      if (value === undefined) return { kind: GridCellKind.Loading, allowOverlay: false };
      const shown = (preview ?? head)!.columns[col];
      const text = value ?? "";
      const right = NUMERIC.test(shown.type);
      const readonly = !editable || key!.includes(shown.name); // key columns identify the row: never edited
      return { kind: GridCellKind.Text, data: text, displayData: text, allowOverlay: true, readonly, contentAlign: right ? "right" : "left" };
    },
    [head, version, preview, editable, key],
  );

  // After a change the grid's rows in view, every visual (they read the table through the semantic engine) and the
  // write-back panel refetch.
  const refresh = () => (reload(), Promise.all(["visual", "how", "writeback"].map((k) => qc.invalidateQueries({ queryKey: [k] }))));
  // One edit_cells batch.
  const change = async (body: object, done: string) => {
    try {
      await post("edit_cells", { project, table: name, ...body });
      setAlert(null);
      setStatus(done);
      await refresh();
      return true;
    } catch (e) {
      setAlert({ text: (e as Error).message, stale: (e as { status?: number }).status === 409 });
      return false;
    }
  };
  const cols = head?.columns.map((c) => c.name) ?? [];
  const onCellEdited = ([col, row]: Item, v: EditableGridCell) => {
    const r = rowAt(row);
    if (!r || v.kind !== GridCellKind.Text || v.data === (r[col] ?? "")) return;
    change(cellBody(cols, key!, r, col, v.data), t("grid.savedCell", { column: cols[col], row: row + 1 }));
  };
  const askDelete = () => {
    const rows = sel.rows.toArray().map(rowAt).filter((r): r is Row => !!r);
    if (!rows.length) return setStatus(t("grid.selectFirst"));
    setDeleting(rows);
  };
  const del = async () => {
    const n = deleting!.length;
    if (await change(deleteBody(cols, key!, deleting!), t("grid.deleted", { n }))) setSel(NONE);
    setDeleting(null);
    focusSoon(ids.del); // WCAG 2.4.3: the Confirm box unmounts
  };
  // WB-6 review "Go to cell": the row's position in the grid's order, then scroll to and select the cell.
  const goTo = async (rowKey: string, column: string | null) => {
    const { position } = await api<{ position: number | null }>(`${url.slice(0, -5)}/position?key=${encodeURIComponent(rowKey)}`);
    if (position === null) return t("grid.orphaned");
    const col = Math.max(0, cols.indexOf(column ?? key![0]));
    setSel({ ...NONE, current: { cell: [col, position], range: { x: col, y: position, width: 1, height: 1 }, rangeStack: [] } });
    grid.current?.scrollTo(col, position);
    grid.current?.focus();
    return t("grid.selected", { column: cols[col], row: position + 1 });
  };
  // WB-8 history of the grid's selected cell (a key column: its whole row)
  const picked = sel.current?.cell;
  const pickedRow = picked && rowAt(picked[1]);
  const pickedCol = picked && key && !key.includes(cols[picked[0]]) ? cols[picked[0]] : null;
  const cell: Cell | null = pickedRow && key ? {
    key: key.map((k) => pickedRow[cols.indexOf(k)]),
    column: pickedCol,
    label: t("grid.cellAt", { column: pickedCol ?? t("grid.row"), key: key.map((k) => `${k} ${pickedRow[cols.indexOf(k)]}`).join(", ") }),
  } : null;
  const onReload = () => {
    setAlert(null);
    reload();
    grid.current?.focus(); // the Reload button unmounts; focus goes back to the grid, where the edit was
  };

  if (!preview && error) return <p role="alert">{t("grid.loadFailed", { table, error })}</p>;
  if (!preview && !head) return <p role="status">{t("grid.loading", { table })}</p>;
  const shown = (preview ?? head)!;
  return (
    <div className="table-grid">
      {editor && key === null && <KeyPicker project={project} table={name} columns={cols} focusAfter={ids.add} />}
      {editable && (
        <div className="row caption toolbar">
          <button id={ids.add} className="sm" aria-expanded={adding} onClick={() => setAdding(!adding)}>{t("grid.addRow")}</button>
          <button id={ids.del} className="sm" onClick={askDelete}>{t("grid.deleteRows")}</button>
          <p role="status" className="muted">{status}</p>
        </div>
      )}
      {adding && editable && (
        <AddRow columns={head!.columns} keyCols={key!}
          onAdd={async (row) => (await change({ add: [row] }, t("grid.added"))) && (setAdding(false), focusSoon(ids.add))}
          onCancel={() => (setAdding(false), focusSoon(ids.add))} />
      )}
      {deleting && (
        <Confirm label={t("common.confirmDelete")} message={t("grid.confirmDelete", { n: deleting.length, table: name })}
          ok={t("common.confirmDelete")} onConfirm={del} onCancel={() => setDeleting(null)} back={ids.del} />
      )}
      {alert && (
        <div role="alert" className="error caption row">
          {alert.text}
          {alert.stale && <button className="sm" onClick={onReload}>{t("grid.reload")}</button>}
        </div>
      )}
      <div className="grid-canvas">
        <DataEditor
          ref={grid}
          columns={shown.columns.map((c) => ({ id: c.name, title: `${c.name} (${c.type})${key?.includes(c.name) ? t("grid.key") : ""}`, width: 160 }))}
          rows={shown.total}
          getCellContent={getCellContent}
          onVisibleRegionChanged={preview ? undefined : onVisible}
          rowMarkers={editable ? "clickable-number" : "number"}
          gridSelection={sel}
          onGridSelectionChange={setSel}
          onCellEdited={editable ? onCellEdited : undefined}
          onDelete={editable ? () => (askDelete(), false) : undefined /* rows only, behind Confirm; never clears cells */}
          theme={theme}
          width="100%"
          height="100%"
        />
      </div>
      {!preview && key && (
        <WriteBack project={project} table={name} editor={editor} owner={me.data?.role === "owner"} cell={cell} goTo={goTo}
          onChanged={refresh} />
      )}
    </div>
  );
}

// WB-1 add row: one field per column; the key is required (the row is found by it).
function AddRow({ columns, keyCols, onAdd, onCancel }: { columns: Page["columns"]; keyCols: string[]; onAdd: (row: Record<string, string | null>) => void; onCancel: () => void }) {
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    onAdd(Object.fromEntries(columns.map((c) => [c.name, (f.get(c.name) as string) || null])));
  };
  return (
    <form className="add-row caption row" aria-label={t("grid.newRow")} onSubmit={submit} onKeyDown={(e) => e.key === "Escape" && onCancel()}>
      {columns.map((c, i) => (
        <label key={c.name}>
          {c.name}{keyCols.includes(c.name) && t("grid.keyMark")}
          <input name={c.name} required={keyCols.includes(c.name)} autoFocus={i === 0} />
        </label>
      ))}
      <button className="primary">{t("grid.add")}</button>
      <button type="button" onClick={onCancel}>{t("common.cancel")}</button>
    </form>
  );
}

// WB-5: a keyless table can't be edited; declaring its key (set_table, a definition change) makes it editable.
function KeyPicker({ project, table, columns, focusAfter }: { project: string; table: string; columns: string[]; focusAfter: string }) {
  const qc = useQueryClient();
  const [err, setErr] = useState("");
  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const key = new FormData(e.currentTarget).getAll("key") as string[];
    if (!key.length) return setErr(t("grid.chooseKey"));
    try {
      await post("set_table", { project, table, key });
      await qc.invalidateQueries({ queryKey: ["model", project] }); // the grid becomes editable and this form unmounts
      focusSoon(focusAfter);
    } catch (x) {
      setErr((x as Error).message);
    }
  };
  return (
    <form className="caption" onSubmit={submit} aria-labelledby={`nokey-${table}`}>
      <p id={`nokey-${table}`}>{t("grid.noKey")}</p>
      <fieldset className="row">
        <legend className="sr-only">{t("grid.keyColumns")}</legend>
        {columns.map((c) => <label key={c}><input type="checkbox" name="key" value={c} /> {c}</label>)}
        <button className="primary">{t("grid.setKey")}</button>
      </fieldset>
      {err && <p role="alert" className="error">{err}</p>}
    </form>
  );
}
