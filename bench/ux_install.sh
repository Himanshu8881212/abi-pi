#!/bin/sh
# The 1.2.5 UX bench: a throwaway ABI install of one git ref on pve1 (LXC 100), seeded so no page is empty, with the
# Mac's model provider (bench/UX.md). Run FROM THE MAC in the repo:
#
#   bench/ux_install.sh BRANCH_OR_TAG NAME PORT        a new install: image abi-ux:NAME, container abi-ux-NAME,
#                                                      volume abi-ux-NAME-data, 127.0.0.1:PORT on the LXC
#   bench/ux_install.sh --redeploy BRANCH_OR_TAG NAME [PORT]   rebuild on another ref, restart, keep the volume
#
# Root-only files on the LXC: /root/abi-ux-NAME.env (the owner's random password) and /root/abi-ux-NAME-accounts
# (bench/ux_seed.py's output: owner, editor1, viewer1, owner2). No password or key is printed or written anywhere else.
# The harness (bench/ux_seed.py, ux_shot.py and the brewline.py and perf.py they use) is this checkout's, copied into
# the container (the image leaves bench/ out), so a ref from before them (v1.2.0) works too. Touches only abi-ux-* containers, volumes, images and /opt/abi-ux-*.
set -eu
REDEPLOY=
[ "${1:-}" = --redeploy ] && { REDEPLOY=1; shift; }
[ $# -ge 2 ] || { sed -n '2,12p' "$0"; exit 2; }
REF=$1 NAME=$2 PORT=${3:-}
case $NAME in ''|*[!a-z0-9-]*) echo "NAME: lowercase letters, digits and -" >&2; exit 2 ;; esac
case $PORT in *[!0-9]*) echo "PORT: a number" >&2; exit 2 ;; esac
[ -n "$PORT" ] || [ -n "$REDEPLOY" ] || { echo "PORT is needed for a new install" >&2; exit 2; }
case $PORT in 8790|8797|9000|9001) echo "Port $PORT belongs to the live installs" >&2; exit 2 ;; esac
HERE=$(cd "$(dirname "$0")/.." && pwd)
C=abi-ux-$NAME
O=/opt/abi-ux-$NAME
git -C "$HERE" rev-parse -q --verify "$REF^{commit}" >/dev/null || { echo "No such ref: $REF" >&2; exit 2; }
lxc() { ssh pve1 "pct exec 100 -- $1"; }

echo "== $REF into $O/src"
git -C "$HERE" archive --format=tar "$REF" | lxc "sh -c 'rm -rf $O/src && mkdir -p $O/src && tar -x -C $O/src'"
COPYFILE_DISABLE=1 tar -C "$HERE" --no-xattrs -cf - bench/ux_seed.py bench/ux_shot.py bench/brewline.py bench/perf.py | lxc "sh -c 'mkdir -p $O/harness && tar -x -C $O/harness'"

echo "== build abi-ux:$NAME, start $C"
lxc "sh -s" <<EOF
set -eu
C=$C E=/root/abi-ux-$NAME.env A=/root/abi-ux-$NAME-accounts PORT=$PORT
docker build -q -t abi-ux:$NAME $O/src </dev/null >/dev/null
if docker inspect \$C >/dev/null 2>&1; then
  [ -n "$REDEPLOY" ] || { echo "\$C exists: use --redeploy" >&2; exit 1; }
  [ -n "\$PORT" ] || PORT=\$(docker inspect -f '{{(index (index .HostConfig.PortBindings "8790/tcp") 0).HostPort}}' \$C)
  docker rm -f \$C >/dev/null
fi
umask 077
[ -f \$E ] || printf 'ABI_USER=owner\nABI_PASSWORD=%s\nABI_PI_PROJECT=brewline\nABI_PI_WORKSPACE=main\n' "\$(head -c 24 /dev/urandom | base64 | tr -d '/+=')" > \$E
docker run -d --name \$C --env-file \$E -v \$C-data:/data -p 127.0.0.1:\$PORT:8790 --restart unless-stopped abi-ux:$NAME </dev/null >/dev/null
i=0
until [ "\$(docker inspect -f '{{.State.Health.Status}}' \$C)" = healthy ]; do
  i=\$((i + 1)); [ \$i -lt 120 ] || { echo "\$C not healthy after 10 min" >&2; docker logs --tail 30 \$C >&2; exit 1; }
  sleep 5
done
docker cp $O/harness/bench/. \$C:/app/bench/ </dev/null
echo "== seed (a few minutes the first time)"
touch \$A
docker exec -i \$C sh -c 'cd /app && eval "\$(scripts/engine.sh env)" && python3 bench/ux_seed.py' < \$A > \$A.new
mv \$A.new \$A
echo "\$C up on 127.0.0.1:\$PORT"
EOF

echo "== model provider (the Mac's proxy)"
# the provider as the Mac's Settings has it, its key decrypted in memory (or, with none stored, the proxy's own from
# ~/cliproxy-server-credentials.txt): two lines into the container's stdin, never printed or kept
"$HERE/.venv/bin/python" - "$HERE" <<'EOF' | lxc "docker exec -i $C sh -c 'cd /app && eval \"\$(scripts/engine.sh env)\" && python3 bench/ux_seed.py --model'"
import json, os, re, sys
sys.path.insert(0, sys.argv[1] + "/scripts")
import settings as s
p = next(x for x in s.load()["providers"] if x["id"] == "proxy")
key = s.key_of("proxy")
if not key:
    f = os.path.expanduser("~/cliproxy-server-credentials.txt")
    key = next((m[1] for m in map(re.compile(r"^API key[^:]*:\s*(\S+)").match, open(f)) if m), "")
print(json.dumps({k: p[k] for k in ("id", "name", "api", "base_url", "models")}))
print(key)
EOF
echo "== done: $C on pve1 LXC 100; accounts in /root/abi-ux-$NAME-accounts"
