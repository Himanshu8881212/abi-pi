#!/bin/sh
# Back up and restore everything ABI-PI holds: the engine's database (users, roles, audit, every project's lake catalog,
# the job queue) as a dump, and the data folder (lake files, project Git repositories, uploads, the app's workspaces).
# The two key files (data/secret.key: the engine's secrets; workspaces/_settings/secret.key: the saved API keys) are
# kept apart, as ABI does: whoever holds a backup and its keys can open every stored secret.
#
#   scripts/backup.sh backup > abi-backup.tar      a backup to stdout (the worker pauses meanwhile; the app keeps serving)
#   scripts/backup.sh keys   > abi-keys.tar        the two key files; keep them somewhere else than the backups
#   scripts/backup.sh restore [keys.tar] < abi-backup.tar   into an empty data folder (a new volume); keys are optional
#   A restore goes to the same data path the backup came from (the lake catalog records it): always /data in the container.
#
# In the container:   docker exec abi scripts/backup.sh backup > abi-backup.tar
#                     docker exec abi scripts/backup.sh keys > abi-keys.tar
#   restore (new volume, container stopped):
#                     docker run --rm -i -v abi_abi-data:/data -v "$PWD/abi-keys.tar:/keys.tar:ro" \
#                       --entrypoint scripts/backup.sh ghcr.io/himanshu8881212/abi:latest restore /keys.tar < abi-backup.tar
#                     (README: Back up and restore; your ABI_VOLUME and ABI_IMAGE if .env sets them)
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
eval "$("$ROOT/scripts/engine.sh" env)"
D=${ABI_PI_DATA:-$ROOT/data}
PGBIN=${PGBIN:-$(dirname "$(command -v pg_ctl 2>/dev/null || echo /opt/homebrew/opt/postgresql@17/bin/pg_ctl)")}
W=${ABI_PI_WORKSPACES:-$ROOT/workspaces}
KEYS="secret.key workspaces/_settings/secret.key"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
umask 077

case "${1:-}" in
backup)
  # the workers' nightly maintenance deletes old lake files: paused (both queues' workers) so the dump and the files agree
  for w in worker worker_heavy; do
    W_PID=$(cat "$D/run/$w.pid" 2>/dev/null || true)
    [ -n "$W_PID" ] && kill "$W_PID" 2>/dev/null && while kill -0 "$W_PID" 2>/dev/null; do sleep 0.2; done
  done
  "$PGBIN/pg_dump" -Fc -f "$T/abi.dump" abi
  # the workspaces live in the data folder in the container, next to it on a machine: both go in as workspaces/
  tar -cf "$T/data.tar" -C "$D" --exclude=./pg --exclude=./run --exclude=./logs --exclude=./secret.key \
      --exclude=./accounts.txt --exclude=./workspaces .
  tar -cf "$T/workspaces.tar" -C "$W" --exclude=./_settings/secret.key .
  (cd "$T" && { echo "abi-pi-backup/1 $(date -u +%Y-%m-%dT%H:%M:%SZ)"; shasum -a 256 abi.dump data.tar workspaces.tar; } > manifest.txt)
  tar -cf - -C "$T" manifest.txt abi.dump data.tar workspaces.tar
  "$ROOT/scripts/engine.sh" start >&2  # the worker again
  ;;
keys)
  mkdir -p "$T/workspaces/_settings"
  [ -f "$D/secret.key" ] && cp "$D/secret.key" "$T/"
  [ -f "$W/_settings/secret.key" ] && cp "$W/_settings/secret.key" "$T/workspaces/_settings/"
  tar -cf - -C "$T" .
  ;;
restore)
  if [ -f "$D/pg/PG_VERSION" ] || [ -n "$(ls -A "$W" 2>/dev/null | grep -v '^.gitkeep$' || true)" ]; then
    echo "Refused: $D already holds a database or workspaces. Restore into a new, empty volume." >&2; exit 1
  fi
  tar -xf - -C "$T"
  head -1 "$T/manifest.txt" | grep -q '^abi-pi-backup/1 ' || { echo "Not an ABI-PI backup." >&2; exit 1; }
  (cd "$T" && tail -n +2 manifest.txt | shasum -a 256 -c --quiet) || { echo "The backup is damaged (checksums differ)." >&2; exit 1; }
  mkdir -p "$D" "$W"
  tar -xf "$T/data.tar" -C "$D"
  tar -xf "$T/workspaces.tar" -C "$W"
  if [ -n "${2:-}" ]; then
    tar -xf "$2" -C "$T/keys" 2>/dev/null || { mkdir -p "$T/keys" && tar -xf "$2" -C "$T/keys"; }
    [ -f "$T/keys/secret.key" ] && cp "$T/keys/secret.key" "$D/"
    [ -f "$T/keys/workspaces/_settings/secret.key" ] && mkdir -p "$W/_settings" && cp "$T/keys/workspaces/_settings/secret.key" "$W/_settings/"
  fi
  "$PGBIN/initdb" -D "$D/pg" -U abi -A trust -E UTF8 --no-locale >/dev/null
  "$PGBIN/pg_ctl" -D "$D/pg" -l "$T/pg.log" -o "-c listen_addresses='' -k $D/pg" -w start >/dev/null
  "$PGBIN/createdb" abi
  "$PGBIN/pg_restore" --no-owner -d abi "$T/abi.dump"
  "$PGBIN/pg_ctl" -D "$D/pg" -m fast stop >/dev/null
  echo "restored $(sed -n 1p "$T/manifest.txt" | cut -d' ' -f2)$([ -n "${2:-}" ] || echo ' without keys: stored secrets and saved API keys must be entered again')" >&2
  ;;
*) sed -n 2,16p "$0" >&2; exit 2 ;;
esac
