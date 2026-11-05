import { fmtDate, fmtNumber, t } from "./i18n";
// RP-2 visual spec → ECharts option, RP-3 cross-filter selections, number formats. Pure: no DOM, no ECharts import.
// Every number comes from `visual_data` (principle 4); this only arranges and formats what the semantic engine returned.

export type Value = string | number | boolean | null;
export type Fmt = { style?: "number" | "integer" | "percent" | "month"; decimals?: number; compact?: boolean }; // month: a DATE member
export type VisualData = {
  id: string;
  type: "kpi" | "bar" | "line" | "pie" | "table" | "matrix" | "scatter" | "slicer" | "map" | "text"
    | "combo" | "waterfall" | "funnel" | "heatmap" | "treemap" | "sankey"; // RP-8
  title: string | null;
  text?: string;
  format?: Record<string, Fmt>;
  columns?: string[]; // dimensions (table.column), grouping_id if totals, then measure IDs
  measures?: Record<string, string>; // measure ID → display name
  names?: Record<string, string>; // UI-1.4.3: display names of ML output columns (dimension → name)
  unfiltered?: string[]; // UI-1.4.3: slicer/cross-filter columns this visual's measures can't reach (not applied)
  // ML-SEASONAL.3: weak or implausible forecast models whose predictions this visual's measures read
  warnings?: { model: string; weak: boolean; wape: number | null; baseline_wape: number | null; forecast_total?: number; last_year_total?: number }[];
  rows?: Value[][];
  // V-02 C-7: `table`/`key`/`columns` when the visual's dimensions include a table's key (`columns` maps visual columns
  // to its columns); WB-11: `total` = the SUM measures whose aggregated cells spread via `edit_total`, `filters` the visual's
  edit?: { table?: string; key?: string[]; columns?: Record<string, string>; total?: string[];
    filters?: { column: string; op: string; value: unknown }[] } | null;
  live?: string[]; // RP-7: the live (ClickHouse) tables these numbers read
  time_zone?: string; currency?: string | null; // SE-13: the zone of its days and the display currency
};

// RP-7: a `changed {tables}` event touches a visual when it read one of those live tables.
export const touches = (v: VisualData | undefined, tables: string[]) => !!v?.live?.some((t) => tables.includes(t));
export type Filter = { column: string; op: "=" | "in"; value: Value | Value[] };

// S-06 format spec with Intl.NumberFormat; `percent` values are ratios (Intl multiplies by 100). No spec (a dimension
// member such as a year): shown as is.
export function format(v: Value, f?: Fmt): string {
  if (v === TOTAL) return t("chart.total");
  if (f?.style === "month" && typeof v === "string") return fmtDate(`${v}T00:00:00Z`, { month: "short", year: "numeric", timeZone: "UTC" });
  if (typeof v !== "number" || !f) return v == null ? t("chart.blank") : String(v);
  const d = f.style === "integer" ? 0 : (f.decimals ?? 2);
  return fmtNumber(v, {
    style: f.style === "percent" ? "percent" : "decimal",
    minimumFractionDigits: f.compact ? 0 : d,
    maximumFractionDigits: d,
    notation: f.compact ? "compact" : "standard",
  });
}

export const dims = (v: VisualData) => (v.columns ?? []).filter((c) => !(c in (v.measures ?? {})) && c !== "grouping_id");
export const label = (v: VisualData, c: string) => v.measures?.[c] ?? v.names?.[c] ?? c.slice(c.indexOf(".") + 1);
const col = (v: VisualData, c: string) => v.columns!.indexOf(c);

// RP-1 totals: `grouping_id` = GROUPING_ID(dimensions in query order), first dimension = highest bit; a set bit means
// the row rolls that dimension up. Such a member shows as TOTAL ("Total"), never as a blank member.
export const TOTAL = "\u0000total";
export function rolled(v: VisualData, row: Value[], dim: string) {
  const ds = dims(v);
  return (((row[col(v, "grouping_id")] as number) ?? 0) >> (ds.length - 1 - ds.indexOf(dim))) % 2 === 1;
}
export const member = (v: VisualData, row: Value[], dim: string) => (rolled(v, row, dim) ? TOTAL : row[col(v, dim)]);

