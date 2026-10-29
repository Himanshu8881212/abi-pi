import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { download, post } from "./api";
import { focusSoon, useUi } from "./store";
import { t } from "./i18n";

type Explanation = { text: string | null; by: "agent" | "human" | null; outdated: boolean };
type Item = {
  object: string | null; title: string; facts: [string, string][]; table: { columns: string[]; rows: string[][] } | null;
  code: string | null; diagram: string | null; explanation: Explanation | null;
};
type ProjectDoc = {
  project: string; name: string; sections: { id: string; number: number; title: string; items: Item[] }[];
  review: ({ object: string } & Explanation)[];
};
export type Pkg = { generated: string; projects: ProjectDoc[]; files: Record<string, string>; drafted?: Record<string, { drafted: string[]; error?: string }> };

const anchor = (project: string, obj: string) => `doc-${project}-${obj}`;
// §8 two layers, marked on every item
export const mark = (e: Explanation) =>
  (e.by === "agent" ? t("docs.agent") : e.by === "human" ? t("docs.human") : t("docs.missing")) +
  (e.outdated ? t("docs.outdated") : "");

/** Object IDs ("table:orders") in a value become links to their docs (lineage map clickable in-app, §8 item 15). */
function Linked({ value, known, project }: { value: string; known: Set<string>; project: string }) {
  return value.split(", ").map((v, i) => (
    <Fragment key={i}>{i > 0 && ", "}{known.has(v) ? <a href={`#${anchor(project, v)}`}>{v}</a> : v}</Fragment>
  ));
}

