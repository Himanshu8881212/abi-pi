#!/bin/sh
# ABI-PI's own engine: a copy of ABI's backend (engine/, see engine/README.md) with its own Postgres and data, so
# ABI-PI runs whether or not any other ABI does. Postgres listens on a socket in data/pg only (no TCP, no password:
# only this user can open it); the engine serves 127.0.0.1:8780; data, logs and pids live in data/ (git-ignored).
# A supervisor keeps the engine and its workers running: one that ends starts again within seconds.
#   scripts/engine.sh [start|stop|status|env]
#   scripts/engine.sh health [APP_URL]   exit 0 when the app (at APP_URL, if given), the engine and the workers serve
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
D=${ABI_PI_DATA:-$ROOT/data}
PORT=${ABI_ENGINE_PORT:-8780}
PGBIN=${PGBIN:-$(dirname "$(command -v pg_ctl 2>/dev/null || echo /opt/homebrew/opt/postgresql@17/bin/pg_ctl)")}
BIN=$ROOT/engine/backend/.venv/bin
WORKERS="worker worker_heavy"  # a worker per queue (abi/worker.py), each with run/NAME.pid and logs/NAME.log
HEAVY=${ABI_HEAVY_CONCURRENCY:-1}  # loads, query copies and ML trainings at a time: each may take DuckDB's memory limit

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
running() { for p in app $WORKERS; do ! alive $p || return 0; done; return 1; }
paused() { alive pause; }  # run/pause.pid names a backup that runs (scripts/backup.sh): no worker meanwhile
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$D/logs/supervisor.log"; }
up() {  # up NAME: the engine (app) or a worker in the background, its output added to its log, its pid in run/NAME.pid
  case $1 in
  app) HOME=${ENGINE_HOME:-$HOME} "$BIN/uvicorn" abi.app:app --host 127.0.0.1 --port "$PORT" --log-config abi/logging.json \
    >> "$D/logs/engine.log" 2>&1 & ;;
  # ticks, answers and light flow runs 4 at a time; heavy jobs never hold them up
  worker) HOME=${ENGINE_HOME:-$HOME} "$BIN/procrastinate" --app=abi.worker.app worker --queues=default --concurrency=4 \
    >> "$D/logs/worker.log" 2>&1 & ;;
  worker_heavy) HOME=${ENGINE_HOME:-$HOME} "$BIN/procrastinate" --app=abi.worker.app worker --queues=heavy --concurrency="$HEAVY" \
    >> "$D/logs/worker_heavy.log" 2>&1 & ;;
  esac
  echo $! > "$D/run/$1.pid"
}
supervise() {
  # Every second: the engine or a worker that ended starts again, at once, or 2, 4, ... 60 s later while it keeps
  # ending within a minute of its start; a line in logs/supervisor.log each time. No worker starts while a backup
  # holds the pause (one that started just as it began stops). TERM: each gets TERM, KILL after 5 s, then this ends.
  set +e  # nothing that fails in here ends the supervisor
  eval "$(env_)"
  cd "$ROOT/engine/backend"
  stop=
  trap 'stop=1' TERM
  for p in app $WORKERS; do eval "at_$p=0 delay_$p=0 due_$p="; ! alive $p || eval "at_$p=$(date +%s)"; done
  log "the supervisor started (pid $$)"
  while [ -z "$stop" ]; do
    now=$(date +%s)
    for p in app $WORKERS; do
      if [ -n "$stop" ] || alive $p; then continue; fi
      eval "at=\$at_$p delay=\$delay_$p due=\$due_$p"
      name=$p; [ $p != app ] || name=engine
      held=; [ $p = app ] || ! paused || held=1
      if [ "$at" -gt 0 ]; then  # it ended
        pid=$(cat "$D/run/$p.pid"); wait "$pid" 2>/dev/null; st=$?  # its status, when this supervisor started it
        if [ -z "$held" ] && [ $((now - at)) -lt 60 ]; then delay=$((delay < 2 ? 2 : delay < 30 ? delay * 2 : 60)); else delay=0; fi
        log "the $name (pid $pid) ended$([ $st = 127 ] || echo " with status $st") after $((now - at)) s;" \
          "it starts again $([ -n "$held" ] && echo "after the backup" || echo "in $delay s")"
        at=0 due=$((now + delay))
      fi
      if [ -z "$held" ] && [ "${due:-0}" -le "$now" ]; then
        up $p; at=$now; due=
        [ $p = app ] || ! paused || kill "$(cat "$D/run/$p.pid")"  # a backup began meanwhile
        log "started the $name (pid $(cat "$D/run/$p.pid"))"
      fi
      eval "at_$p=\$at delay_$p=\$delay due_$p=\$due"
    done
    [ -n "$stop" ] || sleep 1
  done
  for p in app $WORKERS; do ! alive $p || kill "$(cat "$D/run/$p.pid")"; done
  n=0; while running && [ $n -lt 25 ]; do sleep 0.2; n=$((n + 1)); done
  for p in app $WORKERS; do ! alive $p || kill -9 "$(cat "$D/run/$p.pid")"; rm -f "$D/run/$p.pid"; done
  rm -f "$D/run/supervisor.pid"
  log "stopped the engine and the workers"
}

