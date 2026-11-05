#!/bin/sh
# ABI-PI's own engine: a copy of ABI's backend (engine/, see engine/README.md) with its own Postgres and data, so
# ABI-PI runs whether or not any other ABI does. Postgres listens on a socket in data/pg only (no TCP, no password:
# only this user can open it); the engine serves 127.0.0.1:8780; data, logs and pids live in data/ (git-ignored).
# A supervisor keeps the engine and its workers running: one that ends starts again within seconds. The heavy worker
# (M4.1) runs only while the heavy queue has work: it starts when a job is queued and stops ABI_RUNNER_IDLE seconds
# after the queue empties, so its memory goes back; none starts while a runner elsewhere beats (run/runner.beat).
#   scripts/engine.sh [start|stop|status|env]
#   scripts/engine.sh health [APP_URL]   exit 0 when the app (at APP_URL, if given), the engine and the workers serve
#   scripts/engine.sh runner             ABI_ROLE=runner (compose's runner profile): only the heavy jobs, in the
#                                        foreground, on this data's Postgres; no engine, no app, no Postgres server
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
D=${ABI_PI_DATA:-$ROOT/data}
PORT=${ABI_ENGINE_PORT:-8780}
PGBIN=${PGBIN:-$(dirname "$(command -v pg_ctl 2>/dev/null || echo /opt/homebrew/opt/postgresql@17/bin/pg_ctl)")}
BIN=$ROOT/engine/backend/.venv/bin
WORKERS="worker worker_heavy"  # a worker per queue (abi/worker.py), each with run/NAME.pid and logs/NAME.log
HEAVY=${ABI_HEAVY_CONCURRENCY:-1}  # loads, query copies and ML trainings at a time: each may take DuckDB's memory limit
IDLE=${ABI_RUNNER_IDLE:-180}  # seconds the heavy queue is empty before its worker stops
ROLE=${ABI_ROLE:-all}  # all: everything (the one container); runner: the heavy jobs only (engine.sh runner)
PARTS="app $WORKERS" SV=supervisor  # what this supervisor keeps running, and its name (pid and log files)

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
running() { for p in $PARTS; do ! alive $p || return 0; done; return 1; }
started() { echo $(ps -o lstart= -p "$1" 2>/dev/null); }  # a process's start time: a PID reused later has another
paused() {  # run/pause.pid names a backup that runs (scripts/backup.sh) and when it started: no worker meanwhile.
  # A runner (another container: another PID namespace) can't check the process, so the file alone holds it; the
  # supervisor of the one that wrote it removes a stale one.
  [ -f "$D/run/pause.pid" ] && read -r pause_pid pause_at < "$D/run/pause.pid" && [ -n "$pause_at" ] && {
    [ $ROLE = runner ] || [ "$(started "$pause_pid")" = "$pause_at" ]; }
}
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }
side() { [ -f "$D/run/runner.beat" ] && [ $(($(date +%s) - $(mtime "$D/run/runner.beat"))) -lt 15 ]; }  # a runner beats
heavy() {  # "todo doing": heavy jobs due now, and heavy jobs a live worker runs
  "$PGBIN/psql" -Atqc "SELECT count(*) FILTER (WHERE status = 'todo' AND coalesce(scheduled_at, now()) <= now()) || ' ' ||
    count(*) FILTER (WHERE status = 'doing' AND worker_id IN (SELECT id FROM procrastinate_workers
    WHERE last_heartbeat > now() - interval '30 seconds')) FROM procrastinate_jobs WHERE queue_name = 'heavy'" 2>/dev/null || echo "0 0"
}
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$D/logs/$SV.log"; }
up() {  # up NAME: the engine (app) or a worker in the background, its output added to its log, its pid in run/NAME.pid
  case $1 in
  app) HOME=${ENGINE_HOME:-$HOME} "$BIN/uvicorn" abi.app:app --host 127.0.0.1 --port "$PORT" --log-config abi/logging.json \
    >> "$D/logs/engine.log" 2>&1 & ;;
  # ticks, answers and light flow runs 4 at a time; heavy jobs never hold them up
  worker) HOME=${ENGINE_HOME:-$HOME} "$BIN/procrastinate" --app=abi.worker.app worker --queues=default --concurrency=4 \
    >> "$D/logs/worker.log" 2>&1 & ;;
  worker_heavy|runner) HOME=${ENGINE_HOME:-$HOME} "$BIN/procrastinate" --app=abi.worker.app worker --queues=heavy --concurrency="$HEAVY" \
    >> "$D/logs/$1.log" 2>&1 & ;;
  esac
  echo $! > "$D/run/$1.pid"
}
supervise() {
  # Every second: the engine or a worker that ended starts again, at once, or 2, 4, ... 60 s later while it keeps
  # ending within a minute of its start; a line in logs/supervisor.log each time. No worker starts while a backup
  # holds the pause (one that started just as it began stops). TERM: each gets TERM, KILL after 5 s, then this ends.
  # The heavy worker (the runner's, in ABI_ROLE=runner) starts when a heavy job is due and gets TERM (it finishes what
  # it runs) once the queue has been empty IDLE seconds, for a backup's pause, or when a runner elsewhere beats.
  set +e  # nothing that fails in here ends the supervisor
  eval "$(env_)"
  cd "$ROOT/engine/backend"
  stop= q=0 todo=0 doing=0
  trap 'stop=1' TERM
  for p in $PARTS; do eval "at_$p=0 delay_$p=0 due_$p= idle_$p= quit_$p="; ! alive $p || eval "at_$p=$(date +%s)"; done
  log "the supervisor started (pid $$)"
  while [ -z "$stop" ]; do
    now=$(date +%s)
    [ $ROLE = all ] || : > "$D/run/runner.beat"  # the main role takes no heavy job while this is fresh
    [ $ROLE = runner ] || [ ! -f "$D/run/pause.pid" ] || paused || { rm -f "$D/run/pause.pid"; log "removed a stale pause"; }
    [ $((now - q)) -lt 2 ] || { set -- $(heavy) 0 0; todo=$1 doing=$2 q=$now; }  # one small query every 2 s
    for p in $PARTS; do
      [ -z "$stop" ] || continue
      eval "at=\$at_$p delay=\$delay_$p due=\$due_$p idle=\$idle_$p quit=\$quit_$p"
      name=$p; [ $p != app ] || name=engine
      held=; [ $p = app ] || ! paused || held=1
      lazy=; [ $p != worker_heavy ] && [ $p != runner ] || lazy=1
      [ $p != worker_heavy ] || ! side || held=runner
      if alive $p; then
        if [ -n "$lazy" ] && [ -z "$quit" ]; then  # one TERM only: procrastinate stops a running job at the second
          if [ "$held" = runner ] || { [ -n "$held" ] && [ $ROLE = runner ]; }; then quit=held  # else engine.sh pause stops it
          elif [ $((todo + doing)) -gt 0 ]; then idle=
          elif [ -z "$idle" ]; then idle=$now
          elif [ $((now - idle)) -ge "$IDLE" ]; then quit=idle
          fi
          [ -z "$quit" ] || kill "$(cat "$D/run/$p.pid")"
        fi
        eval "idle_$p=\$idle quit_$p=\$quit"
        continue
      fi
      if [ "$at" -gt 0 ]; then  # it ended
        pid=$(cat "$D/run/$p.pid"); wait "$pid" 2>/dev/null; st=$?  # its status, when this supervisor started it
        if [ -n "$quit" ]; then
          delay=0
          log "the $name (pid $pid) stopped after $((now - at)) s: $([ $quit = idle ] && echo "the heavy queue was empty $IDLE s" ||
            echo "it starts again after the backup, or not while a runner takes the heavy jobs")"
        else
          if [ -z "$held" ] && [ $((now - at)) -lt 60 ]; then delay=$((delay < 2 ? 2 : delay < 30 ? delay * 2 : 60)); else delay=0; fi
          log "the $name (pid $pid) ended$([ $st = 127 ] || echo " with status $st") after $((now - at)) s;" \
            "it starts again $([ -n "$held" ] && echo "after the backup" || echo "in $delay s")"
        fi
        at=0 due=$((now + delay)) idle= quit=
      fi
      if [ -z "$held" ] && [ "${due:-0}" -le "$now" ] && { [ -z "$lazy" ] || [ "$todo" -gt 0 ]; }; then
        up $p; at=$now; due=
        [ $p = app ] || ! paused || kill "$(cat "$D/run/$p.pid")"  # a backup began meanwhile
        log "started the $name (pid $(cat "$D/run/$p.pid"))"
      fi
      eval "at_$p=\$at delay_$p=\$delay due_$p=\$due idle_$p=\$idle quit_$p=\$quit"
    done
    [ -n "$stop" ] || sleep 1
  done
  for p in $PARTS; do ! alive $p || kill "$(cat "$D/run/$p.pid")"; done
  n=0; while running && [ $n -lt 25 ]; do sleep 0.2; n=$((n + 1)); done
  for p in $PARTS; do ! alive $p || kill -9 "$(cat "$D/run/$p.pid")"; rm -f "$D/run/$p.pid"; done
  rm -f "$D/run/$SV.pid"; [ $ROLE = all ] || rm -f "$D/run/runner.beat"
  log "stopped the engine and the workers"
}
settings() {
  case $HEAVY in 1|2) ;; *) echo "ABI_HEAVY_CONCURRENCY is 1 or 2 (heavy jobs at a time), not '$HEAVY'." >&2; exit 1 ;; esac
  case $IDLE in ''|*[!0-9]*) echo "ABI_RUNNER_IDLE is a number of seconds, not '$IDLE'." >&2; exit 1 ;; esac
  case $ROLE in all|runner) ;; *) echo "ABI_ROLE is all or runner, not '$ROLE'." >&2; exit 1 ;; esac
}