/** DEMO-1.4: in a bar, line or combo chart, a (month or quarter, year) pair of one table, both numbers in every row, is one time axis: the calendar
 * has no year-month column, and one series per year put 2017's months 11 and 12 after 2018's 1..5. [period, year] or null. */
export function period(v: VisualData): [string, string] | null {
  const ds = dims(v), [a, b] = ds;
  if (!["bar", "line", "combo"].includes(v.type) || ds.length !== 2 || a.split(".")[0] !== b.split(".")[0]) return null;
  const [p, y] = a.endsWith(".year") ? [b, a] : [a, b];
  return y.endsWith(".year") && /\.(month|quarter)$/.test(p) && (v.rows ?? []).every((r) => typeof r[col(v, p)] === "number" && typeof r[col(v, y)] === "number")
    ? [p, y] : null;
}

/** The x categories (first dimension, raw values in first-seen order) and one series per measure, or, with a second
 * dimension, one series per member of it (first measure). */
export function pivot(v: VisualData) {
  // ponytail: x is always the first dimension; have visual_data say which one is the time axis if specs list it second
  const per = period(v);
  const [d0, d1] = per ? [per[0]] : dims(v);
  const at = (c: string) => col(v, c);
  const ms = Object.keys(v.measures ?? {});
  const rows = v.rows ?? [];
  const x = (r: Value[]): Value => !per ? (d0 ? member(v, r, d0) : null)
    : per[0].endsWith(".month") ? `${r[at(per[1])]}-${String(r[at(per[0])]).padStart(2, "0")}` : `${r[at(per[1])]} Q${r[at(per[0])]}`;
  const num = (r: Value[], c: string) => r[at(c)] as number;
  const inOrder = per ? [...rows].sort((r, s) => num(r, per[1]) - num(s, per[1]) || num(r, per[0]) - num(s, per[0])) : rows;
  const cats = [...new Map(inOrder.map((r) => [x(r), 0])).keys()];
  const idx = new Map(cats.map((c, i) => [c, i]));
  // `rows`: the index of the row behind each value (a matrix cell edits that row, C-7)
  const series: { name: string; measure: string; data: (number | null)[]; rows: (number | null)[] }[] = [];
  const put = (name: string, measure: string, row: Value[], ri: number) => {
    let s = series.find((x) => x.name === name);
    if (!s) series.push((s = { name, measure, data: cats.map(() => null), rows: cats.map(() => null) }));
    const i = idx.get(x(row))!;
    s.data[i] = row[at(measure)] as number | null;
    s.rows[i] = ri;
  };
  rows.forEach((r, ri) => {
    // ponytail: series are keyed by shown name, so a real member named "Total" shares the total column
    if (d1) put(format(member(v, r, d1), v.format?.[d1]), ms[0], r, ri);
    else for (const m of ms) put(label(v, m), m, r, ri);
  });
  return { dim: d0 as string | undefined, cats, series, members: !!d1 };
}

// UI-1.4.3: a member (series of a second dimension, pie slice) keeps its colour in every visual: the first visual to
// show it picks a palette colour no member holds yet (else one free in that visual). ponytail: per page load, so
// colours may differ between loads; persist the registry if that matters.
export const COLOURS = new Map<string, number>();
export function colours(names: string[], pal: string[]): string[] {
  const used = new Set<number>(), held = new Set(COLOURS.values()), all = [...PALETTE.keys()];
  return names.map((n) => {
    let i = COLOURS.get(n);
    if (i === undefined || used.has(i)) {
      i = all.find((k) => !used.has(k) && !held.has(k)) ?? all.find((k) => !used.has(k)) ?? used.size % PALETTE.length;
      if (!COLOURS.has(n)) (COLOURS.set(n, i), held.add(i));
    }
    used.add(i);
    return pal[i];
  });
}

