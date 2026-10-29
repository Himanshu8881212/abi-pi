import { useQueries, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { brandingQuery, post, reportsQuery } from "./api";
import { BrandBar, useBrand } from "./Brand";
import { t } from "./i18n";
import type { Filter, VisualData } from "./charts";
import { Body, worldQuery, type Spec } from "./Report";

// T-05.2 print view for report snapshots (TL-2): pages in the given order (MULTI-EXPORT.1: one section each, a sheet
// apiece in the PDF, its <h1> an outline entry), or one visual of a page, with the snapshot's filters sent as
// `interaction` (ANDed after the report's own). Data only through visual_data (principle 4). The server's headless
// Chromium waits for `data-ready`, then reads `data-error` (a visual failed) and `data-measures` (D-24: what was drawn).
export default function Print({ params: q }: { params: URLSearchParams }) {
  const [project, report, visual] = ["project", "print", "visual"].map((k) => q.get(k) ?? "");
  const ids = (q.get("pages") ?? q.get("page") ?? "").split(",");
  const filters = JSON.parse(q.get("filters") || "[]") as Filter[];
  const reps = useQuery(reportsQuery(project));
  const brand = useQuery(brandingQuery(project)); // SC-9: the project's tenant, whoever renders it
  useBrand(brand.data);
  const r = reps.data?.find((x) => x.id === report);
  const pages = ids.map((id) => ({ id, p: r?.pages.find((x) => x.id === id) }))
    .map(({ id, p }) => ({ id, p, visuals: ordered((p?.visuals ?? []).filter((v) => !visual || v.id === visual)) }));
  const all = pages.flatMap(({ id, visuals }) => visuals.map((v) => ({ page: id, v })));
  const data = useQueries({
    queries: all.map(({ page, v }) => ({ queryKey: ["visual", project, report, page, v.id, filters],
      queryFn: () => post<VisualData>("visual_data", { project, report, page, visual: v.id, interaction: filters }) })),
  });
  // a map draws once its shapes have loaded (Body's MapVisual shares this query)
  const maps = data.some((q) => q.data?.type === "map");
  const world = useQuery({ ...worldQuery, enabled: maps });
  const empty = pages.find((x) => !x.visuals.length)?.id;
  const error = reps.error?.message ?? (reps.data && empty !== undefined ? (visual ? t("print.noVisual", { page: empty, visual, report }) : t("print.noPage", { page: empty, report })) : undefined)
    ?? data.find((q) => q.error)?.error?.message ?? world.error?.message ?? brand.error?.message;
  const done = !!error || (all.length > 0 && data.every((q) => q.data) && (!maps || !!world.data) && brand.isSuccess);
  // Set after the children's effects (charts draw in theirs), so the marker means "drawn", not just "fetched".
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(done), [done]);
  let at = 0; // each page's slice of `data`
  const measures = [...new Set(data.flatMap((q) => Object.keys(q.data?.measures ?? {})))];
  return (
    <>
      {brand.data && <BrandBar b={brand.data} />}
      <main className="print" data-ready={ready ? "" : undefined} data-error={ready ? error : undefined} data-measures={JSON.stringify(measures)}>
        {error && <p role="alert">{error}</p>}
        {pages.map(({ id, p, visuals }, i) => {
          const d = data.slice(at, (at += visuals.length));
          return (
            <section key={i} className="print-page">
              {/* one page keeps "Report · Page"; several are titled by page, each title one PDF outline entry */}
              <h1>{pages.length > 1 ? p?.name ?? id : `${r?.name ?? report}${p ? ` · ${p.name}` : ""}`}</h1>
              <Grid visuals={visuals} data={d.map((q) => q.data)} idp={pages.length > 1 ? `${i}-` : ""} />
            </section>
          );
        })}
      </main>
    </>
  );
}

// RP-4 layout order: top to bottom, left to right
export const ordered = (vs: Spec[]) => [...vs].sort((a, b) => (a.layout?.y ?? 1e3) - (b.layout?.y ?? 1e3) || (a.layout?.x ?? 0) - (b.layout?.x ?? 0));

// The visuals of one page in layout order, read-only (the print view and the share view, RP-6). `idp` keeps heading
// ids unique when several pages share a document (visual IDs are unique per page only).
export function Grid({ visuals, data, idp = "" }: { visuals: Spec[]; data: (VisualData | undefined)[]; idp?: string }) {
  return (
    <div className="print-grid">
      {visuals.map((v, i) => {
        const d = data[i];
        const l = v.layout ?? { x: 0, w: 6, h: 4 };
        return (
          <section key={v.id} className="visual" aria-labelledby={`v-${idp}${v.id}-h`} style={{ gridColumn: `${l.x + 1} / span ${l.w}`, gridRow: `span ${l.h}` }}>
            <h2 id={`v-${idp}${v.id}-h`}>{v.title ?? (d?.measures ? Object.values(d.measures).join(", ") : v.id)}</h2>
            <div className="vbody">{d && <Body d={d} onSlice={() => {}} />}</div>
          </section>
        );
      })}
    </div>
  );
}
