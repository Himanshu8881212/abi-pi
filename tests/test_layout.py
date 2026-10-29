"""Workspace = empty folder; project = ABI project + full skeleton, every JSON file valid against ABI's schemas."""
import json

import abi

LAYOUT = {"sources", "tables", "model", "reports", "ml", "flows", "agents", "tools", "security", "environments", "docs"}


def test_new_workspace_is_an_empty_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(abi, "WORKSPACES", tmp_path)
    abi.new_workspace("fresh")
    assert (tmp_path / "fresh").is_dir() and not any((tmp_path / "fresh").iterdir())


def test_new_project_has_the_full_skeleton(tmp_path, monkeypatch):
    monkeypatch.setattr(abi, "WORKSPACES", tmp_path)
    out = abi.new_project("ws", "pi_test_layout", "Pi layout test")
    dest = tmp_path / "ws" / "pi_test_layout"
    assert out["folder"] == str(dest) and (dest / ".git").is_dir()
    assert {p.name for p in dest.iterdir() if p.is_dir() and p.name not in (".git",)} == LAYOUT
    for d in LAYOUT:  # empty folders keep a .gitkeep; filled ones hold ABI's files
        assert any((dest / d).iterdir())
    pj = json.loads((dest / "project.json").read_text())
    assert pj["schema_version"] == 1 and pj["settings"]["auto_commit"] is True
    assert abi.validate_files(dest) == []
    assert (tmp_path / "ws" / "_views" / "pi_test_layout" / "index.html").exists()


def test_project_ids_follow_abis_pattern():
    import pytest
    with pytest.raises(SystemExit):
        abi.new_project("ws", "bad-id")