// RP-3 value axis: ticks are 1/2/3/5 x 10^n, so a compact label gets up to 2 fraction digits whatever the measure's
// own decimals (3bn, 2.5bn; never "3bn, 3bn"); an integer measure never gets fractional ticks that round alike.
export const valueAxis = (f?: Fmt) => ({
  axisLabel: { formatter: (x: unknown) => format(x as Value, f?.compact ? { ...f, style: f.style === "percent" ? "percent" : "number", decimals: 2 } : f) },
  minInterval: f?.style === "integer" ? 1 : 0,
});

// RP-1 category axis: every label flat when they fit the chart `width` (px), else every label rotated 45° and cut at
// 96 px; when even a rotated line doesn't fit (a line over hundreds of days) ECharts' own interval skips some, and the
// data table lists them all. ponytail: ~7 px per character estimate; measure text if other scripts misjudge it.
export function fit(names: string[], width: number) {
  const slot = (width - 64) / Math.max(1, names.length); // ~64 px for the value axis labels
  if (!width || slot < 16) return {};
  if (Math.max(0, ...names.map((n) => n.length)) * 7 <= slot) return { interval: 0 };
  return { interval: 0, rotate: 45, width: 96, overflow: "truncate" };
}

// UI-DS-0 (D-77): design/abi-design-system/tokens/dataviz.css, fixed order navy, orange, ... (--viz-1..8). Colour never
// carries meaning alone (every chart has its data table), and a label on a fill takes that fill's ON colour (--viz-on-N).
// RAMP: map and heatmap low → high (--viz-seq-1..7, paper → navy); no data is NODATA grey, never on the ramp.
// UI-DS.7: the light and dark sets (dataviz.css `[data-abi-theme]`); `bg` is the card under every chart (app.css --bg).
// ON 3 and 7 are navy, not the DS white (white on teal/sage is 3.3:1, under AA for 12px labels; navy is 5.2:1)
const LIGHT = {
  palette: ["#1C2A42", "#E8743B", "#2A9D8F", "#7FA4D6", "#C9A227", "#8E5B8C", "#6E9A5B", "#A39684"],
  on: ["#FFFFFF", "#121B2A", "#121B2A", "#121B2A", "#121B2A", "#FFFFFF", "#121B2A", "#121B2A"],
  ramp: ["#D6DFEA", "#B6C5D8", "#91A6C2", "#6E86A8", "#46618A", "#2A4269", "#121B2A"],
  nodata: "#EEEEEE", hatch: "#C6C6C6", axis: "#7C8491", grid: "#E6E1D6", label: "#5B6270", value: "#3E4756",
  gain: "#1D5F57", loss: "#B04E1C", bg: "#FDFCF9", strong: "#121B2A", border: "#DDD8CC", accent: "#B04E1C",
};
export const VIZ = { light: LIGHT, dark: { ...LIGHT,
  palette: ["#C9D3E3", "#F08A55", "#4CC0B0", "#8FB0E0", "#E0BE4A", "#C08EBD", "#93C27F", "#B5AA98"],
  on: Array(8).fill("#121B2A"),
  ramp: ["#2F4363", "#3D5679", "#4E6B94", "#6784AE", "#86A0C7", "#AEC1DD", "#E2E9F3"],
  nodata: "#2C3038", hatch: "#454B55", axis: "#6B7A92", grid: "#26334A", label: "#B9C3D1", value: "#D5DCE6",
  gain: "#7FD3C6", loss: "#F29A68", bg: "#1B2638", strong: "#F6F4EF", border: "#2E3B52", accent: "#F29A68",
} };
export type Theme = keyof typeof VIZ;
export const PALETTE = LIGHT.palette;
type Viz = typeof LIGHT;