case "${1:-start}" in
env) env_; exit 0 ;;
status)
  for p in supervisor app $WORKERS; do alive $p && echo "$p running" || echo "$p stopped"; done
  ! side || echo "a runner takes the heavy jobs (it beats in run/runner.beat)"
  ! paused || echo "workers paused while process $pause_pid runs (a backup)"
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
  if [ $ROLE = runner ]; then  # its loop beats and Postgres answers (its worker runs only while there is work)
    side || { echo "The runner's loop isn't running: $D/logs/runner_supervisor.log"; exit 1; }
    "$PGBIN/psql" -Atqc "SELECT 1" >/dev/null 2>&1 || { echo "The runner can't reach Postgres in $D/pg."; exit 1; }
    echo healthy; exit 0
  fi
  [ -z "${2:-}" ] || curl -fs -m 5 -o /dev/null "$2" || { echo "The app doesn't answer at $2."; exit 1; }
  curl -fs -m 5 -o /dev/null "http://127.0.0.1:$PORT/api/ready" || { echo "The engine isn't ready: $D/logs/engine.log"; exit 1; }
  alive supervisor || { echo "Nothing restarts the engine and its workers: scripts/engine.sh start"; exit 1; }
  set -- worker; ! alive worker_heavy || set -- worker worker_heavy  # the heavy one runs while there is work
  paused || [ "$("$PGBIN/psql" -Atqc "SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '2 minutes'" 2>/dev/null)" -ge $# ] 2>/dev/null \
    || { echo "A worker sent no heartbeat in 2 minutes: $D/logs/worker*.log"; exit 1; }
  echo healthy
  exit 0 ;;
pause)  # pause PID: the workers stop (once their running jobs end) and none starts while process PID runs
  echo "${2:?the pid of the process that holds the pause} $(started "$2")" > "$D/run/pause.tmp"
  mv "$D/run/pause.tmp" "$D/run/pause.pid"  # whole at once: the supervisor removes a pause it can't read as live
  log "the workers pause while process $2 runs"
  for p in $WORKERS; do ! alive $p || kill "$(cat "$D/run/$p.pid")" || true; done
  for p in $WORKERS; do while alive $p; do sleep 0.2; done; done
  # a runner (ABI_ROLE=runner) sees the file and stops its worker once its job ends: wait until no worker beats
  eval "$(env_)"
  while side && [ "$("$PGBIN/psql" -Atqc "SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '30 seconds'" 2>/dev/null || echo 0)" != 0 ]; do
    sleep 1
  done
  exit 0 ;;
