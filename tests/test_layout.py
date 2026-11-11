"""Workspace = empty folder; project = ABI project + full skeleton."""
import json

from test_pages import objs  # noqa: F401  (the Brewline project with object types, signed in to an app)

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


def test_project_ids_follow_abis_pattern():
    import pytest
    with pytest.raises(SystemExit):
        abi.new_project("ws", "bad-id")


def test_the_object_type_graph_lays_out_clear(objs, tmp_path):
    """1.2.6 M7 (plan item 26): the object-type graph on the model canvas at desktop width, as first laid out: every
    type's box inside the canvas and none over another, every connection's label clear of every box and of the other
    labels. Its screenshot is taken beside (tmp_path)."""
    from test_pages import drive
    shot = tmp_path / "object-graph.png"
    rects = "s => [...document.querySelectorAll(s)].map(e => (r => [r.left, r.top, r.right, r.bottom])(e.getBoundingClientRect()))"
    out = drive(objs["owner"], tmp_path, [
        ["show", "objects"], ["wait", "main .canvas .tbl"], ["until", "document.querySelectorAll('main .canvas rect.lblbg').length === 3"],
        ["get", "boxes", f"({rects})('main .canvas .tbl')"], ["get", "labels", f"({rects})('main .canvas rect.lblbg')"],
        ["get", "canvas", f"({rects})('main .canvas .stage')[0]"], ["shot", str(shot)]])
    hit = lambda a, b: a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]  # noqa: E731
    boxes, labels, (x0, y0, x1, y1) = out["boxes"], out["labels"], out["canvas"]
    assert len(boxes) == 3 and len(labels) == 3 and shot.stat().st_size > 10_000
    assert all(x0 <= b[0] and y0 <= b[1] and b[2] <= x1 + 1 and b[3] <= y1 + 1 for b in boxes + labels), f"inside the canvas: {out}"
    assert not [(i, j) for i, a in enumerate(boxes) for j, b in enumerate(boxes) if i < j and hit(a, b)], f"no box over another: {boxes}"
    assert not [(i, j) for i, a in enumerate(labels) for j, b in enumerate(boxes) if hit(a, b)], f"no label over a box: {labels}"
    assert not [(i, j) for i, a in enumerate(labels) for j, b in enumerate(labels) if i < j and hit(a, b)], f"no label over another: {labels}"