// the chart types; each one's name for screen readers is `chart.<type>` in the locale files
const CHARTS = ["bar", "line", "pie", "scatter", "map", "combo", "waterfall", "funnel", "heatmap", "treemap", "sankey"];
const named = (v: VisualData) => (v.title ? t("chart.titled", { type: t(`chart.${v.type}`), title: v.title }) : t(`chart.${v.type}`));
const byItem = ["pie", "scatter", "waterfall", "funnel", "heatmap", "treemap", "sankey"]; // tooltip per item, not per x
// ECharts renders tooltip formatter strings as HTML; member names are user data
const esc = (x: string) => x.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// RP-1 map: the vendored Natural Earth countries (world.json; names and ISO codes only)
export type World = { features: { properties: { name: string; iso_a2?: string; iso_a3?: string; alt?: string[] } }[] };

/** Each member of the first dimension on its country (by name, other name, ISO-2 or ISO-3, any case), or in
 * `unmatched` with its value: a member with no shape, a blank, or a second member of a country already taken. */
export function mapMatch(v: VisualData, world: World) {
  const by = new Map<string, string>();
  for (const { properties: p } of world.features)
    for (const k of [p.name, p.iso_a2, p.iso_a3, ...(p.alt ?? [])]) if (k) by.set(k.toLowerCase(), p.name);
  const [d] = dims(v);
  const m = Object.keys(v.measures ?? {})[0];
  const matched: { name: string; member: Value; value: Value }[] = [];
  const unmatched: { member: Value; value: Value }[] = [];
  for (const r of v.rows ?? []) {
    const member = r[col(v, d)], value = r[col(v, m)];
    const name = member == null ? undefined : by.get(String(member).toLowerCase());
    if (name && !matched.some((x) => x.name === name)) matched.push({ name, member, value });
    else unmatched.push({ member, value });
  }
  return { measure: m, matched, unmatched };
}

/** ECharts option for the chart types; kpi, table, matrix, slicer and text are HTML (see Report.tsx). A map needs
 * `world`, registered as the ECharts map "world"; its `selected` members (this visual's cross-filter) get an accent outline.
 * `theme`: the light or dark chart set (UI-DS.7). */
export function chartOption(v: VisualData, world?: World, selected: Value[] = [], width = 0, theme: Theme = "light"): Record<string, unknown> | null {
  if (!CHARTS.includes(v.type) || (v.type === "map" && !world)) return null;
  const c = VIZ[theme];
  const o = drawn(v, c, world, selected, width) as Record<string, any>;
  // UI-DS.7 chrome from the tokens: axes, grid, labels, legend, tooltip (ECharts' defaults are for a white page)
  const axis = (a: any): any => a && (Array.isArray(a) ? a.map(axis) : { ...a, axisLine: { lineStyle: { color: c.axis } }, axisTick: { lineStyle: { color: c.axis } },
    axisLabel: { ...a.axisLabel, color: c.label }, nameTextStyle: { color: c.label }, splitLine: { ...a.splitLine, lineStyle: { color: c.grid } } });
  return { ...o, xAxis: axis(o.xAxis), yAxis: axis(o.yAxis), textStyle: { color: c.value },
    legend: o.legend && { ...o.legend, textStyle: { color: c.value }, pageTextStyle: { color: c.value }, pageIconColor: c.value, pageIconInactiveColor: c.axis },
    tooltip: { ...o.tooltip, backgroundColor: c.bg, borderColor: c.border, textStyle: { color: c.value } } };
}

