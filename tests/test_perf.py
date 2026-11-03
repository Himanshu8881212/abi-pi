"""bench/perf.py on this machine's engine, no model: load and isolation at the smallest Brewline, pages through an app
started here (signed in as the owner) and memory idle. Each leaves no project, account, file or change behind."""
import json
import os
import re
import sys
from pathlib import Path

import pytest
from conftest import drop, parquet

import abi
import app
from pi_driver import ROOT

sys.path.insert(0, str(ROOT / "bench"))
import perf  # noqa: E402

P = "pi_perf_test"


def owner_password():
    """The owner's password, read like scripts/creds.py does (never printed)."""
    acc = Path(os.environ.get("ABI_ACCOUNTS", ROOT / "data/accounts.txt"))
    line = next((x for x in acc.read_text().splitlines() if re.match(r"\s*himanshu\b", x)), None) if acc.exists() else None
    return line and line.split()[-1]


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


def test_pages_and_memory_through_an_app_here(capsys, monkeypatch, tmp_path):
    if not (pw := owner_password()):
        pytest.skip("no accounts file")
    monkeypatch.setenv("ABI_USER", "himanshu")
    monkeypatch.setenv("ABI_PASSWORD", pw)
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
        with pytest.raises(SystemExit, match="uncommitted changes"):
            perf.main(["pages", "--url", url, "--project", P, "--sessions", "2"])
        assert abi.call("history", project=P)["commits"][0]["id"] == head, "someone's uncommitted work is never committed"
        perf.sure("delete_measure", project=P, id="work_in_progress")
        r = run(capsys, "pages", "--url", url, "--project", P, "--sessions", "4", "--people", "2")
        for k in ("first", "after_commit"):
            assert r[k]["errors"] == r[k]["dropped"] == 0 and 0 < r[k]["p50"] <= r[k]["p95"] <= r[k]["p99"] <= r[k]["max"], r
        log = [c["message"].strip() for c in abi.call("history", project=P)["commits"][:2]]
        assert re.fullmatch(r"perf\.py \d{10}: probe and accounts removed", log[0]) and log[1].endswith(": page check"), log
        assert not abi.call("changes", project=P)["files"], "the probe's removal is committed too"
        assert abi.call("semantic_model", project=P, all=True)["measures"] == measures
        assert {u["name"] for u in abi.call("list_users")["users"]} == users, "the throwaway accounts are gone"
        r = run(capsys, "memory", "--chats", "0", "--app-pid", str(os.getpid()))
        assert all(r["idle"][k]["n"] >= 1 and r["idle"][k]["mb"] > 0 for k in ("app", "engine", "worker", "postgres")), r
    finally:
        s.shutdown()
        drop([P])
