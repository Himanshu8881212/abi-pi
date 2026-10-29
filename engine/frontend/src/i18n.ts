import { create } from "zustand";

// UI-5: every `locales/<lang>.json` is a language (a new language = a new file); en ships and is the per-key fallback.
// Plural forms are `key.one`/`key.other`/… picked by Intl.PluralRules on `vars.count`.
type Strings = Record<string, string>;
export const LOCALES: Record<string, Strings> = Object.fromEntries(
  Object.entries(import.meta.glob<Strings>("./locales/*.json", { eager: true, import: "default" })).map(([p, s]) => [p.slice(10, -5), s]),
);

const browser = () => globalThis.navigator?.languages ?? [];
// stored choice, else the first browser language we ship, else en
export function initial(): string {
  let stored: string | null = null;
  try {
    stored = globalThis.window?.localStorage.getItem("abi.lang") ?? null; // window: Node warns on a bare localStorage
  } catch {}
  return [stored, ...browser().map((l) => l.split("-")[0])].find((l) => l && l in LOCALES) ?? "en";
}

const html = (lang: string) => globalThis.document && (document.documentElement.lang = lang); // WCAG 3.1.1

export const useLang = create<{ lang: string; setLang: (lang: string) => void }>()((set) => ({
  lang: initial(),
  setLang: (lang) => {
    try {
      localStorage.setItem("abi.lang", lang);
    } catch {}
    html(lang);
    set({ lang });
  },
}));
html(useLang.getState().lang);

// the Intl locale: the browser's own variant of the picked language (en-GB keeps day/month order), else the tag
const locale = () => {
  const { lang } = useLang.getState();
  return browser().find((l) => l.split("-")[0] === lang) ?? lang;
};

export function t(key: string, vars: Record<string, string | number> = {}): string {
  const { lang } = useLang.getState();
  const get = (k: string) => LOCALES[lang]?.[k] ?? LOCALES.en?.[k];
  const n = vars.count;
  const text = (typeof n === "number" ? get(`${key}.${new Intl.PluralRules(locale()).select(n)}`) ?? get(`${key}.other`) : undefined) ?? get(key) ?? key;
  return text.replace(/\{(\w+)\}/g, (m, v: string) => (v in vars ? (typeof vars[v] === "number" ? fmtNumber(vars[v]) : vars[v]) : m));
}

export const fmtNumber = (v: number, o?: Intl.NumberFormatOptions) => new Intl.NumberFormat(locale(), o).format(v);
export const fmtDate = (v: string | number | Date, o?: Intl.DateTimeFormatOptions) => new Date(v).toLocaleString(locale(), o);