function drawn(v: VisualData, c: Viz, world?: World, selected: Value[] = [], width = 0) {
  const fmt = (m: string) => (x: unknown) => format(x as Value, v.format?.[m]);
  const value = (m: string) => valueAxis(v.format?.[m]);
  const { dim, cats, series, members } = pivot(v);
  const names = cats.map((c) => format(c, dim ? v.format?.[dim] : undefined));
  const base = {
    color: c.palette,
    animation: false, // no motion; also keeps headless checks deterministic
    tooltip: { trigger: byItem.includes(v.type) ? "item" : "axis" },
    // only bar, line and combo have one series per measure or member; the others label their own items
    legend: series.length > 1 && ["bar", "line", "combo"].includes(v.type) ? { type: "scroll", top: 0 } : undefined,
    // ECharts v6 AriaComponent: role="img" + this label on the chart element; the data table is the full alternative.
    aria: { enabled: true, label: { description: t("chart.aria", { name: named(v) }) } },
  };
  if (v.type === "map") {
    const { measure, matched, unmatched } = mapMatch(v, world!);
    const nums = matched.map((x) => x.value).filter((x): x is number => typeof x === "number");
    const [min, max] = [Math.min(0, ...nums), Math.max(0, ...nums)];
    const description = unmatched.length ? t("chart.ariaMap", { name: named(v), n: unmatched.length }) : t("chart.aria", { name: named(v) });
    return { ...base, aria: { enabled: true, label: { description } },
      tooltip: { trigger: "item", valueFormatter: (x: unknown) => (Number.isNaN(x) ? t("chart.noData") : fmt(measure)(x)) },
      visualMap: { type: "continuous", min, max, text: [fmt(measure)(max), fmt(measure)(min)], left: 8, bottom: 8, itemHeight: 80,
        inRange: { color: c.ramp }, textStyle: { color: c.label } },
      series: [{ type: "map", map: "world", name: label(v, measure), selectedMode: false, itemStyle: { areaColor: c.nodata, borderColor: c.axis, borderWidth: 0.5 },
        emphasis: { label: { show: false } }, data: matched.map((x) => ({ name: x.name, value: x.value,
          ...(selected.includes(x.member) && { itemStyle: { borderColor: c.accent, borderWidth: 2 } }) })) // DS: the selected country in the accent
          // no data: grey + diagonal hatch (dataviz.css --viz-nodata/-hatch), after the matched ones (picked reads by index)
          .concat(world!.features.map((f) => f.properties.name).filter((n) => !matched.some((x) => x.name === n))
            .map((name) => ({ name, value: NaN, itemStyle: { areaColor: c.nodata, decal: { symbol: "none", color: c.hatch, dashArrayX: [1, 0], dashArrayY: [2, 4], rotation: -Math.PI / 4 } } } as any))) }] };
  }
  if (v.type === "pie") {
    const s = series[0];
    const fill = colours(names, c.palette);
    return { ...base, series: [{ type: "pie", radius: ["40%", "70%"], label: { color: c.value }, data: names.map((name, i) => ({ name, value: s?.data[i], itemStyle: { color: fill[i] } })),
      tooltip: { valueFormatter: fmt(s?.measure) } }] };
  }
  if (v.type === "scatter") {
    const ms = Object.keys(v.measures ?? {});
    const at = (c: string) => col(v, c);
    const [mx, my] = ms.length > 1 ? ms : [dim!, ms[0]];
    const axis = (c: string) => ({ type: c in (v.measures ?? {}) ? "value" : "category", name: label(v, c), scale: true,
      ...(c in (v.measures ?? {}) ? value(c) : { data: names }) });
    // UI-1.4.3: with two measures, a second dimension colours the points (one series per member, e.g. an ML segment)
    const by = ms.length > 1 ? dims(v)[1] : undefined;
    const group = (r: Value[]) => (by ? format(member(v, r, by), v.format?.[by]) : "");
    const groups = [...new Set((v.rows ?? []).map(group))];
    const fill = by ? colours(groups, c.palette) : c.palette;
    return { ...base, grid: { left: 8, right: 48, top: by ? 40 : 32, bottom: 8 }, xAxis: axis(mx), yAxis: axis(my),
      legend: by ? { type: "scroll", top: 0 } : undefined,
      series: groups.map((g, i) => ({ type: "scatter", name: by ? g : undefined, itemStyle: { color: fill[i] },
        data: (v.rows ?? []).filter((r) => group(r) === g).map((r) => ({ name: dim ? format(r[at(dim)]) : "", member: dim ? r[at(dim)] : null,
          value: [mx === dim ? format(r[at(mx)]) : r[at(mx)], r[at(my)]] })) })) };
  }
  const m0 = series[0]?.measure;
  const xAxis = { type: "category", data: names, name: dim && label(v, dim), axisLabel: fit(names, width) };
  if (v.type === "waterfall") {
    // RP-8: each step floats from the running position; the positions only place the bars, the tooltip and the data
    // table show the engine's own values (principle 4). Candlestick [open, close, low, high]; colour plus sign in text.
    let at = 0;
    const data = (series[0]?.data ?? []).map((x) => { const a = at; at += x ?? 0; return [a, at, Math.min(a, at), Math.max(a, at)]; });
    return { ...base, grid: { left: 8, right: 8, top: 16, bottom: 8 }, xAxis, yAxis: { type: "value", ...value(m0) },
      tooltip: { trigger: "item", formatter: (p: { dataIndex: number }) => `${esc(names[p.dataIndex])}: ${esc(fmt(m0)(series[0].data[p.dataIndex]))}` },
      series: [{ type: "candlestick", name: series[0]?.name, data, barMaxWidth: 48,
        itemStyle: { color: c.gain, borderColor: c.gain, color0: c.loss, borderColor0: c.loss } }] };
  }
  if (v.type === "funnel") {
    const vals = series[0]?.data ?? [];
    return { ...base, series: [{ type: "funnel", name: series[0]?.name, min: 0, max: Math.max(0, ...vals.map((x) => x ?? 0)), // default max is 100
      data: names.map((name, i) => ({ name, value: vals[i] })), tooltip: { valueFormatter: fmt(m0) } }] };
  }
  const d1 = dims(v)[1];
  const rows = v.rows ?? [];
  const m = Object.keys(v.measures ?? {})[0];
  if (v.type === "heatmap") {
    // x: first dimension, y: second, colour: first measure; one cell per row, in row order (picked reads the row)
    const ys = [...new Map(rows.map((r) => [d1 ? r[col(v, d1)] : null, 0])).keys()];
    const nums = rows.map((r) => r[col(v, m)]).filter((x): x is number => typeof x === "number");
    const [min, max] = [Math.min(0, ...nums), Math.max(0, ...nums)];
    return { ...base, grid: { left: 8, right: 8, top: 16, bottom: 72 }, xAxis: { ...xAxis, splitArea: { show: true } },
      yAxis: { type: "category", data: ys.map((y) => format(y, v.format?.[d1])), name: d1 && label(v, d1), splitArea: { show: true } },
      visualMap: { type: "continuous", min, max, text: [fmt(m)(max), fmt(m)(min)], orient: "horizontal", left: "center", bottom: 0,
        itemHeight: 80, inRange: { color: c.ramp }, textStyle: { color: c.label } },
      tooltip: { trigger: "item", formatter: (p: { data: [number, number, Value] }) => `${esc(names[p.data[0]])}, ${esc(format(ys[p.data[1]]))}: ${esc(fmt(m)(p.data[2]))}` },
      series: [{ type: "heatmap", name: label(v, m), data: rows.map((r) => [cats.indexOf(r[col(v, dim!)]), ys.indexOf(d1 ? r[col(v, d1)] : null), r[col(v, m)] ?? "-"]) }] };
  }
  // treemap and Sankey draw sizes and widths: only positive values (the rest stay in the data table). `member` on an
  // item is the first-dimension value a click selects (picked).
  const pos = rows.filter((r) => typeof r[col(v, m)] === "number" && (r[col(v, m)] as number) > 0);
  const node = (r: Value[]) => ({ name: format(r[col(v, dim!)]), member: r[col(v, dim!)] });
  if (v.type === "treemap") {
    // fill and label colour are set on the same node: ECharts sorts tiles by value before it colours them by index, so
    // leaving the fill to the series palette would pair a label with another tile's fill. A parent's header band is
    // drawn in its borderColor (the card colour), so its upperLabel is the heading colour whatever the fill.
    const tile = (i: number) => ({ itemStyle: { color: c.palette[i % 8] }, label: { color: c.on[i % 8] } });
    const data = !d1 ? pos.map((r, i) => ({ ...node(r), value: r[col(v, m)], ...tile(i) }))
      : cats.map((k) => ({ name: format(k), member: k, children: pos.filter((r) => r[col(v, dim!)] === k)
        .map((r) => ({ name: format(r[col(v, d1)]), member: k, value: r[col(v, m)] })) })).filter((x) => x.children.length)
        .map((x, i) => ({ ...x, itemStyle: tile(i).itemStyle, upperLabel: { color: c.strong }, children: x.children.map((y) => ({ ...y, ...tile(i) })) }));
    return { ...base, series: [{ type: "treemap", name: label(v, m), data, nodeClick: false, roam: false, breadcrumb: { show: false }, itemStyle: { borderColor: c.bg }, // gaps and parent header bands
      top: 8, left: 8, right: 8, bottom: 8, levels: [{ itemStyle: { gapWidth: 2 } }, { upperLabel: { show: !!d1 } }],
      tooltip: { valueFormatter: fmt(m) } }] };
  }
  if (v.type === "sankey") {
    // flows first dimension → second; a second-dimension member named like a first-dimension one gets the dimension
    // label, so no node links to itself (ECharts refuses cycles)
    const left = new Set(pos.map((r) => node(r).name));
    const right = (r: Value[]) => { const n = format(r[col(v, d1)]); return left.has(n) ? `${n} (${label(v, d1)})` : n; };
    const nodes = [...new Map(pos.map((r) => [node(r).name, node(r)])).values(), ...[...new Set(pos.map(right))].map((name) => ({ name }))];
    return { ...base, series: [{ type: "sankey", name: label(v, m), label: { color: c.value }, data: nodes, left: 8, right: 96, top: 8, bottom: 8,
      links: pos.map((r) => ({ source: node(r).name, target: right(r), value: r[col(v, m)], member: r[col(v, dim!)] })),
      lineStyle: { color: "gradient", opacity: 0.4 }, tooltip: { valueFormatter: fmt(m) } }] };
  }
  const combo = v.type === "combo"; // RP-8: the first series as bars, the others as lines on a right axis (2nd format)
  const fill = members ? colours(series.map((s) => s.name), c.palette) : c.palette;
  // DEMO-1.4: bar labels that don't fit flat make horizontal bars (first category on top), not crowded 45° labels;
  // UI-1.4.2: so do bars too many for even one 45° label each (fit's `{}` at a known width), where ECharts would skip labels
  const across = v.type === "bar" && ("rotate" in xAxis.axisLabel || (!!width && !("interval" in xAxis.axisLabel)));
  return { ...base,
    grid: { left: 8, right: across ? 24 : 8, top: series.length > 1 ? 40 : 16, bottom: 8 }, // v6 keeps axis labels inside (outerBounds)
    xAxis: across ? { type: "value", ...value(m0) } : xAxis,
    yAxis: across ? { type: "category", data: names, inverse: true, axisLabel: { interval: 0, width: 140, overflow: "truncate" } } // every bar labelled
      : combo && series[1] ? [{ type: "value", ...value(m0) }, { type: "value", ...value(series[1].measure), splitLine: { show: false } }]
      : { type: "value", ...value(m0) },
    // owner, 5 Nov 2026: the export looks as the report does: a second dimension's members stack (render.py's chart)
    series: series.map((s, i) => ({ type: combo ? (i ? "line" : "bar") : v.type, ...(combo && i > 0 ? { yAxisIndex: 1 } : {}),
      ...(v.type === "bar" && members ? { stack: "members" } : {}),
      name: s.name, data: s.data, itemStyle: { color: fill[i % fill.length] }, lineStyle: { color: fill[i % fill.length] }, tooltip: { valueFormatter: fmt(s.measure) } })),
  };
}

