"""The engine heals itself (scripts/engine.sh's supervisor), on a private engine: its own data folder, Postgres and port,
removed after. kill -9 of the engine or a worker serves again within 60 s; a backup's pause keeps the workers stopped
until the backup ends; health fails on a stale worker heartbeat and passes on a fresh one; a stop (SIGTERM to the
supervisor) leaves nothing running, within Docker's 10 s. The image and compose wire it in."""
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "engine/backend/.venv/bin/python"


def until(ok, wait: float = 60) -> bool:
    end = time.time() + wait
    while not ok():
        if time.time() > end:
            return False
        time.sleep(0.5)
    return True


def test_a_killed_engine_or_worker_serves_again_and_a_backup_holds_the_workers():
    d = Path(tempfile.mkdtemp(prefix="abi-sv-", dir="/tmp"))  # short: Postgres's socket goes in it (at most 103 bytes)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ABI_", "PGHOST", "PGUSER", "PGDATABASE"))}
    env |= {"ABI_PI_DATA": str(d), "ABI_ENGINE_PORT": str(port), "ABI_PI_WORKSPACES": str(d / "ws")}
    sh = lambda *a: subprocess.run([ROOT / "scripts/engine.sh", *a], env=env, capture_output=True, text=True, timeout=180)  # noqa: E731
    pid = lambda p: int((d / f"run/{p}.pid").read_text())  # noqa: E731
    sql = lambda q: subprocess.run([PY, "-c", "import psycopg, sys; c = psycopg.connect(); c.execute(sys.argv[1]); c.commit()", q],  # noqa: E731
                                   env=env | {"PGHOST": str(d / "pg"), "PGUSER": "abi", "PGDATABASE": "abi"}, check=True)

    def alive(p: str) -> bool:
        try:
            os.kill(pid(p), 0)
            return True
        except (OSError, ValueError):
            return False

    def ready() -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ready", timeout=2) as r:
                return r.status == 200
        except OSError:
            return False
    try:
        r = sh("start")
        assert r.returncode == 0, r.stdout + r.stderr
        parts = sorted(f.stem for f in (d / "run").glob("*.pid") if f.stem not in ("supervisor", "pause"))
        workers = [p for p in parts if p != "app"]
        assert "app" in parts and "worker" in workers and alive("supervisor"), parts
        up = lambda n: all((d / f"logs/{w}.log").read_text().count("Starting worker on") == n for w in workers)  # noqa: E731
        assert until(lambda: up(1), 30), sh("status").stdout  # registered (a starting worker prunes stale ones' rows)

        # health: an app that doesn't answer, a stale heartbeat (frozen workers, theirs 3 minutes old), a fresh one
        assert sh("health").returncode == 0 and sh("health", f"http://127.0.0.1:{port}/api/health").returncode == 0
        r = sh("health", "http://127.0.0.1:9/")
        assert r.returncode == 1 and "app doesn't answer" in r.stdout, r.stdout
        try:
            for p in workers:
                os.kill(pid(p), signal.SIGSTOP)
            sql("UPDATE procrastinate_workers SET last_heartbeat = now() - interval '3 minutes'")
            r = sh("health")
            assert r.returncode == 1 and "no heartbeat" in r.stdout, r.stdout
        finally:
            for p in workers:
                os.kill(pid(p), signal.SIGCONT)
        assert until(lambda: sh("health").returncode == 0, 30), sh("health").stdout  # procrastinate beats every 10 s

        for p in parts:  # kill -9 the engine, then each worker: a new one in its pid file, the engine ready, within 60 s
            old = pid(p)
            os.kill(old, signal.SIGKILL)
            assert until(lambda: ready() and alive(p) and pid(p) != old), f"{p} isn't back: {sh('status').stdout}"
            assert re.search(rf"the {'engine' if p == 'app' else p} \(pid {old}\) ended with status 137 after \d+ s",
                             (d / "logs/supervisor.log").read_text()), (d / "logs/supervisor.log").read_text()
        assert until(lambda: up(2), 30), sh("status").stdout

        # a backup: the workers stop and stay stopped while it runs (its last tar waits on a pipe nobody reads yet), the
        # engine keeps serving and health excuses the workers; once it ends they start again
        (d / "ws").mkdir()
        (d / "files/big.bin").write_bytes(os.urandom(2**20))  # the backup's output can't fit in a pipe's buffer
        before = {p: pid(p) for p in workers}
        b = subprocess.Popen([ROOT / "scripts/backup.sh", "backup"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            assert until(lambda: (d / "run/pause.pid").exists() and not any(map(alive, workers)), 30), sh("status").stdout
            for _ in range(10):  # 5 s: the supervisor looks every second
                assert not any(map(alive, workers)) and ready(), sh("status").stdout
                time.sleep(0.5)
            assert sh("health").returncode == 0, "paused workers don't make it unhealthy"
            out, err = b.communicate(timeout=120)
        finally:
            b.kill()
        assert b.returncode == 0 and out[257:262] == b"ustar" and b"manifest.txt" in out[:100], err
        assert until(lambda: all(alive(p) and pid(p) != before[p] for p in workers), 30), sh("status").stdout
        assert not (d / "run/pause.pid").exists()
        log = (d / "logs/supervisor.log").read_text()
        assert f"the workers pause while process {b.pid} runs" in log and all(
            re.search(rf"the {p} \(pid {before[p]}\) ended with status 0 after \d+ s; it starts again after the backup", log)
            for p in workers), log

        # a backup killed with SIGKILL leaves run/pause.pid; a process that later has its PID (here the supervisor's)
        # doesn't hold the workers: the pause names the backup's start time too
        (d / "run/pause.pid").write_text(f"{pid('supervisor')} Thu Jan  1 00:00:00 1970\n")
        stale = pid(workers[0])
        os.kill(stale, signal.SIGKILL)
        assert until(lambda: alive(workers[0]) and pid(workers[0]) != stale), sh("status").stdout
        (d / "run/pause.pid").unlink()

        # the supervisor itself gone: health says so; start gives a new one that keeps what runs and restarts it too
        running = {p: pid(p) for p in parts}
        os.kill(pid("supervisor"), signal.SIGKILL)
        assert until(lambda: not alive("supervisor"), 10)
        r = sh("health")
        assert r.returncode == 1 and "Nothing restarts the engine" in r.stdout, r.stdout
        assert sh("start").returncode == 0 and alive("supervisor") and {p: pid(p) for p in parts} == running
        assert until(lambda: (d / "logs/supervisor.log").read_text().count("the supervisor started") == 2, 10)  # it looked
        os.kill(running["app"], signal.SIGKILL)
        assert until(lambda: ready() and alive("app") and pid("app") != running["app"]), sh("status").stdout
        assert re.search(rf"the engine \(pid {running['app']}\) ended after \d+ s; it starts again in \d+ s",
                         (d / "logs/supervisor.log").read_text()), (d / "logs/supervisor.log").read_text()

        # stop: TERM to the supervisor ends each part, then itself, then Postgres goes; nothing is left running
        last, t = {p: pid(p) for p in ["supervisor", *parts]}, time.time()
        r = sh("stop")
        assert r.returncode == 0 and time.time() - t < 10, (r.stdout + r.stderr, time.time() - t)
        for p, x in last.items():
            try:
                os.kill(x, 0)
                raise AssertionError(f"{p} ({x}) still runs")
            except ProcessLookupError:
                pass
        assert not list((d / "run").glob("*.pid")) and not (d / "pg/postmaster.pid").exists()
        assert "stopped the engine and the workers" in (d / "logs/supervisor.log").read_text()
    finally:
        sh("stop")
        shutil.rmtree(d, ignore_errors=True)


def test_the_image_checks_health_and_compose_passes_the_memory_settings(tmp_path):
    """Docker's HEALTHCHECK is engine.sh health on the app's page (1.1 GETs the page only: a dead engine or worker
    stayed "healthy"); compose passes DuckDB's memory limit and the query cache's size, with the engine's defaults."""
    from test_https import compose
    assert re.search(r'^HEALTHCHECK .* CMD \["/app/scripts/engine.sh", "health", "http://127\.0\.0\.1:8790/"\]$',
                     (ROOT / "Dockerfile").read_text(), re.M)
    env = compose(tmp_path, "")["abi"]["environment"]
    assert env["ABI_DUCKDB_MEMORY_LIMIT"] == "1500MB" and env["ABI_QUERY_CACHE_MB"] == "256", env
    assert env["ABI_DUCKDB_THREADS"] == "", env  # storage.THREADS: half the CPUs
    env = compose(tmp_path, "ABI_DUCKDB_MEMORY_LIMIT=3GB\nABI_QUERY_CACHE_MB=512\n")["abi"]["environment"]
    assert env["ABI_DUCKDB_MEMORY_LIMIT"] == "3GB" and env["ABI_QUERY_CACHE_MB"] == "512", env
