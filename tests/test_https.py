"""HTTPS in one command: the https compose profile puts Caddy (docker/Caddyfile) in front of the app, the app's own port
stays on this machine with ABI_BIND, and ABI_PUBLIC_URL follows ABI_DOMAIN; scripts/upgrade.sh brings the profile along
and waits for its own install's container."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def compose(tmp_path, env: str) -> dict:
    """The compose file as docker compose reads it with this .env (no daemon needed), nothing from this shell."""
    if not shutil.which("docker") or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode:
        pytest.skip("docker compose not installed")
    (tmp_path / "env").write_text("ABI_USER=admin\nABI_PASSWORD=x\n" + env)
    r = subprocess.run(["docker", "compose", "-f", ROOT / "docker-compose.yml", "--env-file", tmp_path / "env", "config", "--format", "json"],
                       capture_output=True, text=True, env={k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_CONFIG") if k in os.environ}, check=True)
    assert "is not set" not in r.stderr, r.stderr  # compose 2.26 (Debian's) warns on ${X:+${X}} with X unset: ${X:+${X:-}}
    return json.loads(r.stdout)["services"]


def test_the_https_profile_puts_caddy_in_front_and_keeps_the_app_port_local(tmp_path):
    ports = lambda s: sorted((p.get("host_ip"), p["published"], p["target"]) for p in s["ports"])  # noqa: E731
    plain = compose(tmp_path, "")  # no profile: as 1.1, the app alone on every interface
    assert list(plain) == ["abi"] and ports(plain["abi"]) == [(None, "8790", 8790)] and plain["abi"]["environment"]["ABI_PUBLIC_URL"] == ""

    s = compose(tmp_path, "COMPOSE_PROFILES=https\nABI_DOMAIN=abi.example.com\nABI_BIND=127.0.0.1\n")  # a public name
    app, caddy = s["abi"], s["https"]
    assert ports(app) == [("127.0.0.1", "8790", 8790)] and app["environment"]["ABI_PUBLIC_URL"] == "https://abi.example.com"
    assert re.fullmatch(r"caddy:\d+\.\d+\.\d+", caddy["image"]) and ports(caddy) == [(None, "443", 443), (None, "80", 80)]
    assert caddy["environment"] == {"ABI_DOMAIN": "abi.example.com", "ABI_HTTPS_PORT": "443", "ABI_TLS": ""}
    mounts = {v["target"]: (v["source"], v.get("read_only")) for v in caddy["volumes"]}
    assert mounts["/etc/caddy/Caddyfile"] == (str(ROOT / "docker/Caddyfile"), True) and mounts["/data"][0] == "caddy"

    s = compose(tmp_path, "COMPOSE_PROFILES=https\nABI_DOMAIN=10.0.0.5\nABI_TLS=internal\nABI_HTTPS_PORT=8443\nABI_HTTP_PORT=8080\n")  # a LAN address
    assert s["abi"]["environment"]["ABI_PUBLIC_URL"] == "https://10.0.0.5:8443" and ports(s["abi"]) == [(None, "8790", 8790)]
    assert ports(s["https"]) == [(None, "8080", 80), (None, "8443", 8443)]  # inside too: Caddy's redirect names the port
    assert s["https"]["environment"] == {"ABI_DOMAIN": "10.0.0.5", "ABI_HTTPS_PORT": "8443", "ABI_TLS": "internal"}

    s = compose(tmp_path, "COMPOSE_PROFILES=https\nABI_DOMAIN=abi.corp\nABI_TLS_CERT=/etc/ssl/abi.crt\nABI_TLS_KEY=/etc/ssl/abi.key\n"
                          "ABI_PUBLIC_URL=https://bi.corp\n")  # your own certificate; an ABI_PUBLIC_URL of your own wins
    mounts = {v["target"]: (v["source"], v.get("read_only")) for v in s["https"]["volumes"]}
    assert mounts["/certs/cert.pem"] == ("/etc/ssl/abi.crt", True) and mounts["/certs/key.pem"] == ("/etc/ssl/abi.key", True)
    assert s["https"]["environment"]["ABI_TLS"] == "/certs/cert.pem /certs/key.pem" and s["abi"]["environment"]["ABI_PUBLIC_URL"] == "https://bi.corp"

    caddyfile = (ROOT / "docker/Caddyfile").read_text()  # the names compose passes, to the app's port, streamed
    assert "{$ABI_DOMAIN}:{$ABI_HTTPS_PORT:443} {" in caddyfile and "tls {$ABI_TLS} {" in caddyfile
    assert "default_sni {$ABI_DOMAIN}" in caddyfile  # a browser sends no SNI for an IP: without it a LAN address's handshake fails
    assert f"reverse_proxy abi:{app['ports'][0]['target']} {{" in caddyfile and "flush_interval -1" in caddyfile


def test_upgrade_brings_the_profiles_along_and_waits_for_its_own_container(tmp_path):
    """Another install on the same machine (a test one beside the live one) must not be the container upgrade.sh waits
    for: it backs up this folder's container, runs plain `docker compose up -d` (which reads COMPOSE_PROFILES from
    .env) and reads the version from this install's new container."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts/upgrade.sh", tmp_path / "scripts")
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=https\nABI_DOMAIN=abi.example.com\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/docker").write_text(f"""#!/bin/sh
echo "$*" >> {tmp_path}/calls
case "$*" in
  ps*service=abi) echo old ;;
  "exec old scripts/backup.sh backup") echo backup ;;
  "compose ps -q abi") echo new ;;
  inspect*) echo healthy ;;
  "exec new cat /data/VERSION") echo 1.2.0 ;;
  "exec abi cat /data/VERSION") echo another install ;;
esac
""")
    (tmp_path / "bin/docker").chmod(0o755)
    r = subprocess.run(["sh", tmp_path / "scripts/upgrade.sh"], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"})
    calls = (tmp_path / "calls").read_text().splitlines()
    assert r.returncode == 0 and r.stdout.endswith("ok: 1.2.0\n"), r.stdout + r.stderr
    assert "compose up -d" in calls and not [c for c in calls if re.match(r"(inspect|exec) abi\b", c)]
    assert [f.read_text() for f in (tmp_path / "backups").iterdir()] == ["backup\n"]