supervise) supervise; exit 0 ;;
runner)  # ABI_ROLE=runner, in the foreground (the container's entrypoint): a supervisor of the heavy worker alone
  ROLE=runner PARTS=runner SV=runner_supervisor
  settings
  [ -x "$BIN/python" ] || { echo "Run once: (cd engine/backend && uv sync --frozen --no-dev)" >&2; exit 1; }
  umask 077
  mkdir -p "$D/run" "$D/logs"
  rm -f "$D/run/runner.pid"  # a new runner: a pid from before means nothing (and may be reused)
  supervise; exit 0 ;;
start) [ $ROLE = all ] || { echo "ABI_ROLE=runner runs scripts/engine.sh runner, not start." >&2; exit 1; } ;;
*) echo "usage: engine.sh [start|stop|status|health [APP_URL]|env|runner]" >&2; exit 2 ;;
esac

[ -x "$BIN/python" ] || { echo "Run once: (cd engine/backend && uv sync --frozen --no-dev)" >&2; exit 1; }
[ -f "$ROOT/engine/backend/static/index.html" ] || {
  echo "Build the engine's UI once: (cd engine/frontend && npm ci && npx vite build --outDir ../backend/static --emptyOutDir)" >&2; exit 1; }
settings
case ${ABI_DUCKDB_THREADS:-1} in *[!0-9]*) echo "ABI_DUCKDB_THREADS is a number of threads (or empty: half the CPUs), not '$ABI_DUCKDB_THREADS'." >&2; exit 1 ;; esac
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
