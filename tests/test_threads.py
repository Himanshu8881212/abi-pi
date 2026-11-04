"""1.2 M3 round 2: one project's lake runs half the CPUs this process may use (or ABI_DUCKDB_THREADS), never DuckDB's own
count. On pve1 (4 cores, 8 threads; the container may use 6) DuckDB counted 8 for every lake of every process, and a
100M-row load in the heavy worker, then build_model in the engine, took every core: another project's queries ran
x2.38 (p50) and x2.98 (p95) slower. With half: x1.18 and x1.37 (bench/RESULTS.md, 1.2 M3 round 2)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / "engine/backend/.venv/bin/python"
THREADS = """import os, sys
from abi import storage
con = storage.connect("pi_threads", catalog=sys.argv[1], data_path=sys.argv[2])
print(con.execute("SELECT current_setting('threads')").fetchone()[0], os.process_cpu_count())"""


def test_a_lake_runs_half_the_cpus_this_process_may_use_or_the_threads_it_is_given(tmp_path):
    def threads(**env) -> list[int]:
        base = {k: v for k, v in os.environ.items() if k != "ABI_DUCKDB_THREADS"}
        r = subprocess.run([PY, "-c", THREADS, str(tmp_path / "catalog.ducklake"), str(tmp_path / "lake")], cwd=ROOT / "engine/backend",
                           env=base | env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        return [int(x) for x in r.stdout.split()]
    have, cpus = threads()
    assert have == max(1, cpus // 2), (have, cpus)
    assert threads(ABI_DUCKDB_THREADS="3")[0] == 3
    assert threads(ABI_DUCKDB_THREADS="")[0] == have  # compose's empty default
