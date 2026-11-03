#!/bin/sh
# Upgrade ABI in place, from the folder with docker-compose.yml and .env: a backup of the running install first
# (backups/abi-<time>.tar), then the new image (pulled, else built here) on the same data volume, then wait until it
# is healthy. An install from 1.0 (compose project abi-pi) moves over too: its volume stays where it is (ABI_VOLUME
# in .env) and its old container goes once the backup is done. Restore a backup: scripts/backup.sh (restore).
#
#   git pull && scripts/upgrade.sh
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env here: run this from the folder you started ABI in." >&2; exit 1; }
here=$(pwd -P)
mine() { docker ps -q --filter "label=com.docker.compose.project.working_dir=$here" --filter "label=com.docker.compose.service=$1"; }
OLD=$(mine abi-pi)  # 1.0's service name
C=${OLD:-$(mine abi)}
if [ -n "$C" ]; then
  mkdir -p backups
  F=backups/abi-$(date +%Y%m%d-%H%M%S).tar
  echo "backing up to $F"
  docker exec "$C" scripts/backup.sh backup > "$F" || { rm -f "$F"; echo "The backup failed: nothing was changed." >&2; exit 1; }
fi
if [ -n "$OLD" ]; then
  V=$(docker inspect "$OLD" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
  grep -q '^ABI_VOLUME=' .env || printf '\nABI_VOLUME=%s\n' "$V" >> .env
  echo "moving the 1.0 install (volume $V) over"
fi
docker compose pull 2>/dev/null || docker compose build
[ -z "$OLD" ] || docker rm -f "$OLD" > /dev/null
docker compose up -d
printf 'waiting for ABI'
for _ in $(seq 60); do
  case $(docker inspect abi --format '{{.State.Health.Status}}' 2>/dev/null) in
    healthy) echo " ok: $(docker exec abi cat /data/VERSION)"; exit 0 ;;
  esac
  printf .; sleep 5
done
echo; echo "Not healthy after 5 minutes: docker logs abi" >&2; exit 1
