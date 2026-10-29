"""The team's rules (scripts/team.py) without ABI: plans, the delegation guard, handoffs and post-checks."""
import pytest

import team


@pytest.fixture(autouse=True)
def tmp_team(tmp_path, monkeypatch):
    monkeypatch.setattr(team, "tdir", lambda project: tmp_path)
    monkeypatch.setattr(team, "_rows", lambda action, **kw: ROWS.get(action, {}))
    ROWS.clear()


ROWS: dict = {}
PLAN = {"goal": "CMS demo", "tasks": [
    {"id": "bronze", "agent": "data-engineer", "area": "sources", "task": "load the bucket"},
    {"id": "model", "agent": "analytics-engineer", "area": "model", "task": "model on gold", "depends_on": ["bronze"]},
    {"id": "reports", "agent": "analyst", "area": "reports", "task": "three reports", "depends_on": ["model"]},
    {"id": "flows", "agent": "automation-engineer", "area": "flows", "task": "two flows", "depends_on": ["model"]},
]}


def test_plan_rules():
    assert team.validate(PLAN) == []
    wrong = {"tasks": [{"id": "a", "agent": "analyst", "area": "flows", "task": "x"}]}
    assert "belongs to automation-engineer" in team.validate(wrong)[0]
    order = {"tasks": [PLAN["tasks"][0], {**PLAN["tasks"][1], "depends_on": []}]}
    assert "must come after bronze" in team.validate(order)[0]
    assert "unknown agent" in team.validate({"tasks": [{"id": "a", "agent": "boss", "area": "x", "task": "y"}]})[0]


def test_guard_enforces_plan_order_area_and_attempts():
    assert not team.guard("p", "data-engineer", "Plan task bronze: go")["ok"], "no plan yet"
    team.save_plan("p", {**PLAN, "tasks": [dict(t) for t in PLAN["tasks"]]})
    assert "Start the task with" in team.guard("p", "data-engineer", "load it")["reason"]
    assert "not in the plan" in team.guard("p", "data-engineer", "Plan task nope: x")["reason"]
    assert "belongs to data-engineer" in team.guard("p", "analyst", "Plan task bronze: x")["reason"]
    assert "waits for bronze" in team.guard("p", "analytics-engineer", "Plan task model: x")["reason"]
    assert team.guard("p", "data-engineer", "Plan task bronze: load")["ok"]
    assert "already running" in team.guard("p", "data-engineer", "Plan task bronze: again")["reason"]


def test_handoff_passes_unlocks_dependents_and_failures_go_back():
    team.save_plan("p", {**PLAN, "tasks": [dict(t) for t in PLAN["tasks"]]})
    team.guard("p", "data-engineer", "Plan task bronze: load")
    assert not team.handoff("p", "data-engineer", {"task_id": "bronze"})["accepted"], "summary and status are required"
    ROWS["data_quality"] = {"columns": ["table", "rows"], "rows": [["bronze_basic", 1000]]}
    ROWS["semantic_model"] = {"columns": {}}
    bad = team.handoff("p", "data-engineer", {"task_id": "bronze", "status": "done", "summary": "loaded",
                                              "done": [{"kind": "table", "id": "bronze_basic"}, {"kind": "table", "id": "bronze_missing"}]})
    assert bad["result"] == "failed" and "bronze_missing" in bad["checks"][0]["problems"][0]
    assert team.guard("p", "data-engineer", "Plan task bronze: retry")["ok"], "a failed task can be retried"
    ok = team.handoff("p", "data-engineer", {"task_id": "bronze", "status": "done", "summary": "loaded", "decisions": ["one source per folder"],
                                             "done": [{"kind": "table", "id": "bronze_basic"}],
                                             "pending_confirmations": [{"action": "add_data", "args": {"table": "x"}, "why": "first send to a new host"}]})
    assert ok["result"] == "passed"
    assert team.guard("p", "analytics-engineer", "Plan task model: build")["ok"], "the dependent may start now"
    assert team.pending("p")[0]["action"] == "add_data"
    b = team.brief("p", "analytics-engineer", "model")
    assert "one source per folder" in b and "Your task model" in b, "the next agent is briefed with the handoff"


def test_flows_must_be_saved_off_and_attempts_are_limited():
    team.save_plan("p", {"tasks": [{"id": "f", "agent": "automation-engineer", "area": "flows", "task": "digest"}]})
    ROWS["list_flows"] = {"flows": [{"id": "weekly", "enabled": True}]}
    for _ in range(team.MAX_ATTEMPTS):
        assert team.guard("p", "automation-engineer", "Plan task f: digest")["ok"]
        r = team.handoff("p", "automation-engineer", {"task_id": "f", "status": "done", "summary": "made", "done": [{"kind": "flow", "id": "weekly"}]})
        assert r["result"] == "failed" and "enabled" in r["checks"][0]["problems"][0]
    assert "ask the person" in team.guard("p", "automation-engineer", "Plan task f: again")["reason"]
