#!/bin/sh
# Start ABI-PI in its container: its own engine (Postgres, engine, worker; scripts/engine.sh), the owner account on
# the first start, the first project, then the app.
#   ABI_USER / ABI_PASSWORD   the workspace owner: created on the first start; the app attaches projects as it
#   ABI_PI_PROJECT            the project to open first (created if missing; default main)
#   ABI_PI_WORKSPACE          its workspace (default main)
#   ABI_PI_LLM_BASE/ABI_PI_LLM_KEY  optional: one OpenAI-compatible model endpoint (or connect one in Settings)
# Secrets come from the environment only; nothing is written to the image or logged.
set -eu
cd /app
if [ -z "${ABI_USER:-}" ] || [ -z "${ABI_PASSWORD:-}" ]; then
  echo "Set ABI_USER and ABI_PASSWORD (at least 8 characters): the workspace owner, created on the first start." >&2
  exit 1
fi
P="${ABI_PI_PROJECT:-main}"
WS="${ABI_PI_WORKSPACE:-main}"
rm -f /data/run/*.pid  # a new container: pids from before a restart mean nothing (and may be reused)
scripts/engine.sh start
eval "$(scripts/engine.sh env)"
mkdir -p "$ABI_PI_WORKSPACES" /data/logs
[ -e logs ] || ln -s /data/logs logs
# the first start makes the owner (ABI's /api/setup answers 409 once one exists); the password stays in this process
python3 - <<'EOF'
import json, os, sys, urllib.error, urllib.request
body = json.dumps({"name": os.environ["ABI_USER"], "password": os.environ["ABI_PASSWORD"]}).encode()
try:
    urllib.request.urlopen(urllib.request.Request(os.environ["ABI_URL"] + "/api/setup", body, {"Content-Type": "application/json"}), timeout=30)
    print(f"created the owner account {os.environ['ABI_USER']}")
except urllib.error.HTTPError as e:
    if e.code != 409:
        sys.exit(f"Creating the owner account failed ({e.code}): ABI_PASSWORD needs at least 8 characters.")
EOF
if ! python3 -c "import sys; sys.path.insert(0, 'scripts'); import abi; sys.exit(0 if abi.folder('$P') else 1)"; then
  echo "attaching $P into workspace $WS"
  python3 scripts/abi.py project "$WS" "$P" > /dev/null
fi
exec python3 scripts/app.py "$P" 8790 --open
