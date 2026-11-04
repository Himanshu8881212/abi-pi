"""bench/perf.py on this machine's engine, no model: load, isolation and the medallion rebuild at the smallest Brewline,
pages and Publish through an app started here (signed in as the owner, with and without row-level security), memory
idle and with a stand-in for Pi, a killed process (a stand-in named by its pid file). Each leaves no project, account,
rule, file or change behind."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

import pytest
from conftest import drop, parquet

import abi
import app
from pi_driver import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import perf  # noqa: E402

P = "perf_test"  # a bench project: pages and memory commit to it


def run(capsys, *argv) -> dict:
    perf.main(list(argv))
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def projects() -> set:
    return {x["project"] for x in abi.call("list_projects")["projects"]}


def test_load_and_isolation_measure_and_leave_nothing(capsys, monkeypatch):
    before, files = projects(), set(perf.FILES.iterdir())
    r = run(capsys, "load", "--rows", "37000")
    assert r["ready"] and r["exact"] and 30_000 < r["rows"] < 45_000 and r["rows_per_s"] > 0 and r["lake_bytes"] > 100_000, r
    monkeypatch.setattr(perf, "copies", lambda p: None)  # no wait for the worker's minute
    r = run(capsys, "isolation", "--rows", "37000", "--queries", "5")
    assert r["idle"]["n"] == 5 and r["during_load"]["n"] >= 1 and "load_error" not in r and r["x_p50"] > 0, r
    r = run(capsys, "medallion", "--rows", "37000")  # 1.1: silver and gold each load the orders source again
    assert r["exact"] and set(r["build_s"]) == {"bronze_cafes", "bronze_products", "bronze_orders", "silver_orders", "gold_revenue"}, r
    rebuilt = [x for x in r["rebuild"] if x.startswith("refresh ")]  # a query copy the worker writes meanwhile isn't one
    assert rebuilt[0] == "refresh bronze_orders" and sorted(rebuilt[1:]) == ["refresh gold_revenue", "refresh silver_orders"], r
    assert r["rebuild_bytes"]["raw"] > 0 and r["rebuild_bytes"]["model"] > 0 and r["lake"]["raw"] > 100_000 and r["rebuild_s"] > 0, r
    assert projects() == before and set(perf.FILES.iterdir()) == files


def test_a_chats_pi_counts_also_once_it_renamed_itself():
    """Pi starts as `node .../pi --mode rpc ...` and soon shows only `pi` (ps on Linux): both are a chat's agent."""
    procs = {10: (7, 104704, "pi"), 11: (7, 25728, "node /usr/local/bin/pi --mode rpc --session s.jsonl"),
             12: (7, 3164, "git -C x fetch -q"), 13: (8, 104704, "pi"), 14: (7, 900, "python3 pip.py")}
    assert perf.pis(7, procs) == [10, 11]


def test_a_bench_project_is_made_as_the_baselines_were(capsys):
    with pytest.raises(SystemExit, match="bench_"):
        perf.main(["project", "brewline"])
    try:
        r = run(capsys, "project", "perf_made_test", "--scale", "0.005")
        assert r["ready"] and r["report"] == "overview" and 30_000 < r["orders"] < 45_000, r
        assert set(abi.call("semantic_model", project="perf_made_test")["tables"]) >= {"cafes", "customers", "products", "orders"}
    finally:
        drop(["perf_made_test"])


