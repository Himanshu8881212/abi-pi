"""Project as code: JSON schemas, serialization, bundle, import, Git. PRD §6.6 PC-*."""

from pathlib import Path

# PC-1: every object file carries `"$schema": SCHEMA + "<type>.json"`, the $id of schemas/v1/<type>.json.
# `.invalid` (RFC 2606) never resolves, so no third party can serve a schema under it.
SCHEMA = "https://schema.abi.invalid/v1/"
SCHEMAS = Path(__file__).resolve().parents[3] / "schemas" / "v1"
ID = r"^[a-z][a-z0-9_-]{0,62}$"  # object IDs (measures, tools, agents, reports and their pages/visuals, flows): schemas/v1 `id`
