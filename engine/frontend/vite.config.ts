import { appendFileSync, readFileSync } from "node:fs";
import { defineConfig } from "vite";

// Dev only: the UI calls FastAPI on :8000. In the image, FastAPI serves the built UI itself.
// fs.allow "..": the steps editor imports ../schemas/v1/table.json (TR-1 step types).
// license: the minified bundle drops licence comments, so the build writes every bundled package's licence text to
// /licenses.md (MIT/BSD/ISC notice terms; D-98 audit in work/validation/LICENSE-AGPL.md). It lists node_modules
// packages only, so the Lucide icons inlined in src/icons.tsx (UI-DS.2) append their licence themselves.
export default defineConfig({
  server: { proxy: { "/api": "http://127.0.0.1:8000" }, fs: { allow: [".."] } },
  build: { license: { fileName: "licenses.md" } },
  plugins: [{
    name: "lucide-license",
    writeBundle: (o) => appendFileSync(`${o.dir}/licenses.md`,
      `\n## lucide-static - 0.460.0 (ISC, Feather-derived icons MIT; inlined in src/icons.tsx)\n\n${readFileSync(new URL("./src/icons.LICENSE", import.meta.url), "utf8")}`),
  }],
});