/** The raw first-dimension value behind a chart click (null: nothing to filter on, e.g. a blank member). `item`: the
 * clicked ECharts data item (treemap and Sankey items carry `member`; a Sankey second-dimension node has none). */
export function picked(v: VisualData, dataIndex: number, world?: World, item?: unknown): Value {
  if (v.type === "map") return mapMatch(v, world!).matched[dataIndex]?.member ?? null; // regions without data come after
  if (v.type === "treemap" || v.type === "sankey") return (item as { member?: Value } | undefined)?.member ?? null;
  if (v.type === "scatter" && item && typeof item === "object" && "member" in item) return (item as { member: Value }).member; // series per member
  if (v.type === "scatter" || v.type === "heatmap") return (v.rows?.[dataIndex] ?? [])[col(v, dims(v)[0])] ?? null;
  if (period(v)) return null; // one period is two members (month and year), not one `=` filter
  const c = pivot(v).cats[dataIndex] ?? null;
  return c === TOTAL ? null : c; // a total isn't a member to filter on
}

/** RP-3: a visual gets every selection on the page except its own (a slicer doesn't filter itself). */
export const interactionFor = (sel: Record<string, Filter>, visual: string) =>
  Object.entries(sel).filter(([id]) => id !== visual).map(([, f]) => f);

