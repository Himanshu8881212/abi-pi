#!/usr/bin/env python3
"""Credentials at request time, never stored: Pi runs `creds.py <what>` and reads stdout (SC-4).

  llm-base   CLIProxyAPI base URL (from ~/cliproxy-server-credentials.txt)
  llm-key    CLIProxyAPI API key
  abi-token  an ABI session token: $ABI_TOKEN, else sign in as $ABI_USER (default himanshu) with the
             password from data/accounts.txt (scripts/accounts.py). The password never leaves this process.

Stdlib only (runs on the system python3).
"""
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ABI_URL = os.environ.get("ABI_URL", "http://127.0.0.1:8780")
PROXY = Path(os.environ.get("CLIPROXY_CREDS", "~/cliproxy-server-credentials.txt")).expanduser()
ACCOUNTS = Path(os.environ.get("ABI_ACCOUNTS", Path(__file__).resolve().parent.parent / "data/accounts.txt")).expanduser()


def proxy(label: str) -> str:
    env = {"API base URL": "ABI_PI_LLM_BASE", "API key": "ABI_PI_LLM_KEY"}.get(label)
    if env and os.environ.get(env):  # a container (docker/entrypoint.sh) passes them in its environment
        return os.environ[env]
    m = re.search(rf"^{label}[^:]*:\s*(\S+)", PROXY.read_text(), re.M)
    if not m:
        sys.exit(f"{label} not found in {PROXY}")
    return m[1]


def abi_token() -> str:
    if os.environ.get("ABI_TOKEN"):
        return os.environ["ABI_TOKEN"]
    user = os.environ.get("ABI_USER", "himanshu")
    if os.environ.get("ABI_PASSWORD"):  # a container: the service account's password from its environment, never a file
        return _sign_in(user, os.environ["ABI_PASSWORD"])
    line = next((l for l in ACCOUNTS.read_text().splitlines() if re.match(rf"\s*{re.escape(user)}\b", l)), None)
    if not line:
        sys.exit(f"No account line for {user}")
    return _sign_in(user, line.split()[-1])


def _sign_in(user: str, password: str) -> str:
    body = json.dumps({"name": user, "password": password}).encode()
    req = urllib.request.Request(f"{ABI_URL}/api/login", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)["token"]
    except Exception as e:  # never echo the body: a 422 repeats the password
        sys.exit(f"ABI sign-in failed: {type(e).__name__}")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "llm-base":
        print(proxy("API base URL"))
    elif what == "llm-key":
        print(proxy("API key"))
    elif what == "abi-token":
        print(abi_token())
    elif what == "abi-bearer":
        print("Bearer " + abi_token())
    else:
        sys.exit(__doc__)
