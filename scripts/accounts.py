#!/usr/bin/env python3
"""The own engine's accounts for this machine: the first run makes `himanshu` the workspace owner (ABI's /api/setup),
then creates the others. Random passwords go to data/accounts.txt (0600, git-ignored; `name password` lines, as
creds.py reads them) and are never printed. Re-running only adds missing names.

    scripts/accounts.py [name ...]     default: himanshu staff lena omar etl sara ken tom ivy
"""
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("ABI_URL", "http://127.0.0.1:8780")
FILE = Path(os.environ.get("ABI_ACCOUNTS", Path(__file__).resolve().parent.parent / "data/accounts.txt"))
NAMES = sys.argv[1:] or "himanshu staff lena omar etl sara ken tom ivy".split()


def post(path: str, body: dict, token: str | None = None) -> dict:
    h = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    try:
        with urllib.request.urlopen(urllib.request.Request(URL + path, json.dumps(body).encode(), h), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:  # never echo the body: a 422 repeats the password
        sys.exit(f"{path} answered {e.code}")


have = dict(l.split()[:2] for l in FILE.read_text().splitlines() if l.strip()) if FILE.exists() else {}
FILE.parent.mkdir(parents=True, exist_ok=True)
FILE.touch(0o600)
owner = NAMES[0] if not have else next(iter(have))
for name in NAMES:
    if name in have:
        continue
    pw = secrets.token_urlsafe(18)
    if not have:
        post("/api/setup", {"name": name, "password": pw})
    else:
        tok = post("/api/login", {"name": owner, "password": have[owner]})["token"]
        post("/api/actions/create_user", {"name": name, "password": pw}, tok)
    have[name] = pw
    with FILE.open("a") as f:
        f.write(f"{name} {pw}\n")
    print(f"created {name}")
