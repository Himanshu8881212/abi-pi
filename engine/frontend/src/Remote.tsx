import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { branchesQuery, canEdit, confirmable, meQuery, post, remoteQuery } from "./api";
import { Confirm } from "./mel";
import { focusSoon } from "./store";
import { t } from "./i18n";

// PC-10 a conflict from `pull` (409): one measure, visual, page, step, value or whole file both sides changed.
export type Conflict = { file: string; kind: string; id: string | null; path: string; base: unknown; ours: unknown; theirs: unknown };
type Side = "ours" | "theirs";
export const resolutionKey = (c: Conflict) => `${c.file}#${c.path}`;
// The first conflict after `after` (wrapping) with no choice yet; -1 when every one is chosen.
export const nextUnresolved = (cs: Conflict[], picks: Record<string, Side>, after = -1) => {
  for (let n = 1; n <= cs.length; n++) {
    const i = (after + n) % cs.length;
    if (!picks[resolutionKey(cs[i])]) return i;
  }
  return -1;
};
const show = (v: unknown) => (v === null || v === undefined ? t("remote.absent") : typeof v === "string" ? v : JSON.stringify(v, null, 2));

/** PC-10 semantic conflicts side by side (base, ours, theirs) with an Ours/Theirs choice each; Apply once all are chosen. */
export function Conflicts({ branch, conflicts, picks, onPick, onApply, onCancel }: {
  branch: string; conflicts: Conflict[]; picks: Record<string, Side>;
  onPick: (i: number, side: Side) => void; onApply: () => void; onCancel: () => void;
}) {
  useEffect(() => document.getElementById("conflicts-h")?.focus(), []); // WCAG 2.4.3: the list replaces the Pull click
  return (
    <section aria-labelledby="conflicts-h" className="conflicts">
      <h3 id="conflicts-h" tabIndex={-1}>{t("remote.conflicts", { count: conflicts.length, branch })}</h3>
      <p className="muted">{t("remote.conflictsHelp")}</p>
      {conflicts.map((c, i) => {
        const key = resolutionKey(c);
        return (
          <fieldset key={key}>
            <legend>{t("remote.in", { what: c.kind + (c.id ? ` ${c.id}` : ""), file: c.file })} <code>{c.path}</code></legend>
            <div className="sides">
              {(["base", "ours", "theirs"] as const).map((s) => (
                <div key={s}>
                  <p className="muted">{s === "base" ? t("remote.base") : s === "ours" ? t("remote.ours") : t("remote.theirs", { branch })}</p>
                  <pre tabIndex={0}>{show(c[s])}</pre>{/* scrolls: keyboard-reachable (WCAG 2.1.1, axe scrollable-region-focusable) */}
                </div>
              ))}
            </div>
            <div className="row">
              {(["ours", "theirs"] as const).map((s) => (
                <label key={s}>
                  <input type="radio" id={`conflict-${i}-${s}`} name={`conflict-${i}`} checked={picks[key] === s} onChange={() => onPick(i, s)} />
                  {s === "ours" ? t("remote.keepOurs") : t("remote.takeTheirs")}
                </label>
              ))}
            </div>
          </fieldset>
        );
      })}
      <div className="row">
        <button id="conflicts-apply" className="primary" disabled={nextUnresolved(conflicts, picks) >= 0} onClick={onApply}>{t("remote.apply")}</button>
        <button onClick={onCancel}>{t("common.cancel")}</button>
      </div>
    </section>
  );
}