/** A click on a member selects it (`=`); clicking the selected member again clears it. */
export function toggle(sel: Record<string, Filter>, visual: string, column: string, value: Value): Record<string, Filter> {
  const { [visual]: old, ...rest } = sel;
  return old?.op === "=" && old.column === column && old.value === value ? rest : { ...rest, [visual]: { column, op: "=", value } };
}

/** A slicer's checked members (`in`); none checked clears it. */
export function slice(sel: Record<string, Filter>, visual: string, column: string, values: Value[]): Record<string, Filter> {
  const { [visual]: _, ...rest } = sel;
  return values.length ? { ...rest, [visual]: { column, op: "in", value: values } } : rest;
}

/** WB-1 `edit_cells` body for a table or matrix visual cell (C-7: `v.edit` names the one table row behind it). `old`
 * is the value the visual showed, so a cell changed since is refused with "…Reload?" (WB-7). An empty input clears. */
export function cellEdit(v: VisualData, row: Value[], column: string, text: string) {
  const e = v.edit!;
  const at = (c: string) => col(v, c);
  return { table: e.table, edits: [{ key: e.key!.map((k) => row[at(k)]), column: e.columns![column], value: text === "" ? null : text, old: row[at(column)] }] };
}

/** How a table or matrix cell is edited: "row" = the one table row behind it (`edit_cells`), "total" = WB-11 spread
 * over the rows it sums (`edit_total`; a blank member can't be an `=` filter, so its cells stay read-only), else null. */
export function cellMode(v: VisualData, row: Value[], column: string): "row" | "total" | null {
  const e = v.edit, ds = dims(v);
  if (!e) return null;
  if (e.columns?.[column] && !ds.some((d) => rolled(v, row, d))) return "row";
  return e.total?.includes(column) && ds.every((d) => rolled(v, row, d) || row[col(v, d)] !== null) ? "total" : null;
}

/** WB-11 `edit_total` body: the visual's filters plus each member the cell doesn't roll up; `old` = the total shown (WB-7). */
export function totalEdit(v: VisualData, row: Value[], column: string, text: string) {
  const members = dims(v).filter((d) => !rolled(v, row, d)).map((d) => ({ column: d, op: "=", value: row[col(v, d)] }));
  return { measure: column, filters: [...v.edit!.filters!, ...members], value: text, old: row[col(v, column)] };
}
