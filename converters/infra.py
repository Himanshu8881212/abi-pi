#!/usr/bin/env python3
"""Outside the app: the local RustFS (S3) that holds the demo's Parquet files.

    converters/.venv/bin/python converters/infra.py up        start abi-pi-rustfs on 127.0.0.1:9100, bucket yt-cms
    converters/.venv/bin/python converters/infra.py status

The upload keys are generated here, kept only in the macOS Keychain (service abi-pi-rustfs) and handed to Docker and
the uploaders through the environment; they are never printed or written to a file. The bucket is read-only for
anonymous requests, so ABI reads it without any secret; the container listens on 127.0.0.1 only.
"""
import datetime
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

NAME, IMAGE, PORT, BUCKET = "abi-pi-rustfs", "rustfs/rustfs:1.0.2", 9100, "yt-cms"
ENDPOINT = f"http://127.0.0.1:{PORT}"
DATA = Path(__file__).resolve().parent.parent / "scratch" / "infra" / "rustfs"
KEYCHAIN = "abi-pi-rustfs"


def keys() -> tuple[str, str]:
    """The upload keys from the Keychain; created on first use."""
    def get(acct):
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", acct, "-w"], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    kid, sec = get("key_id"), get("secret")
    if not (kid and sec):
        kid, sec = "abipi" + secrets.token_hex(6), secrets.token_urlsafe(30)
        for acct, val in (("key_id", kid), ("secret", sec)):
            subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", acct, "-w", val], check=True, capture_output=True)
    return kid, sec


def signed(method: str, path: str, body: bytes = b"", query: str = "") -> urllib.request.Request:
    """AWS Signature V4 for one S3 request (stdlib only)."""
    kid, sec = keys()
    now = datetime.datetime.now(datetime.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = f"127.0.0.1:{PORT}"
    ph = hashlib.sha256(body).hexdigest()
    canon = f"{method}\n{path}\n{query}\nhost:{host}\nx-amz-content-sha256:{ph}\nx-amz-date:{amz}\n\nhost;x-amz-content-sha256;x-amz-date\n{ph}"
    scope = f"{day}/us-east-1/s3/aws4_request"
    sts = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{hashlib.sha256(canon.encode()).hexdigest()}"
    k = f"AWS4{sec}".encode()
    for part in (day, "us-east-1", "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={kid}/{scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}"
    url = f"{ENDPOINT}{path}" + (f"?{query}" if query else "")
    return urllib.request.Request(url, data=body or None, method=method,
                                  headers={"x-amz-date": amz, "x-amz-content-sha256": ph, "Authorization": auth})


def s3(method: str, path: str, body: bytes = b"", query: str = "") -> int:
    try:
        with urllib.request.urlopen(signed(method, path, body, query), timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def up() -> dict:
    kid, sec = keys()
    DATA.mkdir(parents=True, exist_ok=True)
    running = subprocess.run(["docker", "ps", "-q", "-f", f"name=^{NAME}$"], capture_output=True, text=True).stdout.strip()
    if not running:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
        env = {**os.environ, "RUSTFS_ACCESS_KEY": kid, "RUSTFS_SECRET_KEY": sec}  # `-e NAME` reads them from here
        subprocess.run(["docker", "run", "-d", "--name", NAME, "--restart", "unless-stopped",
                        "-p", f"127.0.0.1:{PORT}:9000", "-p", f"127.0.0.1:{PORT + 1}:9001", "-v", f"{DATA}:/data",
                        "-e", "RUSTFS_ACCESS_KEY", "-e", "RUSTFS_SECRET_KEY", IMAGE], env=env, check=True, capture_output=True)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{ENDPOINT}/", timeout=2)
            break
        except urllib.error.HTTPError:
            break  # an S3 error page: it answers
        except OSError:
            time.sleep(1)
    made = s3("PUT", f"/{BUCKET}")
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{BUCKET}/*"]},
        {"Effect": "Allow", "Principal": {"AWS": ["*"]}, "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{BUCKET}"]}]}
    pol = s3("PUT", f"/{BUCKET}", json.dumps(policy).encode(), "policy=")
    return {"endpoint": ENDPOINT, "bucket": BUCKET, "bucket_put": made, "policy_put": pol, "console": f"http://127.0.0.1:{PORT + 1}"}


def status() -> dict:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/{BUCKET}?list-type=2&max-keys=5", timeout=5) as r:
            anon = r.status
    except urllib.error.HTTPError as e:
        anon = e.code
    except OSError:
        anon = "down"
    return {"endpoint": ENDPOINT, "anonymous_list": anon}


def env() -> dict:
    """For uploaders in this process tree: the keys as AWS_* variables (never printed)."""
    kid, sec = keys()
    return {"AWS_ACCESS_KEY_ID": kid, "AWS_SECRET_ACCESS_KEY": sec, "S3_ENDPOINT": ENDPOINT}


if __name__ == "__main__":
    cmd = (sys.argv[1:] or ["status"])[0]
    print(json.dumps({"up": up, "status": status}[cmd](), indent=1))