// PC-8 / PC-9 / PC-10 in the Changes tab (UI-4): the remote and environment, branches with ahead/behind, push, pull
// (another branch = promote by merge, confirmed with what it changes) and the per-object conflict view.
export default function Remote({ project }: { project: string }) {
  const qc = useQueryClient();
  const role = useQuery(meQuery(project)).data?.role;
  const settings = useQuery(remoteQuery(project));
  const remote = settings.data?.remote;
  const branches = useQuery({ ...branchesQuery(project), enabled: !!remote });
  const [form, setForm] = useState<{ url: string; username: string; secret: string } | null>(null); // null: as saved
  const [env, setEnv] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [alert, setAlert] = useState("");
  type Pull = { branch: string; resolutions?: Record<string, Side> };
  const [confirm, setConfirm] = useState<{ pull: Pull; from: string; message: string; token: string } | null>(null);
  const [conflicts, setConflicts] = useState<{ pull: Pull; from: string; list: Conflict[]; picks: Record<string, Side> } | null>(null);

  const editor = canEdit(role);
  const environment = settings.data?.environment ?? "main";
  const f = form ?? { url: remote?.url ?? "", username: remote?.username ?? "", secret: /\{secret:(\w+)\}/.exec(remote?.password ?? "")?.[1] ?? "" };
  const run = async (what: () => Promise<unknown>, text: string) => {
    setAlert("");
    try {
      await what();
      setStatus(text);
      for (const key of ["remote_settings", "remote_branches", "changes"]) qc.invalidateQueries({ queryKey: [key, project] });
      return true;
    } catch (e) {
      setAlert((e as Error).message);
      return false;
    }
  };
  // Pull may ask a confirmation (promote, roles: D-1) or come back with conflicts; the token goes back only on a click (MA-3).
  const pull = async (p: Pull, from: string, token?: string) => {
    setAlert("");
    setStatus("");
    const r = await confirmable("pull", { project, ...p }, token);
    if (r.confirm) return setConfirm({ pull: p, from, ...r.confirm }); // Confirm takes focus (autoFocus)
    setConfirm(null);
    const list = (r.json?.detail?.conflicts ?? null) as Conflict[] | null;
    if (!r.ok && list && !p.resolutions) {
      return setConflicts({ pull: p, from, list, picks: {} });
    }
    if (!r.ok) {
      focusSoon(p.resolutions ? "conflicts-apply" : from);
      return setAlert(t("remote.pullFailed", { error: r.error ?? "" }));
    }
    setConflicts(null);
    // UI-1.2-fix-1: the status shows once the branch table is fresh (a remote fetch: slow), so it never reads stale counts
    await qc.invalidateQueries({ queryKey: ["remote_branches", project] });
    qc.invalidateQueries(); // a pull can change every object of the project
    setStatus(p.branch === environment ? t("remote.pulled", { branch: p.branch }) : t("remote.promoted", { branch: p.branch, environment }));
    focusSoon(from); // WCAG 2.4.3: the Confirm button or the conflict view is gone
  };
  const pick = (i: number, side: Side) => {
    if (!conflicts) return;
    const key = resolutionKey(conflicts.list[i]);
    const first = !conflicts.picks[key];
    const picks = { ...conflicts.picks, [key]: side };
    setConflicts({ ...conflicts, picks });
    if (first) { // only a first choice moves on, so changing a choice (arrow keys) keeps focus in its group
      const next = nextUnresolved(conflicts.list, picks, i);
      focusSoon(next < 0 ? "conflicts-apply" : `conflict-${next}-ours`);
    }
  };

  if (settings.error) return <p role="alert">{t("remote.loadFailed", { error: settings.error.message })}</p>;
  if (!settings.data) return <p role="status">{t("remote.loading")}</p>;
  const ownerWhy = role === "owner" ? undefined : "env-why";
  const editWhy = editor ? undefined : "remote-why";

  return (
    <section aria-labelledby="remote-h" className="remote">
      <h2 id="remote-h">{t("remote.title")}</h2>
      <p>{remote ? <>{t("remote.remote")}<code>{remote.url}</code>{remote.username && <>{t("remote.as", { user: remote.username })}<code>{remote.password}</code></>}</>
        : t("remote.none")}{t("remote.envBranch")}<code>{environment}</code></p>
      {!editor && <p id="remote-why" className="muted">{t("remote.editorsOnly")}</p>}
      <form className="row" onSubmit={(e) => {
        e.preventDefault();
        run(() => post("set_remote", { project, ...(f.url && { url: f.url }), ...(f.username && { username: f.username, secret: f.secret }) }),
          f.url ? t("remote.saved") : t("remote.removed")).then((ok) => ok && setForm(null));
      }}>
        <label>{t("remote.url")} <input id="remote-url" value={f.url} placeholder={t("remote.urlExample")} onChange={(e) => setForm({ ...f, url: e.target.value })} /></label>
        <label>{t("remote.user")} <input id="remote-user" value={f.username} onChange={(e) => setForm({ ...f, username: e.target.value })} /></label>
        <label>{t("remote.secret")} <input id="remote-secret" value={f.secret} placeholder={t("remote.secretExample")} onChange={(e) => setForm({ ...f, secret: e.target.value })} /></label>
        <button type="submit" disabled={!editor} aria-describedby={editWhy}>{t("remote.save")}</button>
      </form>
      <div className="row">
        <label>{t("remote.environment")} <input id="env-branch" value={env ?? environment} onChange={(e) => setEnv(e.target.value)} /></label>
        <button disabled={role !== "owner"} aria-describedby={ownerWhy}
          onClick={() => run(() => post("set_environment", { project, branch: env ?? environment }), t("remote.envSet")).then((ok) => ok && setEnv(null))}>{t("remote.setEnv")}</button>
      </div>
      {ownerWhy && <p id="env-why" className="muted">{t("remote.ownerOnly")}</p>}
      {remote && (
        <>
          {branches.error ? <p role="alert" className="error">{t("remote.branchesFailed", { error: branches.error.message })}</p>
            : !branches.data ? <p role="status">{t("remote.branchesLoading")}</p>
            : (
              <table className="branches">
                <caption className="muted">{t("remote.branches")}</caption>
                <thead><tr><th scope="col">{t("remote.branch")}</th><th scope="col">{t("remote.ahead")}</th><th scope="col">{t("remote.behind")}</th><th scope="col">{t("remote.action")}</th></tr></thead>
                <tbody>
                  {branches.data.map((b) => (
                    <tr key={b.name}>
                      <th scope="row">{b.name}{b.name === environment ? t("remote.thisEnv") : ""}</th>
                      <td>{b.ahead}</td><td>{b.behind}</td>
                      <td>
                        <button id={`pull-${b.name}`} disabled={!editor} aria-describedby={editWhy} onClick={() => pull({ branch: b.name }, `pull-${b.name}`)}>
                          {b.name === environment ? t("remote.pull") : t("remote.promote", { branch: b.name })}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          <div className="row">
            <button id="push" disabled={!editor} aria-describedby={editWhy}
              onClick={() => run(() => post("push", { project }), t("remote.pushed", { environment }))}>{t("remote.push", { environment })}</button>
          </div>
        </>
      )}
      {confirm && (
        <Confirm label={t("remote.confirmPull")} message={confirm.message}
          ok={confirm.pull.branch === environment ? t("remote.confirmPull") : t("remote.confirmPromote")}
          onConfirm={() => pull(confirm.pull, confirm.from, confirm.token)} onCancel={() => setConfirm(null)} back={confirm.from} />
      )}
      {conflicts && (
        <Conflicts branch={conflicts.pull.branch} conflicts={conflicts.list} picks={conflicts.picks} onPick={pick}
          onApply={() => pull({ ...conflicts.pull, resolutions: conflicts.picks }, conflicts.from)}
          onCancel={() => { setConflicts(null); focusSoon(conflicts.from); }} />
      )}
      <p role="status" className="muted">{status}</p>
      {alert && <p role="alert" className="error">{alert}</p>}
    </section>
  );
}
