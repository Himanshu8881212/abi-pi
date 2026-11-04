"""bench/perf.py on this machine's engine, no model: load and isolation at the smallest Brewline, pages through an app
started here (signed in as the owner, with and without row-level security), memory idle and with a stand-in for Pi.
Each leaves no project, account, rule, file or change behind."""
import json
import os
import re
import sys

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
    assert projects() == before and set(perf.FILES.iterdir()) == files


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
    s = app.serve(P, 0, open_browser=False, signin=True)
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
        r = run(capsys, "memory", "--url", url, "--project", P, "--chats", "1", "--app-pid", str(os.getpid()))
        assert r["started"]["pi"]["chats"] == 1 and r["per_chat_mb"]["started"] > 0 and "answered" not in r, r
        assert not abi.call("changes", project=P)["files"], "roles.json without the account is committed"
        assert {u["name"] for u in abi.call("list_users")["users"]} == users
        r = run(capsys, "memory", "--project", P, "--chats", "0", "--app-pid", str(os.getpid()))
        assert all(r["idle"][k]["n"] >= 1 and r["idle"][k]["mb"] > 0 for k in ("app", "engine", "worker", "postgres")), r
    finally:
        s.shutdown()
        drop([P])
