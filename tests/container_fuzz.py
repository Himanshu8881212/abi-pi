"""Security, stress and edge cases against the running test container (the loop's areas A, C, D), as a signed-in owner.
Never calls publish, invite, sending or syncing to a source. The owner's password comes from the container's own
environment (`docker inspect`), stays in this process and is never printed.

    .venv/bin/python tests/container_fuzz.py          (after tests/container_check.sh left abi-pi-test running)
"""
import concurrent.futures as cf
import http.cookiejar
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

B = "http://127.0.0.1:8796"
ENV = subprocess.run(["docker", "inspect", "abi-pi-test", "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
                     capture_output=True, text=True, check=True).stdout
PW = next(line.split("=", 1)[1] for line in ENV.splitlines() if line.startswith("ABI_PASSWORD="))


def opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def session():
    op = opener()
    op.open(B + "/signin", urllib.parse.urlencode({"name": "admin", "password": PW}).encode()).read()
    return op, re.search(r"KEY='([^']+)'", op.open(B + "/").read().decode("utf8", "replace")).group(1)


def post(op, key, path, body=None, raw=None, cid="t"):
    r = urllib.request.Request(B + path, raw if raw is not None else json.dumps(body).encode(),
                               {"X-Abi-App": key, "Content-Type": "application/json", "X-Abi-Client": cid})
    try:
        with op.open(r, timeout=120) as x:
            return x.status, x.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001 (a dropped connection is a finding)
        return type(e).__name__, b""


op, key = session()
P = {"project": "pi_app_test"}
q = {**P, "measures": ["orders_rows"], "dimensions": ["orders.region"]}
checks = {}
checks["no session: 403"] = post(opener(), "", "/api/semantic_model", P)[0] == 403
checks["wrong page key: 403"] = post(op, "nope", "/api/semantic_model", P)[0] == 403
checks["bad JSON: 400"] = post(op, key, "/api/semantic_model", raw=b"{x")[0] == 400
checks["70 MB body: 413, every time"] = all(post(op, key, "/api/semantic_model", raw=b"x" * (70 << 20))[0] == 413 for _ in range(5))
checks["vault traversal refused"] = b"use .md" in post(op, key, "/vault/read", {"name": "../../../data/pg/PG_VERSION"})[1]
checks["pack traversal refused"] = post(op, key, "/packs/file", {"id": "../../_settings", "file": "settings.json"})[0] == 404
checks["metadata provider refused"] = b"metadata" in post(op, key, "/admin/settings/provider", {"provider": {
    "id": "x", "api": "openai-completions", "base_url": "http://169.254.169.254/v1", "models": ["m"]}})[1]
c, b = post(op, key, "/admin/settings/provider", {"provider": {"id": "fz", "api": "openai-completions", "base_url": "https://example.invalid/v1",
                                                               "models": ["m"]}, "key": "sk-fuzz-0123456789abcdef"})
checks["API key never returned"] = c == 200 and b"sk-fuzz-0123456789" not in b + post(op, key, "/admin/settings", {})[1]
post(op, key, "/admin/settings/remove", {"id": "fz"})
checks["unknown action: 403"] = post(op, key, "/api/drop_everything", P)[0] == 403
checks["1e309: 400"] = post(op, key, "/edits/rows", {"table": "orders", "limit": 1e309})[0] == 400
checks["null and missing fields answer"] = all(isinstance(post(op, key, "/api/semantic_query", b)[0], int) for b in
                                               ({**P, "measures": None, "dimensions": None}, P, {}, {"project": None}))
checks["unicode and long names answer"] = all(isinstance(post(op, key, "/api/semantic_query", {**P, "measures": [n], "dimensions": []})[0], int)
                                              for n in ("Ünïcødé ✓", "x" * 5000, "'; DROP TABLE orders; --", "<script>"))
post(op, key, "/analyses", {"analyses": [{"name": "<img src=x onerror=alert(1)>", "measures": [], "dimensions": []}]})
page = op.open(B + "/").read().decode("utf8", "replace")
# inside the page's JSON (a <script>) it is data; the page draws names with textContent (h()), never as HTML
checks["XSS: never in the HTML outside scripts"] = "<img src=x onerror" not in re.sub(r"(?s)<script.*?</script>", "", page)
h = op.open(B + "/").headers
checks["framing refused"] = bool(h.get("X-Frame-Options"))
ss = [session() for _ in range(10)]
with cf.ThreadPoolExecutor(50) as ex:
    codes = Counter(ex.map(lambda i: post(*ss[i % 10], "/api/semantic_query", q, cid=f"c{i % 10}")[0], range(300)))
checks[f"300 queries, 10 sessions, 50 at once: {dict(codes)}"] = codes == {200: 300}
with cf.ThreadPoolExecutor(50) as ex:
    codes = Counter(ex.map(lambda i: post(op, key, "/mode", {"mode": "viewer" if i % 2 else "developer"}, cid="flip")[0], range(200)))
checks[f"200 mode flips, 50 at once: {dict(codes)}"] = codes == {200: 200}
with cf.ThreadPoolExecutor(20) as ex:
    codes = Counter(ex.map(lambda i: post(op, key, "/api/semantic_model", {"project": ["pi_app_test", "nope", "", "../x"][i % 4]},
                                         cid=f"sw{i % 3}")[0], range(80)))
checks[f"rapid project switching: {dict(codes)}"] = all(isinstance(k, int) for k in codes)
for k, v in checks.items():
    print("ok  " if v else "FAIL", k)
sys.exit(0 if all(checks.values()) else 1)
