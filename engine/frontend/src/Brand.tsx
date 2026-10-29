import { useEffect } from "react";

// SC-9 tenant branding (E-05.md decision 9): `logo` is an image URL (blob: or data:), shown only in <img>;
// `text_color` is the server's WCAG pick (black or white) for text on `color`.
export type Branding = { name: string; color: string | null; text_color: string | null; logo: string | null };

// The tenant name as the tab title and its colour as CSS variables on <html> (app.css: header bar, primary buttons;
// the focus ring keeps its fixed AA colour). Undone on unmount, so signing out restores the default look.
export function useBrand(b: Branding | null | undefined) {
  useEffect(() => {
    if (!b) return;
    const s = document.documentElement.style;
    const title = document.title;
    document.title = b.name;
    if (b.color) {
      s.setProperty("--brand", b.color);
      s.setProperty("--brand-text", b.text_color);
    }
    return () => {
      document.title = title;
      s.removeProperty("--brand");
      s.removeProperty("--brand-text");
    };
  }, [b]);
}

// The logo sits next to the visible name, so it is decorative (alt="", WCAG 1.1.1) and the name isn't read twice.
// `as="div"` inside the app's top bar, which is itself the page's one banner (UI-1.2).
export function BrandBar({ b, as: Tag = "header" }: { b: Branding; as?: "header" | "div" }) {
  return (
    <Tag className="brandbar">
      {b.logo && <img src={b.logo} alt="" />}
      <span>{b.name}</span>
    </Tag>
  );
}
