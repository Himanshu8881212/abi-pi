#!/bin/sh
# ABI-PI's own engine: a copy of ABI's backend (engine/, see engine/README.md) with its own Postgres and data, so
# ABI-PI runs whether or not any other ABI does. Postgres listens on a socket in data/pg only (no TCP, no password:
# only this user can open it); the engine serves 127.0.0.1:8780; data, logs and pids live in data/ (git-ignored).
#   scripts/engine.sh [start|stop|status|env]
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
D=${ABI_PI_DATA:-$ROOT/data}
PORT=${ABI_ENGINE_PORT:-8780}
PGBIN=${PGBIN:-$(dirname "$(command -v pg_ctl 2>/dev/null || echo /opt/homebrew/opt/postgresql@17/bin/pg_ctl)")}
BIN=$ROOT/engine/backend/.venv/bin

env_() {
  cat <<EOF
export PGHOST=$D/pg PGUSER=abi PGDATABASE=abi
export ABI_LAKE_CATALOG=postgres: ABI_DATA_PATH=$D/lake ABI_FILES_PATH=$D/files ABI_PROJECTS_PATH=$D/projects
export ABI_ARTIFACTS_PATH=$D/artifacts ABI_SECRET_KEY_FILE=$D/secret.key ABI_SNAPSHOT_RETENTION_DAYS=7
export ABI_DUCKDB_MEMORY_LIMIT=\${ABI_DUCKDB_MEMORY_LIMIT:-1500MB} PYTHONPATH=$ROOT/engine/backend
export ABI_PUBLIC_URL=${ABI_PUBLIC_URL:-http://127.0.0.1:$PORT} ABI_APP_URL=http://127.0.0.1:$PORT ABI_URL=http://127.0.0.1:$PORT
EOF
}
alive() { [ -f "$D/run/$1.pid" ] && kill -0 "$(cat "$D/run/$1.pid")" 2>/dev/null; }

case "${1:-start}" in
env) env_; exit 0 ;;
status)
  for p in app worker worker_heavy; do alive $p && echo "$p running" || echo "$p stopped"; done
  "$PGBIN/pg_ctl" -D "$D/pg" status >/dev/null 2>&1 && echo "postgres running" || echo "postgres stopped"
  exit 0 ;;
stop)
  for p in app worker worker_heavy; do if alive $p; then kill "$(cat "$D/run/$p.pid")"; fi; done
  for p in app worker worker_heavy; do while alive $p; do sleep 0.2; done; rm -f "$D/run/$p.pid"; done
  "$PGBIN/pg_ctl" -D "$D/pg" -m fast stop >/dev/null 2>&1 || true
  echo "engine stopped"
  exit 0 ;;
start) ;;
*) echo "usage: engine.sh [start|stop|status|env]" >&2; exit 2 ;;
esac

[ -x "$BIN/python" ] || { echo "Run once: (cd engine/backend && uv sync --frozen --no-dev)" >&2; exit 1; }
[ -f "$ROOT/engine/backend/static/index.html" ] || {
  echo "Build the engine's UI once: (cd engine/frontend && npm ci && npx vite build --outDir ../backend/static --emptyOutDir)" >&2; exit 1; }
HEAVY=${ABI_HEAVY_CONCURRENCY:-1}  # loads, query copies and ML trainings at a time: each may take DuckDB's memory limit
case $HEAVY in 1|2) ;; *) echo "ABI_HEAVY_CONCURRENCY is 1 or 2 (heavy jobs at a time), not '$HEAVY'." >&2; exit 1 ;; esac
umask 077
mkdir -p "$D/run" "$D/logs" "$D/lake" "$D/files" "$D/projects" "$D/artifacts"
eval "$(env_)"
if [ ! -f "$D/pg/PG_VERSION" ]; then
  "$PGBIN/initdb" -D "$D/pg" -U abi -A trust -E UTF8 --no-locale >/dev/null
fi
if ! "$PGBIN/pg_ctl" -D "$D/pg" status >/dev/null 2>&1; then
  "$PGBIN/pg_ctl" -D "$D/pg" -l "$D/logs/pg.log" -o "-c listen_addresses='' -k $D/pg" -w start >/dev/null
fi
"$PGBIN/psql" -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname = 'abi'" | grep -q 1 || "$PGBIN/createdb" abi
cd "$ROOT/engine/backend"
HOME=${ENGINE_HOME:-$HOME} "$BIN/python" -c "import abi.ops, abi.db; abi.ops.setup(); abi.db.init(queue=True)"
# a worker per queue (abi/worker.py): ticks, answers and light flow runs 4 at a time; heavy jobs never hold them up
if ! alive worker; then
  HOME=${ENGINE_HOME:-$HOME} nohup "$BIN/procrastinate" --app=abi.worker.app worker --queues=default --concurrency=4 \
    > "$D/logs/worker.log" 2>&1 & echo $! > "$D/run/worker.pid"
fi
if ! alive worker_heavy; then
  HOME=${ENGINE_HOME:-$HOME} nohup "$BIN/procrastinate" --app=abi.worker.app worker --queues=heavy --concurrency="$HEAVY" \
    > "$D/logs/worker_heavy.log" 2>&1 & echo $! > "$D/run/worker_heavy.pid"
fi
if ! alive app; then
  HOME=${ENGINE_HOME:-$HOME} nohup "$BIN/uvicorn" abi.app:app --host 127.0.0.1 --port "$PORT" --log-config abi/logging.json > "$D/logs/engine.log" 2>&1 &
  echo $! > "$D/run/app.pid"
fi
n=0
until curl -fs -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/ready"; do
  n=$((n + 1)); [ $n -lt 60 ] || { echo "The engine didn't start; see $D/logs/engine.log" >&2; exit 1; }; sleep 1
done
echo "engine on http://127.0.0.1:$PORT (data in $D)"
