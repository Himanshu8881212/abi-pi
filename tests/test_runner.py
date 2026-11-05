"""1.2 M4.1 the on-demand runner, on a private engine (its own data folder, Postgres and port, removed after): the heavy
worker starts within seconds of a heavy job and stops ABI_RUNNER_IDLE after the queue empties; while a runner beats
(ABI_ROLE=runner, compose's runner profile) the main role takes no heavy job; abi-run runs one queued job and says by
its exit code how it went; a backup's pause holds the runner too and lets its running job finish first. The compose
file with and without the runner profile."""
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import duckdb

import abi

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "engine/backend/.venv/bin/python"
P = "pi_runner_test"
IDLE = 4


def until(ok, wait: float = 60) -> bool:
    end = time.time() + wait
    while not ok():
        if time.time() > end:
            return False
        time.sleep(0.5)
    return True


def test_heavy_jobs_start_the_runner_and_it_goes_away(tmp_path):
    d = Path(tempfile.mkdtemp(prefix="abi-rn-", dir="/tmp"))  # short: Postgres's socket goes in it (at most 103 bytes)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ABI_", "PGHOST", "PGUSER", "PGDATABASE"))}
    env |= {"ABI_PI_DATA": str(d), "ABI_ENGINE_PORT": str(port), "ABI_PI_WORKSPACES": str(d / "ws"),
            "ABI_RUNNER_IDLE": str(IDLE), "ABI_INLINE_ROWS": "1000"}
    pg = env | {"PGHOST": str(d / "pg"), "PGUSER": "abi", "PGDATABASE": "abi"}
    sh = lambda *a: subprocess.run([ROOT / "scripts/engine.sh", *a], env=env, capture_output=True, text=True, timeout=180)  # noqa: E731
    run = lambda job: subprocess.run([ROOT / "scripts/abi-run", str(job)], env=env, capture_output=True, text=True, timeout=300)  # noqa: E731
    sql = lambda q, *a: subprocess.run([PY, "-c", "import psycopg, sys; c = psycopg.connect(); r = c.execute(sys.argv[1], sys.argv[2:] or None)"  # noqa: E731
                                        ".fetchall(); c.commit(); print(r[0][0] if r else '')", q, *a],
                                       env=pg, capture_output=True, text=True, check=True).stdout.strip()
    pid = lambda p: int((d / f"run/{p}.pid").read_text())  # noqa: E731
    log = lambda f="supervisor": (d / f"logs/{f}.log").read_text() if (d / f"logs/{f}.log").exists() else ""  # noqa: E731

    def alive(p: str) -> bool:
        try:
            os.kill(pid(p), 0)
            return True
        except (OSError, ValueError):
            return False

    def queued(table: str, file: str) -> str:
        r = abi.call("add_data", project=P, table=table, location=f"pi_run/{file}")
        assert r["status"] == "queued", r
        return r["run"]

    def job(load_run: str) -> int:
        return int(sql("SELECT id FROM procrastinate_jobs WHERE task_name = 'abi.worker.load' AND args->>'run' = %s", load_run))

    def status(load_run: str) -> str:
        return abi.call("load_runs", project=P, run=load_run)["runs"][0]["status"]

    def exact(table: str) -> list:
        return abi.call("sql_query", project=P, sql=f"SELECT count(*), sum(amount) FROM {table}")["rows"]

    def beating():  # a runner elsewhere, as far as the main role can tell
        while not beat.is_set():
            (d / "run/runner.beat").touch()
            time.sleep(2)

    url, beat, runner = abi.URL, threading.Event(), None
    try:
        r = sh("start")
        assert r.returncode == 0, r.stdout + r.stderr
        assert until(lambda: "Starting worker on" in log("worker"), 30) and not alive("worker_heavy")  # nothing to do yet
        abi.URL, pw = f"http://127.0.0.1:{port}", secrets.token_urlsafe(16)
        urllib.request.urlopen(urllib.request.Request(abi.URL + "/api/setup", f'{{"name": "owner", "password": "{pw}"}}'.encode(),
                                                      {"Content-Type": "application/json"}), timeout=30)
        (d / "files/pi_run").mkdir(parents=True)
        for f, n in (("a", 5000), ("b", 7000), ("c", 3000), ("broken", 200_000)):
            duckdb.sql(f"COPY (SELECT range AS id, range % 1000 AS amount FROM range({n})) TO '{d}/files/pi_run/{f}.parquet' (FORMAT parquet)")
        with (d / "files/pi_run/broken.parquet").open("r+b") as f:  # its footer whole (it says 200,000 rows), its middle not
            f.seek((d / "files/pi_run/broken.parquet").stat().st_size // 3)
            f.write(b"\xff" * 65536)
        with abi.as_user(abi.login("owner", pw)[1]["token"]):
            abi.call("create_project", project=P, name=P)

            # a heavy job with the heavy worker gone: it starts within seconds, the load lands exact, and the worker
            # stops IDLE s after the queue empties (1.2 before: always running, holding the memory its last job took)
            t0, a = time.time(), queued("a", "a.parquet")
            assert until(lambda: alive("worker_heavy"), 15), log()
            assert until(lambda: status(a) == "done", 60) and exact("a") == [[5000, 5 * 499_500]], abi.call("load_runs", project=P)
            started = time.time() - t0
            assert until(lambda: not alive("worker_heavy"), IDLE + 15), log()
            assert until(lambda: f"the heavy queue was empty {IDLE} s" in log(), 5) and started < 15, (started, log())

            # a runner elsewhere beats (here this test, as a launcher would): the main role starts no heavy worker, and
            # abi-run runs a queued job to its end, 0; one whose file breaks mid-load, 1 with ABI's error and Git clean
            threading.Thread(target=beating, daemon=True).start()
            assert until(lambda: "a runner takes the heavy jobs" in sh("status").stdout, 10)
            b = queued("b", "b.parquet")
            time.sleep(5)
            assert status(b) == "queued" and not alive("worker_heavy"), log()
            r = run(job(b))
            assert r.returncode == 0 and status(b) == "done" and exact("b") == [[7000, 7 * 499_500]], r.stderr[-2000:]
            repo = d / "projects" / P
            git = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True).stdout  # noqa: E731
            head, staged = git("rev-parse", "HEAD"), git("status", "--porcelain")  # a's and b's add_data, not committed
            r = run(job(x := queued("broken", "broken.parquet")))
            assert r.returncode == 1 and status(x) == "failed", (r.returncode, r.stderr[-2000:])
            assert "Loading 'pi_run/broken.parquet' failed" in r.stderr.splitlines()[-1], r.stderr[-2000:]
            assert git("rev-parse", "HEAD") == head and git("status", "--porcelain") == staged, staged
            assert not (repo / "sources/broken.json").exists()
            assert run(job(b)).returncode == 2 and run(999_999).returncode == 2  # done already; no such job
            assert "started the worker_heavy" not in log().split(f"the heavy queue was empty {IDLE} s")[-1], log()

            # the runner role (compose's runner profile) on the same data: it takes the heavy jobs, the main role none
            beat.set()
            runner = subprocess.Popen([ROOT / "scripts/engine.sh", "runner"], env=env | {"ABI_ROLE": "runner"},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert until(lambda: "a runner takes the heavy jobs" in sh("status").stdout, 15)
            c = queued("c", "c.parquet")
            assert until(lambda: status(c) == "done", 60) and exact("c") == [[3000, 3 * 499_500]], log("runner_supervisor")
            assert "started the runner" in log("runner_supervisor") and "Starting worker on" in log("runner"), log("runner_supervisor")
            assert until(lambda: not alive("runner"), IDLE + 15)
            assert until(lambda: f"the heavy queue was empty {IDLE} s" in log("runner_supervisor"), 5), log("runner_supervisor")
            assert log().count("started the worker_heavy") == 1 and not alive("worker_heavy"), log()
            r = subprocess.run([ROOT / "scripts/engine.sh", "health"], env=env | {"ABI_ROLE": "runner"}, capture_output=True, text=True)
            assert r.returncode == 0, r.stdout

            # a backup: the runner's running job finishes before the backup copies, the next waits until it ends
            hold = subprocess.Popen([PY, "-c", """
import psycopg, sys, uuid
run = str(uuid.uuid4())
c = psycopg.connect(autocommit=True)
c.execute("INSERT INTO ml_runs (id, project, model, status, spec) VALUES (%s, 'pi_runner_test', 'nap', 'queued', '{}')", [run])
h = psycopg.connect()  # the training's first step (ml.job: UPDATE ml_runs ... WHERE status = 'queued') waits on this row
h.execute("SELECT 1 FROM ml_runs WHERE id = %s FOR UPDATE", [run])
print(c.execute("INSERT INTO procrastinate_jobs (queue_name, task_name, args) VALUES ('heavy', 'abi.worker.train_model', %s)"
                " RETURNING id", [psycopg.types.json.Jsonb({"run_id": run})]).fetchone()[0], flush=True)
sys.stdin.readline()
h.execute("UPDATE ml_runs SET status = 'failed' WHERE id = %s", [run])
h.commit()
"""], env=pg, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            held = int(hold.stdout.readline())
            st = lambda j: sql("SELECT status::text FROM procrastinate_jobs WHERE id = %s", str(j))  # noqa: E731
            assert until(lambda: st(held) == "doing", 30), log("runner_supervisor")
            (d / "ws").mkdir(exist_ok=True)
            with (tmp_path / "backup.tar").open("wb") as out:
                bk = subprocess.Popen([ROOT / "scripts/backup.sh", "backup"], env=env, stdout=out, stderr=subprocess.PIPE)
                try:
                    assert until(lambda: (d / "run/pause.pid").exists(), 15)
                    after = int(sql("INSERT INTO procrastinate_jobs (queue_name, task_name, args) VALUES ('heavy', 'abi.worker.train_model',"
                                    " jsonb_build_object('run_id', gen_random_uuid()::text)) RETURNING id"))
                    for _ in range(10):  # 5 s: the backup waits for the running job, the next job waits for the backup
                        assert bk.poll() is None and st(held) == "doing" and st(after) == "todo", log("runner_supervisor")
                        time.sleep(0.5)
                    hold.communicate("go\n", timeout=30)
                    _, err = bk.communicate(timeout=120)
                finally:
                    bk.kill()
            assert bk.returncode == 0 and st(held) == "succeeded", err
            assert until(lambda: st(after) == "succeeded", 60), log("runner_supervisor")  # after the backup
            assert log().count("started the worker_heavy") == 1, log()
    finally:
        beat.set()
        abi.URL = url
        if runner:
            runner.send_signal(signal.SIGTERM)
            runner.wait(30)
        sh("stop")
        shutil.rmtree(d, ignore_errors=True)


def test_the_runner_profile_adds_a_runner_container_and_a_plain_install_is_as_before(tmp_path):
    """No profile: the app's container alone, as 1.1 (its 1.1 settings unchanged, no memory limit, no role). The runner
    profile (with https too) adds abi-runner: the same image and volume, ABI_ROLE=runner, its memory limit with no swap
    past it and its own DuckDB limit, no port (Postgres stays a socket in the volume); the app's container is the same."""
    from test_https import compose
    plain = compose(tmp_path, "")
    assert list(plain) == ["abi"] and "mem_limit" not in plain["abi"] and "ABI_ROLE" not in plain["abi"]["environment"]
    old = subprocess.run(["git", "-C", ROOT, "show", "v1.1.0:docker-compose.yml"], capture_output=True, text=True, check=True).stdout
    (tmp_path / "old").mkdir()
    (tmp_path / "old/docker-compose.yml").write_text(old)
    r = subprocess.run(["docker", "compose", "-f", tmp_path / "old/docker-compose.yml", "--env-file", tmp_path / "env", "config", "--format", "json"],
                       capture_output=True, text=True, check=True, env={k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_CONFIG") if k in os.environ})
    was, now = json.loads(r.stdout)["services"]["abi"], plain["abi"]
    assert {k: now["environment"].get(k) for k in was["environment"]} == was["environment"]  # every 1.1 setting as it was
    assert (was["image"], was["container_name"], was["restart"]) == (now["image"], now["container_name"], now["restart"])
    assert all(v in now["volumes"] for v in was["volumes"]) and "mem_limit" not in was
    s = compose(tmp_path, "COMPOSE_PROFILES=runner\n")
    assert sorted(s) == ["abi", "runner"] and s["abi"] == plain["abi"], s
    r = s["runner"]
    assert r["image"] == plain["abi"]["image"] and r["container_name"] == "abi-runner" and "ports" not in r
    assert r["mem_limit"] == r["memswap_limit"] == str(4 * 2**30) and r["restart"] == "unless-stopped"
    assert [(v["source"], v["target"]) for v in r["volumes"]] == [("abi-data", "/data")]
    e = r["environment"]
    assert e["ABI_ROLE"] == "runner" and e["ABI_RUNNER_IDLE"] == "180" and e["ABI_DUCKDB_MEMORY_LIMIT"] == "2GB", e
    assert not {"ABI_USER", "ABI_PASSWORD", "ABI_PI_LLM_KEY", "ABI_PI_SECRET_KEY"} & set(e), e  # no secret it doesn't need
    s = compose(tmp_path, "COMPOSE_PROFILES=https,runner\nABI_DOMAIN=abi.example.com\nABI_RUNNER_MEMORY=6g\nABI_RUNNER_DUCKDB_MEMORY_LIMIT=3GB\n")
    assert sorted(s) == ["abi", "https", "runner"] and s["runner"]["mem_limit"] == s["runner"]["memswap_limit"] == str(6 * 2**30)
    assert s["runner"]["environment"]["ABI_DUCKDB_MEMORY_LIMIT"] == "3GB" and s["runner"]["environment"]["ABI_PUBLIC_URL"] == "https://abi.example.com"
    assert s["abi"]["environment"]["ABI_DUCKDB_MEMORY_LIMIT"] == "1500MB"
