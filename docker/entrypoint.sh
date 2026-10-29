#!/bin/sh
# Start ABI-PI in a container: point the agent's MCP at ABI, attach the project on first start, serve the app.
#   ABI_URL               ABI's address as the container sees it (default http://host.docker.internal:8770)
#   ABI_PI_WORKSPACE      workspace folder for the project (default main)
#   ABI_PI_PROJECT        the ABI project to open first (required); attached on first start (created in ABI if missing)
#   ABI_USER/ABI_PASSWORD the service account the app syncs project copies with (or ABI_TOKEN); people sign in as themselves
#   ABI_PI_LLM_BASE/ABI_PI_LLM_KEY  the model proxy (OpenAI-compatible) the agent uses
# Secrets come from the environment only; nothing is written to the image or logged.
set -eu
: "${ABI_PI_PROJECT:?set ABI_PI_PROJECT to the ABI project to open}"
export ABI_URL="${ABI_URL:-http://host.docker.internal:8770}"
WS="${ABI_PI_WORKSPACE:-main}"
cd /app
if ! curl -fsS -m 10 -o /dev/null "$ABI_URL/api/health"; then
  echo "Can't reach ABI at $ABI_URL (set ABI_URL to ABI's address as this container sees it)." >&2
  exit 1
fi
if [ ! -d "$ABI_PROJECTS_PATH" ] || [ -z "$(ls -A "$ABI_PROJECTS_PATH" 2>/dev/null)" ]; then
  echo "ABI's projects folder isn't mounted at $ABI_PROJECTS_PATH (mount it read-only: ABI_PROJECTS_DIR in .env)." >&2
  exit 1
fi
if [ -z "${ABI_TOKEN:-}" ] && { [ -z "${ABI_USER:-}" ] || [ -z "${ABI_PASSWORD:-}" ]; }; then
  echo "Set ABI_USER and ABI_PASSWORD (a service account) so the app can sync project copies." >&2
  exit 1
fi
mkdir -p "$ABI_PI_WORKSPACES" /data/logs
[ -e logs ] || ln -s /data/logs logs
# the agent's MCP server is ABI's, wherever it runs
python3 - <<'EOF'
import json, os
p = ".pi/mcp.json"
m = json.load(open(p))
m["mcpServers"]["abi"]["url"] = os.environ["ABI_URL"].rstrip("/") + "/api/mcp"
json.dump(m, open(p, "w"), indent=1)
EOF
if ! python3 -c "import sys; sys.path.insert(0, 'scripts'); import abi; sys.exit(0 if abi.folder('$ABI_PI_PROJECT') else 1)"; then
  echo "attaching $ABI_PI_PROJECT into workspace $WS"
  python3 scripts/abi.py project "$WS" "$ABI_PI_PROJECT" > /dev/null
fi
exec python3 scripts/app.py "$ABI_PI_PROJECT" 8790 --open
