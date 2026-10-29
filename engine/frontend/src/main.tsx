import { QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { queryClient, signinReturn } from "./api";
import { App } from "./App";
import type { Link } from "./Account";
import { chatgptReturn } from "./ChatGpt";
import { sourceOauthReturn } from "./NewData";
import "./app.css";

const Print = lazy(() => import("./Print")); // lazy: it pulls in Report.tsx (echarts), which App also loads lazily
const Share = lazy(() => import("./Share"));
const params = new URLSearchParams(location.search); // `?print=<report>&…` is the report snapshot view (T-05.2)
const share = location.hash.match(/^#share=([\w-]+)$/)?.[1]; // `/#share=<token>` is a view-only share link (RP-6)
// `/embed/<kid>#embed=<JWT>` is an embedded report (RP-9); any other /embed/ page shows the invalid-embed message
const embed = location.pathname.startsWith("/embed/") ? (location.hash.match(/^#embed=([\w.-]+)$/)?.[1] ?? "") : null;
// SC-10 emailed links (`/#reset=`, `/#invite=`, `/#signup=`): the token leaves the URL before the first render and
// lives only in App's state
const m = location.hash.match(/^#(reset|invite|signup)=([\w-]+)$/);
const link = m && embed === null ? { kind: m[1] as Link["kind"], token: m[2] } : null;
if (link) history.replaceState(null, "", location.pathname + location.search);
// a share or emailed link pasted over an open app (or leaving one) changes only the hash; Docs anchors must not reload
addEventListener("hashchange", () => (share || embed !== null || /^#(share|reset|invite|signup)=/.test(location.hash)) && location.reload());

// SC-1: a return from a sign-in provider (`?signin=<result>`) is redeemed before the first render; never in an embed,
// which has no session (E-08.md decision 7); so is a return from OpenAI (`/#chatgpt=<outcome>`, AG-6)
if (embed === null) {
  await signinReturn();
  chatgptReturn();
  sourceOauthReturn(); // API-SRC.ui: back from an API source's OAuth consent (`/#source-oauth=<outcome>`)
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={queryClient}>
    {embed !== null ? <Suspense><Share token={embed} kind="embed" /></Suspense> : share ? <Suspense><Share token={share} /></Suspense> : params.has("print") ? <Suspense><Print params={params} /></Suspense> : <App link={link} />}
  </QueryClientProvider>,
);
