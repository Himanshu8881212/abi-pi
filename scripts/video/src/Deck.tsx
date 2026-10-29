import React from "react";
import { AbsoluteFill, Easing, interpolate, Sequence, useCurrentFrame, useVideoConfig } from "remotion";

// A silent explainer: a title card, one slide per report view (its numbers as ABI returned them, animated in) with
// a caption that says what to see, and an end card. The look is the app's ledger: paper, ink, hard rules, no radius.
export type Theme = { paper: string; sheet: string; ink: string; mute: string; faint: string; accent: string };
export type Series = { name: string; values: (number | null)[] };
export type Slide = {
  title: string; caption: string; kind: "kpi" | "bar" | "line" | "table";
  labels?: string[]; series?: Series[]; unit?: string;        // bar, line
  kpis?: { label: string; value: number | null; text: string }[]; // kpi: text is ABI's formatted value
  columns?: string[]; rows?: (string | number | null)[][]; dims?: number; // table: the first `dims` columns are labels
};
export type DeckProps = { title: string; subtitle: string; footer: string; theme: Theme; seconds: number; width: number; height: number; slides: Slide[] };

const FONT = '"Courier Prime", "IBM Plex Mono", "Courier New", monospace';
const compact = (v: number | null) => v === null ? "—" : Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(1) + "bn" : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(1) + "m"
  : Math.abs(v) >= 1e3 ? (v / 1e3).toFixed(1) + "k" : Number.isInteger(v) ? String(v) : v.toFixed(2);
const ease = (f: number, a: number, b: number) => interpolate(f, [a, b], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.out(Easing.cubic) });

const Card: React.FC<{ t: Theme; children: React.ReactNode; title: string; caption?: string; n?: string }> = ({ t, children, title, caption, n }) => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: t.paper, color: t.ink, fontFamily: FONT, padding: "64px 96px", display: "flex", flexDirection: "column", gap: 28 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 24, borderBottom: `3px solid ${t.ink}`, paddingBottom: 18, opacity: ease(f, 0, 12) }}>
        {n && <span style={{ background: t.ink, color: t.paper, padding: "4px 14px", fontSize: 30, fontWeight: 700 }}>{n}</span>}
        <span style={{ fontSize: 54, fontWeight: 700, textTransform: "uppercase", letterSpacing: 1 }}>{title}</span>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: "hidden", background: t.sheet, border: `3px solid ${t.ink}`, padding: 36, display: "flex" }}>{children}</div>
      {caption && <div style={{ fontSize: 36, lineHeight: 1.35, maxWidth: 1500, opacity: ease(f, 30, 50), transform: `translateY(${(1 - ease(f, 30, 50)) * 20}px)` }}>{caption}</div>}
    </AbsoluteFill>
  );
};

const Bars: React.FC<{ s: Slide; t: Theme }> = ({ s, t }) => {
  const f = useCurrentFrame(), series = s.series || [], labels = s.labels || [];
  const max = Math.max(1e-9, ...series.flatMap((x) => x.values.map((v) => v ?? 0)));
  const cols = [t.ink, t.accent, t.mute, t.faint];
  return (
    <div style={{ flex: 1, display: "flex", alignItems: "flex-end", gap: 18 }}>
      {labels.map((l, i) => (
        <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 10, height: "100%" }}>
          <div style={{ flex: 1, width: "100%", display: "flex", alignItems: "flex-end", gap: 6 }}>
            {series.map((x, j) => {
              const v = x.values[i] ?? 0, g = ease(f, 10 + i * 3, 40 + i * 3);
              return <div key={j} style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", alignItems: "center", height: "100%" }}>
                <span style={{ fontSize: 22, opacity: g }}>{compact(x.values[i])}</span>
                <div style={{ width: "100%", height: `${(v / max) * 85 * g}%`, background: cols[j % 4] }} />
              </div>;
            })}
          </div>
          <span style={{ fontSize: 22, color: t.mute, whiteSpace: "nowrap" }}>{l.length > 10 ? l.slice(0, 9) + "…" : l}</span>
        </div>
      ))}
    </div>
  );
};

