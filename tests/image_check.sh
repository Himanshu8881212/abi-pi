#!/usr/bin/env bash
# The image started as a stranger would, and checked: healthy, PID 1 tini as abi, sign-in, a small CSV through the app's
# upload read back exact, a PDF and a video of a report. Used by image.yml (arm64) and release.yml (before it publishes).
#   IMAGE=abi:check C=abi-check PORT=8790 tests/image_check.sh      (Docker; the owner's password is generated, masked)
set -eu
: "${IMAGE:?}" "${C:?}" "${PORT:=8790}"
A=http://127.0.0.1:$PORT; S=$(mktemp -d)
export ABI_PASSWORD; ABI_PASSWORD=$(openssl rand -hex 16); if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::add-mask::$ABI_PASSWORD"; fi  # never in argv, a log or a terminal
docker run -d --name "$C" -p "127.0.0.1:$PORT:8790" -v "$C:/data" -e ABI_USER=admin -e ABI_PASSWORD "$IMAGE" >/dev/null
for _ in $(seq 120); do
  h=$(docker inspect -f '{{.State.Health.Status}}' "$C")
  case $h in healthy|unhealthy) break ;; esac; sleep 5
done
echo "health: $h"; [ "$h" = healthy ]
set -- $(docker exec "$C" ps -o comm=,user= -p 1); echo "PID 1: $*"; [ "$1 $2" = "tini abi" ]
[ "$(docker exec "$C" id -un)" = abi ]
# sign in as the owner, as the sign-in page does; the page carries its key
code=$(printf %s "$ABI_PASSWORD" | curl -s -o /dev/null -w '%{http_code}' -c "$S/jar" --data-urlencode name=admin \
  --data-urlencode password@- "$A/signin")
echo "sign-in: $code"; [ "$code" = 303 ]; grep -q abi_u "$S/jar"
KEY=$(curl -fs -b "$S/jar" "$A/" | sed -n "s/.*KEY='\([^']*\)'.*/\1/p" | head -1); [ -n "$KEY" ]
post() { curl -sS -b "$S/jar" -H "X-Abi-App: $KEY" -H "X-Abi-Client: ci" -H "X-Abi-Project: $2" \
  -H "Content-Type: application/json" --data-binary @- "$A$1"; }
# a small CSV through Data › Sources › Add source (the page's upload, one piece), read back exact
printf 'order_date,region,amount\n2026-05-01,North,10.25\n2026-05-01,South,20.50\n2026-05-02,North,5.10\n' > "$S/orders.csv"
python3 -c "import base64, json, secrets, sys; print(json.dumps({'table': 'orders', 'name': 'orders.csv', 'id': secrets.token_hex(16),
  'last': True, 'data': base64.b64encode(open(sys.argv[1], 'rb').read()).decode()}))" "$S/orders.csv" | post /upload main > "$S/up"
echo "upload: $(cut -c1-300 "$S/up")"; if grep -q '"error"' "$S/up"; then exit 1; fi
echo '{"table": "orders", "limit": 50}' | post /edits/rows main > "$S/rows"
python3 - "$S/rows" <<'EOF'
import json, sys
from decimal import Decimal
r = json.load(open(sys.argv[1]))
r = r["result"] if "result" in r else sys.exit(f"rows: {r}")
rows = [dict(zip([c["name"] for c in r["columns"]], x)) for x in r["rows"]]
got = sorted((str(x["order_date"])[:10], x["region"], Decimal(str(x["amount"]))) for x in rows)
want = [("2026-05-01", "North", Decimal("10.25")), ("2026-05-01", "South", Decimal("20.50")), ("2026-05-02", "North", Decimal("5.10"))]
print("rows:", got)
assert got == want and sum(x[2] for x in got) == Decimal("35.85"), (got, want)
EOF
# a PDF of a report: the same file as a project with its starter model and report, exported through the app
docker cp "$S/orders.csv" "$C:/tmp/orders.csv"
docker exec "$C" sh -c 'cd /app && eval "$(scripts/engine.sh env)" && python3 -c "
import sys; sys.path.insert(0, \"scripts\"); import abi
r = abi.load(\"main\", \"arm_pdf\", [\"/tmp/orders.csv\"]); assert r.get(\"report\") and not r[\"failed\"], r; print(r[\"report\"])"'
echo '{"report": "overview", "pages": "all", "format": "pdf"}' | post /report/export arm_pdf > "$S/r.pdf"
echo "pdf: $(wc -c < "$S/r.pdf") bytes, $(head -c 200 "$S/r.pdf" | tr -cd '[:print:]')"; [ "$(head -c 5 "$S/r.pdf")" = "%PDF-" ]
# a video of the same report: the image's Chromium draws it and ffmpeg encodes it, so its arm64 build must render too
docker exec "$C" sh -c 'cd /app && eval "$(scripts/engine.sh env)" && python3 scripts/video.py arm_pdf overview --seconds 1 --size 320x180 --out /tmp/v.mp4'
docker cp "$C:/tmp/v.mp4" "$S/v.mp4"; echo "video: $(wc -c < "$S/v.mp4") bytes"; head -c 12 "$S/v.mp4" | grep -q ftyp
echo "all checks passed on $(docker exec "$C" uname -m)"
