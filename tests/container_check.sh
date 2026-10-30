#!/bin/sh
# The release container as a stranger runs it (the loop's area E): fresh volume, data, exports, the agent, a hard kill,
# backup + restore into a new volume, secrets nowhere, non-root, health. Secrets only in this shell's environment.
#   tests/container_check.sh     (needs Docker, the image abi-pi:latest, the model endpoint via scripts/creds.py)
set -eu
cd "$(dirname "$0")/.."
S=$(mktemp -d); trap 'rm -rf "$S"; docker rm -f abi-pi-restored >/dev/null 2>&1; docker volume rm abi-pi-restore >/dev/null 2>&1 || true' EXIT
ok() { echo "ok   $1"; }; bad() { echo "FAIL $1"; FAILED=1; }; FAILED=0
up() { for _ in $(seq 1 90); do curl -fs -o /dev/null "127.0.0.1:$1/" && return 0; sleep 2; done; return 1; }
in_c() { docker exec "$1" sh -c "cd /app && eval \"\$(scripts/engine.sh env)\" && $2"; }
docker rm -f abi-pi-test >/dev/null 2>&1 || true; docker volume rm abi-pi-e2e >/dev/null 2>&1 || true
export ABI_PASSWORD; ABI_PASSWORD=$(openssl rand -hex 12)
export ABI_PI_LLM_BASE ABI_PI_LLM_KEY; ABI_PI_LLM_BASE=$(python3 scripts/creds.py llm-base); ABI_PI_LLM_KEY=$(python3 scripts/creds.py llm-key)
run() { docker run -d --name "$1" -p "127.0.0.1:$2:8790" -v "$3:/data" -e ABI_USER=admin -e ABI_PASSWORD -e ABI_PI_LLM_BASE \
  -e ABI_PI_LLM_KEY -e ABI_PI_PROJECT=pi_app_test -e ABI_PI_WORKSPACE=demo abi-pi:latest >/dev/null; }
t=$(date +%s); run abi-pi-test 8796 abi-pi-e2e; up 8796 && ok "fresh volume up in $(( $(date +%s) - t )) s" || bad "fresh start"
[ "$(docker exec abi-pi-test ps -o comm= -p 1)" = tini ] && ok "tini is PID 1" || bad "PID 1"
[ "$(docker exec abi-pi-test id -un)" = abi ] && ok "runs as abi" || bad "user"
in_c abi-pi-test 'mkdir -p /tmp/d && printf "order_date,region,amount\n2026-05-01,North,10\n2026-05-01,South,20\n2026-05-02,North,5\n" > /tmp/d/orders.csv && python3 -c "
import json, sys; sys.path.insert(0, \"scripts\"); import abi
abi.load(\"demo\", \"pi_app_test\", [\"/tmp/d/orders.csv\"])
pages = [p[\"id\"] for p in json.load(open(abi.folder(\"pi_app_test\") / \"reports/overview.json\"))[\"pages\"]]
for f in (\"pdf\", \"pptx\"):
    b = abi.artifact(abi.call(\"report_snapshot\", project=\"pi_app_test\", report=\"overview\", format=f, pages=pages)[\"snapshot\"]); assert b[:2] in (b\"%P\", b\"PK\"), f
print(abi.call(\"semantic_query\", project=\"pi_app_test\", measures=[\"orders_rows\"], dimensions=[\"orders.region\"])[\"rows\"])"' > "$S/rows" 2>&1 \
  && ok "load, query, PDF, PPTX: $(tail -1 "$S/rows")" || bad "load/exports: $(tail -2 "$S/rows")"
in_c abi-pi-test 'python3 scripts/video.py pi_app_test overview --seconds 1 --size 320x180 --out /tmp/v.mp4' >/dev/null 2>&1 && ok "video" || bad "video"
in_c abi-pi-test 'export ABI_TOKEN=$(python3 scripts/creds.py abi-token) && pi -p "In project pi_app_test: how many orders rows per region? Give the figures."' > "$S/agent" 2>&1 || true
grep -q "North" "$S/agent" && grep -q "2" "$S/agent" && ok "agent answers inside" || bad "agent: $(tail -3 "$S/agent")"
docker kill -s KILL abi-pi-test >/dev/null; docker start abi-pi-test >/dev/null; up 8796 && in_c abi-pi-test 'python3 -c "import sys; sys.path.insert(0, \"scripts\"); import abi; print(abi.call(\"semantic_query\", project=\"pi_app_test\", measures=[\"orders_rows\"], dimensions=[\"orders.region\"])[\"rows\"])"' > "$S/after" 2>&1
cmp -s "$S/after" "$(printf '%s' "$S/rows")" 2>/dev/null; [ "$(tail -1 "$S/after")" = "$(tail -1 "$S/rows")" ] && ok "hard kill keeps data" || bad "after kill: $(tail -1 "$S/after")"
docker exec abi-pi-test scripts/backup.sh backup > "$S/b.tar" 2>/dev/null && docker exec abi-pi-test scripts/backup.sh keys > "$S/k.tar" && ok "backup $(du -h "$S/b.tar" | cut -f1)" || bad "backup"
docker volume create abi-pi-restore >/dev/null
docker run --rm -i -v abi-pi-restore:/data -v "$S/k.tar:/k.tar:ro" --entrypoint scripts/backup.sh abi-pi:latest restore /k.tar < "$S/b.tar" >/dev/null 2>&1 || bad "restore"
run abi-pi-restored 8797 abi-pi-restore; up 8797 && in_c abi-pi-restored 'python3 -c "import sys; sys.path.insert(0, \"scripts\"); import abi; print(abi.call(\"semantic_query\", project=\"pi_app_test\", measures=[\"orders_rows\"], dimensions=[\"orders.region\"])[\"rows\"])"' > "$S/restored" 2>&1
[ "$(tail -1 "$S/restored")" = "$(tail -1 "$S/rows")" ] && ok "restore into a new volume answers the same rows" || bad "restored: $(tail -1 "$S/restored")"
for s in "$ABI_PASSWORD" "$ABI_PI_LLM_KEY"; do
  n=$(docker logs abi-pi-test 2>&1 | grep -cF "$s" || true); n=$((n + $(docker exec -e P="$s" abi-pi-test sh -c 'grep -rlF "$P" /data /app 2>/dev/null | wc -l')))
  n=$((n + $(docker history --no-trunc abi-pi:latest | grep -cF "$s" || true))); n=$((n + $(tar -xOf "$S/b.tar" 2>/dev/null | grep -caF "$s" || true)))
  [ "$n" = 0 ] || bad "a secret appears in logs, files, layers or the backup ($n)"
done; [ "$FAILED" = 0 ] && ok "no secret in logs, the volume, the image or the backup"
docker run --rm -e ABI_USER=a -e ABI_PASSWORD=short abi-pi:latest 2>&1 | grep -q "8 characters" && ok "short password: clear message" || bad "short password message"
docker run --rm abi-pi:latest 2>&1 | grep -q "Set ABI_USER" && ok "missing env: clear message" || bad "missing env message"
sleep 35; [ "$(docker inspect abi-pi-test --format '{{.State.Health.Status}}')" = healthy ] && ok "healthy" || bad "health"
echo "image $(docker images abi-pi:latest --format '{{.Size}}')"; exit "$FAILED"
