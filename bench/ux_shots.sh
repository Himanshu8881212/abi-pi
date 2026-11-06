#!/bin/sh
# Screenshots of a UX bench install (bench/ux_install.sh), run FROM THE MAC: bench/ux_shot.py in container abi-ux-NAME,
# then its PNGs and steps.jsonl copied back into OUTDIR (bench/UX.md).
#
#   bench/ux_shots.sh NAME OUTDIR --tour [--role owner|viewer] [--width 1440|390]
#   bench/ux_shots.sh NAME OUTDIR steps.json [--role owner|editor|viewer] [--width 1440|390]
#
# The accounts come from the root-only /root/abi-ux-NAME-accounts on the LXC, passed in the container's environment
# (ABI_UX_ACCOUNTS), never on a command line.
set -eu
[ $# -ge 3 ] || { sed -n '2,9p' "$0"; exit 2; }
NAME=$1 OUT=$2 WHAT=$3
shift 3
case $NAME in ''|*[!a-z0-9-]*) echo "NAME: lowercase letters, digits and -" >&2; exit 2 ;; esac
for x in "$@"; do case $x in --role|--width|owner|editor|viewer|1440|390) ;; *) echo "Unknown option $x" >&2; exit 2 ;; esac; done
HERE=$(cd "$(dirname "$0")/.." && pwd)
C=abi-ux-$NAME
O=/opt/abi-ux-$NAME
lxc() { ssh pve1 "pct exec 100 -- $1"; }
COPYFILE_DISABLE=1 tar -C "$HERE" --no-xattrs -cf - bench/ux_shot.py | lxc "sh -c 'mkdir -p $O/harness && tar -x -C $O/harness && docker cp $O/harness/bench/ux_shot.py $C:/app/bench/'"
if [ "$WHAT" = --tour ]; then
  ARGS="--tour $*"
else
  lxc "docker exec -i $C sh -c 'cat > /tmp/ux-steps.json'" < "$WHAT"
  ARGS="/tmp/ux-steps.json $*"
fi
lxc "sh -s" <<EOF
set -eu
docker exec $C rm -rf /tmp/ux-shots
ABI_UX_ACCOUNTS="\$(cat /root/abi-ux-$NAME-accounts)" docker exec -e ABI_UX_ACCOUNTS $C python3 /app/bench/ux_shot.py --out /tmp/ux-shots $ARGS </dev/null
rm -rf $O/shots && docker cp $C:/tmp/ux-shots $O/shots
EOF
mkdir -p "$OUT"
lxc "tar -C $O/shots -cf - ." | tar -x -C "$OUT"
echo "$(ls "$OUT"/*.png | wc -l | tr -d ' ') PNGs and steps.jsonl in $OUT"