def test_pages_and_memory_through_an_app_here(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("ABI_USER", "himanshu")
    monkeypatch.delenv("ABI_PASSWORD", raising=False)  # the bench reads data/accounts.txt, as scripts/creds.py does
    if not perf.password("himanshu"):
        pytest.skip("no accounts file")
    with pytest.raises(SystemExit, match="isn't a bench project"):  # someone's project gets no bench commits or accounts
        perf.main(["pages", "--url", "http://127.0.0.1:9", "--project", "main"])
    monkeypatch.setattr(perf, "copies", lambda p: None)  # no wait for the worker's minute
    if P in projects():  # an interrupted run's
        drop([P])
    (tmp_path / "orders.csv").write_text("order_id,order_date,region,amount\n" + "".join(
        f"{d * 2 + i},2026-05-{d:02d},{r},{d * 3 + i}\n" for d in range(1, 29) for i, r in enumerate(["North", "South"])))
    abi.load("demo", P, parquet(tmp_path / "orders.csv"))
    s, stand = app.serve(P, 0, open_browser=False, signin=True), []
    try:
        users = {u["name"] for u in abi.call("list_users")["users"]}
        measures = abi.call("semantic_model", project=P, all=True)["measures"]
        url, head = f"http://127.0.0.1:{s.server_address[1]}", abi.call("history", project=P)["commits"][0]["id"]
        abi.call("set_measure", project=P, id="work_in_progress", name="Someone's", expression=measures[0]["expression"])
        for bench in (["pages", "--sessions", "2"], ["memory", "--chats", "1", "--app-pid", str(os.getpid())]):
            with pytest.raises(SystemExit, match="uncommitted changes"):
                perf.main([*bench, "--url", url, "--project", P])
        assert abi.call("history", project=P)["commits"][0]["id"] == head, "someone's uncommitted work is never committed"
        perf.sure("delete_measure", project=P, id="work_in_progress")
        for rls in ([], ["--rls", "orders.region"]):  # Viewers and Developers; then Viewers each with their own region
            r = run(capsys, "pages", "--url", url, "--project", P, "--sessions", "4", "--people", "2", *rls)
            for k in ("first", "after_commit"):
                assert r[k]["errors"] == r[k]["dropped"] == 0 and 0 < r[k]["p50"] <= r[k]["p95"] <= r[k]["p99"] <= r[k]["max"], r
                assert len(r[k]["snapshots"]) == 2 and "uncommitted" not in r, r
            log = [c["message"].strip() for c in abi.call("history", project=P)["commits"][:2]]
            assert re.fullmatch(r"perf\.py \d{10}: probe and accounts removed", log[0]) and log[1].endswith(": page check"), log
            assert not abi.call("changes", project=P)["files"], "the probe's removal is committed too"
            assert abi.call("semantic_model", project=P, all=True)["measures"] == measures
            assert {u["name"] for u in abi.call("list_users")["users"]} == users, "the throwaway accounts are gone"
        assert r["rls"] == "orders.region" and not abi.call("rls_rules", project=P)["rules"], "the rule is gone"
        before, files = projects(), set(perf.FILES.iterdir())  # a load through the app's upload, as a person's browser sends it
        r = run(capsys, "load", "--rows", "37000", "--door", "app", "--url", url)
        assert r["door"] == "app" and r["ready"] and r["exact"] and 30_000 < r["rows"] < 45_000 and r["tables_s"]["orders"] > 0, r
        assert projects() == before and set(perf.FILES.iterdir()) == files
        assert not [*abi.WORKSPACES.glob("*/perf_*_load"), *abi.WORKSPACES.glob("*/_*/perf_*_load")], "its copy and folders are gone"
        # a chat's memory with a stand-in for Pi (no model: it takes the message and never answers), so no tokens
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin/pi").write_text("#!/usr/bin/env python3\nimport sys\nfor _ in sys.stdin:\n    pass\n")
        (tmp_path / "bin/pi").chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setattr(perf.settings, "load", lambda: {"model": None, "thinking": "medium"})  # the app's chats read it too
        monkeypatch.delenv("ABI_PI_LLM_MODEL", raising=False)
        r = run(capsys, "memory", "--url", url, "--project", P, "--chats", "1", "--app-pid", str(os.getpid()), "--hold", "0.5")
        # the app here is this pytest process, whose memory moves by itself (once -65 MB between samples): Pi's own counts
        assert r["started"]["pi"]["chats"] == 1 and r["started"]["pi"]["mb"] > 0 and "started" in r["per_chat_mb"] and "answered" not in r, r
        assert r["held"]["pi"]["chats"] == 1, "the chat stays open while held"
        assert not abi.call("changes", project=P)["files"], "roles.json without the account is committed"
        assert {u["name"] for u in abi.call("list_users")["users"]} == users
        r = run(capsys, "memory", "--project", P, "--chats", "0", "--app-pid", str(os.getpid()))
        assert all(r["idle"][k]["n"] >= 1 and r["idle"][k]["mb"] > 0 for k in ("app", "engine", "worker", "postgres")), r

        def late(*_):
            raise TimeoutError("timed out")
        with monkeypatch.context() as m:  # an upload the app drops or gives up on is a result, and leaves nothing either
            m.setattr(perf, "upload", late)
            r = run(capsys, "load", "--rows", "37000", "--door", "app", "--url", url)
        assert r["error"] == "TimeoutError: timed out" and r["error_s"] >= 0 and r["tables_s"] == {} and "exact" not in r, r
        assert projects() == before and set(perf.FILES.iterdir()) == files
        head = abi.call("history", project=P)["commits"][0]["id"]  # Publish as the button does, first and measure-only
        r = run(capsys, "publish", "--url", url, "--project", P)
        assert set(r["first"]["reloads"]) == {"orders"} and r["measure_only"]["reloads"] == ["orders"], r  # 1.1 reloads every source
        assert r["measure_only"]["commit"].endswith(", a description") and "uncommitted" not in r, r
        assert all(0 < r[k]["answered_s"] <= r[k]["seconds"] for k in ("first", "measure_only")), r
        assert f"{P}_pub" not in projects() and not (abi.folder(P).parent / "_views" / P / "published.json").exists()
        assert [c["id"] for c in abi.call("history", project=P)["commits"]][3] == head, "a Viewer, the description, both back"
        assert not abi.call("changes", project=P)["files"] and abi.call("semantic_model", project=P, all=True)["measures"] == measures
        assert {u["name"] for u in abi.call("list_users")["users"]} == users
        with monkeypatch.context() as m:  # kill -9 of the worker, here a stand-in its pid file names
            m.setattr(perf, "D", tmp_path)
            (tmp_path / "run").mkdir()
            pid = tmp_path / "run/worker.pid"

            def started():
                stand.append(subprocess.Popen(["sleep", "60"]))
                pid.write_text(f"{stand[-1].pid}\n")

            def supervisor(gone):  # a new process a second after the old one died
                while gone.poll() is None:
                    time.sleep(0.1)
                time.sleep(1)
                started()
            for back in (False, True):  # 1.1 restarts nothing; a new process in the pid file serves again
                started()
                gone = stand[-1]
                if back:
                    threading.Thread(target=supervisor, args=(gone,), daemon=True).start()
                r = run(capsys, "kill", "--part", "worker", "--url", url, "--project", P, "--wait", "30" if back else "2")
                assert gone.wait(5) == -signal.SIGKILL and r["timeline"][0][1:] == [True, 200, False], r
                assert (1 <= r["serving_s"] < 30 and r["timeline"][-1][1:] == [True, 200, True]) if back else r["serving_s"] is None, r
    finally:
        s.shutdown()
        for x in stand:
            x.kill()
        drop([x for x in (P, f"{P}_pub") if x in projects()])
