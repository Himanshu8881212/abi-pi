import os
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tests")]


def parquet(*csvs) -> list[str]:
    """Tests write their small tables as CSV and load a Parquet copy (DuckDB converts)."""
    import duckdb
    out = []
    for f in map(Path, csvs):
        duckdb.sql(f"COPY (SELECT * FROM read_csv('{f}')) TO '{f.with_suffix('.parquet')}' (FORMAT parquet)")
        out.append(str(f.with_suffix(".parquet")))
    return out


TEST_PROJECT = re.compile(r"(pi_app_(test|other)|pi_test_layout|pi_date_test|pi_model_test|pi_pages_test|yt|(sec|s1)_\d{8})(_pub)?")


def drop(projects) -> None:
    """Throwaway projects (a test run's, the bench's) go through ABI's delete_project, confirmed here because the run
    made them, with their copies under workspaces/."""
    import abi
    for p in projects:
        copies = [f for ws in abi.WORKSPACES.iterdir() for f in [ws / p, *(ws / s / p for s in ("_views", "_app", "_team", "_vault"))]]
        try:
            abi.call("delete_project", project=p)
        except abi.ActionError as e:
            abi.call("delete_project", confirm=e.body["confirm_token"], project=p)
        for f in copies:
            shutil.rmtree(f, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _remove_test_projects():
    """The projects this run created for its tests go after it (24 had piled up)."""
    import abi
    try:
        before = {p["project"] for p in abi.call("list_projects")["projects"]}
    except Exception:  # noqa: BLE001 (no engine: nothing to clean)
        yield
        return
    drop(p for p in before if TEST_PROJECT.fullmatch(p))  # one an interrupted run left would be reused, stale (release run 31 Oct)
    before = {p["project"] for p in abi.call("list_projects")["projects"]}
    yield
    drop(p for p in {p["project"] for p in abi.call("list_projects")["projects"]} - before if TEST_PROJECT.fullmatch(p))


def pytest_collection_modifyitems(config, items):
    """Live tests drive GPT-6.1 Sol through Pi (minutes, tokens): opt in with ABI_PI_LIVE=1."""
    if os.environ.get("ABI_PI_LIVE") != "1":
        skip = pytest.mark.skip(reason="live LLM test: set ABI_PI_LIVE=1")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip)