export function DocItem({ item, known, project, when }: { item: Item; known: Set<string>; project: string; when: string }) {
  const e = item.explanation;
  return (
    <article className="doc-item" aria-labelledby={item.object ? anchor(project, item.object) : undefined}>
      <h3 id={item.object ? anchor(project, item.object) : undefined} tabIndex={-1}>
        {item.title} {item.object && <code className="muted">{item.object}</code>}
      </h3>
      <p className="layer">{t("docs.structural", { when })}</p>
      {item.facts.length > 0 && (
        <dl>{item.facts.map(([k, v]) => <div key={k}><dt>{k}</dt><dd><Linked value={v} known={known} project={project} /></dd></div>)}</dl>
      )}
      {item.table && (
        <table>
          <thead><tr>{item.table.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr></thead>
          <tbody>{item.table.rows.map((r, i) => (
            <tr key={i}>{r.map((v, j) => <td key={j}><Linked value={v} known={known} project={project} /></td>)}</tr>
          ))}</tbody>
        </table>
      )}
      {item.code && <pre aria-label={t("docs.definitionOf", { name: item.title })}><code>{item.code}</code></pre>}
      {item.diagram && <details><summary>{t("docs.diagram")}</summary><pre>{item.diagram}</pre></details>}
      {e && (
        <blockquote className={e.by === "agent" || e.outdated ? "explain warn" : "explain"}>
          <p className="layer">{t("docs.explanatory", { mark: mark(e) })}</p>
          {e.text && <p>{e.text}</p>}
        </blockquote>
      )}
    </article>
  );
}

// generate_docs input: handover (DC-7) only while the toggle is on, so the default request is unchanged
export const docsBody = (project: string | null, handover: boolean, more: object = {}) =>
  ({ ...(project ? { project } : {}), ...(handover ? { handover } : {}), ...more });

// DC-1 living docs (read now, per project or per object), DC-2 Generate Documentation, DC-3 Markdown bundle, DC-5 review.
// `project` null = workspace level: every project the user has a role in.
export default function Docs({ project }: { project: string | null }) {
  const qc = useQueryClient();
  const open = useUi((s) => s.open);
  const [object, setObject] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  const [handover, setHandover] = useState(false); // DC-7: off by default
  const where = project ?? t("docs.workspace");
  const live = useQuery({ queryKey: ["docs", project], queryFn: () => post<Pkg>("docs", project ? { project } : {}) });

  const act = async <T,>(name: string, body: object, then: (r: T) => string | Promise<string>) => {
    setAlert("");
    setBusy(true);
    try {
      setStatus(await then(await post<T>(name, body)));
    } catch (e) {
      const error = (e as Error).message;
      setAlert(name === "generate_docs" ? t("docs.generateFailed", { error }) : name === "save_docs" ? t("docs.saveFailed", { error }) : t("docs.reviewFailed", { error }));
    } finally {
      setBusy(false);
    }
  };
  const generate = () => act<Pkg>("generate_docs", docsBody(project, handover), (r) => {
    qc.setQueryData(["docs", project], r);
    const n = Object.values(r.drafted ?? {}).reduce((a, d) => a + d.drafted.length, 0);
    const failed = Object.values(r.drafted ?? {}).some((d) => d.error);
    return t("docs.generated", { n: r.projects.reduce((a, p) => a + p.sections.length, 0), when: r.generated }) +
      t("docs.drafted", { n: n }) + (failed ? t("docs.noLlm") : "");
  });
  const review = (p: string, obj: string) => act<object>("review_description", { project: p, object: obj }, () => {
    qc.invalidateQueries({ queryKey: ["docs"] });
    focusSoon("review-h"); // WCAG 2.4.3: the reviewed item and its button leave the list
    return t("docs.reviewed", { object: obj });
  });

  const pkg = live.data;
  if (live.error) return <p role="alert">{t("docs.loadFailed", { where, error: live.error.message })}</p>;
  if (!pkg) return <p role="status">{t("docs.loading", { where })}</p>;
  const review_ = pkg.projects.flatMap((p) => p.review.map((r) => ({ ...r, project: p.project })));
  const objects = pkg.projects.flatMap((p) => p.sections.flatMap((s) => s.items.flatMap((i) => (i.object ? [i.object] : []))));

  return (
    <div className="docs">
      <section aria-labelledby="docs-title" className="docs-head">
        <div className="section-head">
          <h2 id="docs-title">{t("docs.title", { where })}</h2>
          <div className="toolbar">
            <button className="primary" disabled={busy} onClick={generate}>{t("docs.generate")}</button>
            {project && <button disabled={busy} onClick={() => act<{ files: string[] }>("save_docs", { project }, (r) =>
              t("docs.savedFiles", { n: r.files.length }))}>{t("docs.save")}</button>}
            {project && (["html", "pdf"] as const).map((format) => ( // DC-3: a static site (zip) or a PDF, rendered by the server
              <button key={format} disabled={busy} onClick={() => act<{ link: string }>("generate_docs", docsBody(project, handover, { format }), async (r) => {
                await download(r.link, `${project}-docs.${format === "html" ? "zip" : "pdf"}`);
                return t("docs.exported", { format: format.toUpperCase() });
              })}>{t(`docs.${format}`)}</button>
            ))}
            {project && <button onClick={() => open(`${project}/#model`)}>{t("docs.openEr")}</button>}
          </div>
        </div>
        <label className="check"><input type="checkbox" checked={handover} onChange={(e) => setHandover(e.target.checked)} /> {t("docs.handover")}</label>
        <p role="status" className="muted">{busy ? t("common.working") : status}</p>
        {alert && <p role="alert" className="error">{alert}</p>}
        <details>
          <summary>{t("docs.bundle", { n: Object.keys(pkg.files).length })}</summary>
          <ul>{Object.entries(pkg.files).map(([path, text]) => (
            <li key={path}><a download={path.replaceAll("/", "_")} href={`data:text/markdown;charset=utf-8,${encodeURIComponent(text)}`}>{path}</a></li>
          ))}</ul>
        </details>
        {project && (
          <label>{t("docs.showObject")} <select value={object} onChange={(e) => setObject(e.target.value)}>
            <option value="">{t("docs.allObjects")}</option>
            {objects.map((o) => <option key={o} value={o}>{o}</option>)}
          </select></label>
        )}
      </section>
      <section aria-labelledby="review-h">
        <h2 id="review-h" tabIndex={-1}>{t("docs.forReview", { n: review_.length })}</h2>
        {review_.length === 0 && <p className="muted">{t("docs.allReviewed")}</p>}
        <ul className="review">{review_.map((r) => (
          <li key={r.project + r.object}>
            <a href={`#${anchor(r.project, r.object)}`}>{r.object}</a> <span className="muted">({mark(r)})</span>
            {r.text && <button className="sm" disabled={busy} onClick={() => review(r.project, r.object)}>{t("docs.markReviewed", { object: r.object })}</button>}
          </li>
        ))}</ul>
      </section>
      {pkg.projects.map((p) => {
        const known = new Set(p.sections.flatMap((s) => s.items.flatMap((i) => (i.object ? [i.object] : []))));
        const sections = p.sections.map((s) => ({ ...s, items: s.items.filter((i) => !object || i.object === object) })).filter((s) => s.items.length);
        return (
          <section key={p.project} aria-label={t("docs.title", { where: p.name })}>
            {!project && <h2>{p.name} <code className="muted">{p.project}</code></h2>}
            <nav aria-label={t("docs.sectionsOf", { name: p.name })}><ol className="toc">{sections.map((s) => (
              <li key={s.id}><a href={`#sec-${p.project}-${s.id}`}>{s.number}. {s.title}</a></li>
            ))}</ol></nav>
            {sections.map((s) => (
              <section key={s.id} aria-labelledby={`sec-${p.project}-${s.id}`}>
                <h2 id={`sec-${p.project}-${s.id}`} tabIndex={-1}>{s.number}. {s.title}</h2>
                {s.items.map((i, n) => <DocItem key={i.object ?? n} item={i} known={known} project={p.project} when={pkg.generated} />)}
              </section>
            ))}
          </section>
        );
      })}
    </div>
  );
}