case "${1:-start}" in
env) env_; exit 0 ;;
status)
  for p in supervisor app $WORKERS; do alive $p && echo "$p running" || echo "$p stopped"; done
  ! paused || echo "workers paused while process $(cat "$D/run/pause.pid") runs (a backup)"
  "$PGBIN/pg_ctl" -D "$D/pg" status >/dev/null 2>&1 && echo "postgres running" || echo "postgres stopped"
  exit 0 ;;
stop)
  if alive supervisor; then kill "$(cat "$D/run/supervisor.pid")"; while alive supervisor; do sleep 0.2; done; fi
  for p in app $WORKERS; do if alive $p; then kill "$(cat "$D/run/$p.pid")"; fi; done  # started without a supervisor
  for p in app $WORKERS; do while alive $p; do sleep 0.2; done; rm -f "$D/run/$p.pid"; done
  rm -f "$D/run/supervisor.pid"
  "$PGBIN/pg_ctl" -D "$D/pg" -m fast stop >/dev/null 2>&1 || true
  echo "engine stopped"
  exit 0 ;;
health)
  # the app answers (when its address is given), so does the engine's /api/ready, the supervisor runs and every worker
  # sent its heartbeat (procrastinate's, every 10 s) in the last 2 minutes, unless a backup paused them; else exit 1
  # saying which isn't so
  eval "$(env_)"
  [ -z "${2:-}" ] || curl -fs -m 5 -o /dev/null "$2" || { echo "The app doesn't answer at $2."; exit 1; }
  curl -fs -m 5 -o /dev/null "http://127.0.0.1:$PORT/api/ready" || { echo "The engine isn't ready: $D/logs/engine.log"; exit 1; }
  alive supervisor || { echo "Nothing restarts the engine and its workers: scripts/engine.sh start"; exit 1; }
  set -- $WORKERS
  paused || [ "$("$PGBIN/psql" -Atqc "SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '2 minutes'" 2>/dev/null)" -ge $# ] 2>/dev/null \
    || { echo "A worker sent no heartbeat in 2 minutes: $D/logs/worker*.log"; exit 1; }
  echo healthy
  exit 0 ;;
pause)  # pause PID: the workers stop (once their running jobs end) and none starts while process PID runs
  echo "${2:?the pid of the process that holds the pause}" > "$D/run/pause.pid"
  log "the workers pause while process $2 runs"
  for p in $WORKERS; do ! alive $p || kill "$(cat "$D/run/$p.pid")" || true; done
  for p in $WORKERS; do while alive $p; do sleep 0.2; done; done
  exit 0 ;;
supervise) supervise; exit 0 ;;
start) ;;
*) echo "usage: engine.sh [start|stop|status|health [APP_URL]|env]" >&2; exit 2 ;;
esac

[ -x "$BIN/python" ] || { echo "Run once: (cd engine/backend && uv sync --frozen --no-dev)" >&2; exit 1; }
[ -f "$ROOT/engine/backend/static/index.html" ] || {
  echo "Build the engine's UI once: (cd engine/frontend && npm ci && npx vite build --outDir ../backend/static --emptyOutDir)" >&2; exit 1; }
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
if ! alive supervisor; then
  nohup "$ROOT/scripts/engine.sh" supervise >> "$D/logs/supervisor.log" 2>&1 &
  echo $! > "$D/run/supervisor.pid"
fi
n=0
until curl -fs -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/ready"; do
  n=$((n + 1)); [ $n -lt 60 ] || { echo "The engine didn't start; see $D/logs/engine.log" >&2; exit 1; }; sleep 1
done
echo "engine on http://127.0.0.1:$PORT (data in $D)"