const Line: React.FC<{ s: Slide; t: Theme }> = ({ s, t }) => {
  const f = useCurrentFrame(), series = s.series || [], labels = s.labels || [], W = 1600, H = 560;
  const vals = series.flatMap((x) => x.values.filter((v): v is number => v !== null)), max = Math.max(...vals, 0), min = Math.min(...vals, 0);
  const x = (i: number) => 60 + (i * (W - 220)) / Math.max(1, labels.length - 1), y = (v: number) => H - 60 - ((v - min) / (max - min || 1)) * (H - 100);
  const cols = [t.ink, t.accent, t.mute], g = ease(f, 8, 70), step = Math.ceil(labels.length / 8);
  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 12, minWidth: 0 }}>
      <div style={{ display: "flex", gap: 36, fontSize: 26 }}>{series.map((sr, j) => (
        <span key={j} style={{ display: "flex", alignItems: "center", gap: 10, color: cols[j % 3] }}>
          <span style={{ width: 36, height: 6, background: cols[j % 3] }} />{sr.name}</span>))}</div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", flex: 1, minHeight: 0 }} preserveAspectRatio="xMidYMid meet">
        <line x1={60} x2={W - 160} y1={H - 60} y2={H - 60} stroke={t.ink} strokeWidth={3} />
        {series.map((sr, j) => {
          const pts = sr.values.map((v, i) => (v === null ? null : [x(i), y(v), v])).filter(Boolean) as number[][];
          const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" "), len = pts.length * 260, last = pts[pts.length - 1];
          return <g key={j}>
            <path d={d} fill="none" stroke={cols[j % 3]} strokeWidth={8} strokeDasharray={len} strokeDashoffset={len * (1 - g)} />
            {last && <text x={last[0] + 16} y={last[1] + 10} fontSize={34} fill={cols[j % 3]} fontFamily={FONT} opacity={g}>{compact(last[2])}</text>}
          </g>;
        })}
        {labels.map((l, i) => (i % step === 0 || i === labels.length - 1 ?
          <text key={i} x={x(i)} y={H - 14} textAnchor="middle" fontSize={30} fill={t.mute} fontFamily={FONT}>{l}</text> : null))}
      </svg>
    </div>
  );
};

const Kpis: React.FC<{ s: Slide; t: Theme }> = ({ s, t }) => {
  const f = useCurrentFrame();
  return (
    <div style={{ flex: 1, display: "grid", gridTemplateColumns: `repeat(${Math.min(4, (s.kpis || []).length)}, minmax(0, 1fr))`, gap: 28, alignContent: "center" }}>
      {(s.kpis || []).map((k, i) => {
        const g = ease(f, 6 + i * 6, 40 + i * 6);
        return <div key={i} style={{ border: `3px solid ${t.ink}`, padding: 28, opacity: g, minWidth: 0 }}>
          <div style={{ fontSize: (s.kpis || []).length > 3 ? 64 : 88, fontWeight: 700, whiteSpace: "nowrap" }}>{g < 1 && k.value !== null ? compact(k.value * g) : k.text}</div>
          <div style={{ fontSize: 28, color: t.mute, textTransform: "uppercase", letterSpacing: 2, marginTop: 12 }}>{k.label}</div>
        </div>;
      })}
    </div>
  );
};

const Table: React.FC<{ s: Slide; t: Theme }> = ({ s, t }) => {
  const f = useCurrentFrame(), rows = (s.rows || []).slice(0, 10);
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 28, alignSelf: "flex-start" }}>
      <thead><tr>{(s.columns || []).map((c, i) => <th key={i} style={{ textAlign: i ? "right" : "left", borderBottom: `3px solid ${t.ink}`, padding: "10px 14px", textTransform: "uppercase", fontSize: 22 }}>{c}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => (
        <tr key={i} style={{ opacity: ease(f, 8 + i * 4, 24 + i * 4) }}>{r.map((v, j) => (
          <td key={j} style={{ textAlign: j >= (s.dims ?? 1) ? "right" : "left", borderBottom: `1px solid ${t.faint}`, padding: "10px 14px" }}>{typeof v === "number" && j >= (s.dims ?? 1) ? compact(v) : v ?? "—"}</td>))}</tr>))}
      </tbody>
    </table>
  );
};

export const Deck: React.FC<DeckProps> = (p) => {
  const { fps } = useVideoConfig(), per = Math.round(p.seconds * fps), t = p.theme;
  const Title: React.FC = () => {
    const f = useCurrentFrame();
    return <AbsoluteFill style={{ background: t.ink, color: t.paper, fontFamily: FONT, justifyContent: "center", padding: 120 }}>
      <div style={{ fontSize: 110, fontWeight: 700, textTransform: "uppercase", opacity: ease(f, 0, 20) }}>{p.title}</div>
      <div style={{ fontSize: 44, marginTop: 24, opacity: ease(f, 15, 35) }}>{p.subtitle}</div>
      <div style={{ width: `${ease(f, 20, 60) * 60}%`, height: 6, background: t.paper, marginTop: 48 }} />
    </AbsoluteFill>;
  };
  return (
    <AbsoluteFill style={{ background: t.paper }}>
      <Sequence durationInFrames={per}><Title /></Sequence>
      {p.slides.map((s, i) => (
        <Sequence key={i} from={per * (i + 1)} durationInFrames={per}>
          <Card t={t} title={s.title} caption={s.caption} n={String(i + 1).padStart(2, "0")}>
            {s.kind === "bar" ? <Bars s={s} t={t} /> : s.kind === "line" ? <Line s={s} t={t} /> : s.kind === "kpi" ? <Kpis s={s} t={t} /> : <Table s={s} t={t} />}
          </Card>
        </Sequence>
      ))}
      <Sequence from={per * (p.slides.length + 1)} durationInFrames={per}>
        <AbsoluteFill style={{ background: t.ink, color: t.paper, fontFamily: FONT, justifyContent: "center", alignItems: "center", fontSize: 40, textAlign: "center", padding: 120 }}>
          {p.footer}
        </AbsoluteFill>
      </Sequence>
    </AbsoluteFill>
  );
};
