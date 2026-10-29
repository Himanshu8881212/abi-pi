import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/editor/editor.api";
import EditorWorker from "monaco-editor/editor/editor.worker?worker";
import JsonWorker from "monaco-editor/language/json/json.worker?worker";
// The JSON mode (tool editor) imports every editor feature (hover, suggest, codelens, …). Loaded lazily it registers
// their services after the first editor exists ("UNKNOWN service ICodeLensCache"), so load it before any editor.
import "monaco-editor/languages/features/json/jsonMode";
import "monaco-editor/languages/definitions/python/register"; // calculation tools' code (TL-5): tokenizer only, no worker

// Monaco is bundled from node_modules, never fetched from a CDN (the wrapper's default). One setup for every editor tab.
self.MonacoEnvironment = { getWorker: (_, label) => (label === "json" ? new JsonWorker() : new EditorWorker()) };
loader.config({ monaco });

export { monaco };
