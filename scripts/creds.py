#!/usr/bin/env python3
"""Credentials at request time, never stored: Pi runs `creds.py <what>` and reads stdout (SC-4).

  llm-base   the server's model endpoint: $ABI_PI_LLM_BASE (.env's ABI_LLM_BASE)
  llm-key    its key: $ABI_PI_LLM_KEY (.env's ABI_LLM_KEY); empty when it takes none
  abi-token  an ABI session token: $ABI_TOKEN, else sign in as $ABI_USER (default admin) with the
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
ACCOUNTS = Path(os.environ.get("ABI_ACCOUNTS", Path(__file__).resolve().parent.parent / "data/accounts.txt")).expanduser()


def proxy(label: str) -> str:
    """1.2: only what the deployment sets (no file of anyone's own): the endpoint, else none."""
    env = {"API base URL": "ABI_PI_LLM_BASE", "API key": "ABI_PI_LLM_KEY"}[label]
    if os.environ.get(env):
        return os.environ[env]
    if label == "API key" and os.environ.get("ABI_PI_LLM_BASE"):
        return "none"  # an endpoint that takes no key (a local Ollama)
    sys.exit(f"{env} isn't set: the server has no model endpoint of its own (connect one in Govern > Settings)")


def abi_token() -> str:
    if os.environ.get("ABI_TOKEN"):
        return os.environ["ABI_TOKEN"]
    user = os.environ.get("ABI_USER") or "admin"
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
