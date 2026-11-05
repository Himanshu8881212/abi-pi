#!/bin/sh
# Upgrade ABI in place, from the folder with docker-compose.yml and .env: a backup of the running install first
# (backups/abi-<time>.tar and its keys, abi-<time>-keys.tar), then the new image (pulled, else built here) on the same data volume, then wait until it
# is healthy. An install from 1.0 (compose project abi-pi) moves over too: its volume stays where it is (ABI_VOLUME
# in .env) and its old container goes once the backup is done. Restore a backup: scripts/backup.sh (restore).
# The profiles .env names (COMPOSE_PROFILES=https: Caddy in front) come along: plain docker compose reads them, and
# so does the image it chose (ABI_IMAGE).
#
#   git pull && scripts/upgrade.sh
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env here: run this from the folder you started ABI in." >&2; exit 1; }
here=$(pwd -P)
mine() { docker ps -q --filter "label=com.docker.compose.project.working_dir=$here" --filter "label=com.docker.compose.service=$1"; }
OLD=$(mine abi-pi)  # 1.0's service name
C=${OLD:-$(mine abi)}
VOL=$(sed -n 's/^ABI_VOLUME=//p' .env | tail -1 | tr -d "\"'")
if [ -n "$C" ]; then
  mkdir -p backups
  F=backups/abi-$(date +%Y%m%d-%H%M%S).tar  # the whole database: readable by this machine's owner only
  K=${F%.tar}-keys.tar  # the two key files: without them stored source secrets and saved API keys can't be opened
  echo "backing up to $F and $K"
  (umask 077 && docker exec "$C" scripts/backup.sh backup > "$F" && docker exec "$C" scripts/backup.sh keys > "$K") \
    || { rm -f "$F" "$K"; echo "The backup failed: nothing was changed." >&2; exit 1; }
elif [ "${ABI_NO_BACKUP:-}" != 1 ] && docker volume inspect "${VOL:-abi_abi-data}" > /dev/null 2>&1; then
  # a stopped install: the new version migrates its database for good, so never without a backup
  echo "ABI isn't running: start it (docker compose up -d) so upgrade.sh can back it up first (ABI_NO_BACKUP=1 skips the backup)." >&2
  exit 1
fi
if [ -n "$OLD" ]; then
  V=$(docker inspect "$OLD" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')
  grep -q '^ABI_VOLUME=' .env || printf '\nABI_VOLUME=%s\n' "$V" >> .env
  echo "moving the 1.0 install (volume $V) over"
fi
IMG=$(sed -n 's/^ABI_IMAGE=//p' .env | tail -1 | tr -d "\"'")
IMG=${IMG:-ghcr.io/himanshu8881212/abi:latest}
docker compose pull 2>/dev/null || docker build -t "$IMG" .
[ -z "$OLD" ] || docker rm -f "$OLD" > /dev/null
docker compose up -d
printf 'waiting for ABI'
for _ in $(seq 60); do
  C=$(docker compose ps -q abi)  # this install's new container, never another install's on this machine
  case $(docker inspect "$C" --format '{{.State.Health.Status}}' 2>/dev/null) in
    healthy) echo " ok: $(docker exec "$C" cat /data/VERSION)"; exit 0 ;;
  esac
  printf .; sleep 5
done
echo; echo "Not healthy after 5 minutes: docker compose logs abi" >&2; exit 1
