import { t } from "./i18n";

// D-71 reasoning effort: the SDK's level names (backend `llm.LEVELS`) shown with a translated name and hint.
export const level = (lv: string) => ({
  none: t("effort.none"), minimal: t("effort.minimal"), low: t("effort.low"), medium: t("effort.medium"),
  high: t("effort.high"), xhigh: t("effort.xhigh"), max: t("effort.max"),
} as Record<string, string>)[lv] ?? lv;
const hint = (lv: string) => ({
  none: t("effort.hint.none"), minimal: t("effort.hint.minimal"), low: t("effort.hint.low"), medium: t("effort.hint.medium"),
  high: t("effort.hint.high"), xhigh: t("effort.hint.xhigh"), max: t("effort.hint.max"),
} as Record<string, string>)[lv] ?? "";

// one level as option text, "low (faster, fewer tokens)", the model's default marked
export const levelText = (lv: string, dflt: string | null) =>
  t(lv === dflt ? "effort.levelDefault" : "effort.level", { level: level(lv), hint: hint(lv) });

// A model the user can pick, with the effort levels it supports (none: no effort control) and its default level.
export type Choice = { id: string; name: string; efforts: string[]; default: string | null };
type Opt = { value: string; text: string };

// AG-6/D-71: ONE "model · effort" select ("GPT-6.1-Sol · low (faster, fewer tokens)"); a model with no levels (or no
// default level) is also listed alone. Values are `<model>|<effort>` (model IDs never hold "|"); `lead` options come
// first (workspace models / Automatic). A saved model or effort no longer offered stays listed until changed.
export function ModelEffort({ id, label, describedBy, choices, model, effort, onPick, lead }: {
  id: string; label: string; describedBy?: string; choices: Choice[]; model: string; effort: string | null;
  onPick: (model: string, effort: string | null) => void; lead: Opt[];
}) {
  const value = `${model}|${effort ?? choices.find((x) => x.id === model)?.default ?? ""}`;
  const opts = [...lead, ...choices.flatMap((x) => [
    ...(!x.efforts.length || x.default === null ? [{ value: `${x.id}|`, text: x.name }] : []),
    ...x.efforts.map((e) => ({ value: `${x.id}|${e}`, text: t("effort.option", { model: x.name, level: levelText(e, x.default) }) })),
  ])];
  if (!opts.some((o) => o.value === value)) opts.splice(lead.length, 0, { value, text: effort ? t("effort.option", { model, level: level(effort) }) : model });
  return (
    <label>{label}{" "}
      <select id={id} aria-describedby={describedBy} value={value}
        onChange={(e) => { const [m, ef] = e.target.value.split("|"); onPick(m, ef || null); }}>
        {opts.map((o) => <option key={o.value} value={o.value}>{o.text}</option>)}
      </select>
    </label>
  );
}
