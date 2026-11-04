"""1.2 M3.5 on this machine's engine, no LLM: a deleted project's uploads go with its lake, 30 days after the deletion,
never one that a live project's files name, now or in their Git history (another project's source, a published copy's),
or that a queued load reads. The worker's daily purge runs here with the clock moved on, on a copy of this test's
uploads, so no other upload on this machine is touched."""
import json
import os
import shutil
import time
from pathlib import Path

from conftest import drop
from test_doors import projects, source, sure
from test_engine import engine

import abi
import app

S, Q, P, O, BROKEN = "pi_keep_s", "pi_keep_q", "pi_keep_p", "pi_keep_o", "pi_keep_broken"
ALL = (S, Q, P, f"{P}_pub", O, BROKEN)  # a broken project left behind would stop the purge: it may read anything
UPLOADS = Path(os.environ.get("ABI_PI_DATA") or abi.ROOT / "data") / "files/uploads"  # scripts/engine.sh's


def purge(files: Path, days: int, queued: str | None = None) -> set[str]:
    """The worker's upload purge `days` from now on `files` (with the lists this test's deletions wrote, but O's:
    deleted before 1.2 wrote any), while a load of upload `queued` waits in load_runs: the uploads left."""
    for f in UPLOADS.glob("_deleted_*_pi_keep_*.json"):
        if not f.name.endswith(f"_{O}.json"):
            shutil.copy2(f, files / "uploads")
    r = engine(f"""
import json, os
os.environ["ABI_FILES_PATH"] = {str(files)!r}
from abi import db, ingest
with db.connect() as c:
    if {queued!r}:
        c.execute("INSERT INTO load_runs (id, project, table_name, action, payload, via, user_id) VALUES"
                  " (gen_random_uuid(), %s, 'orders', 'add_data', %s::jsonb, 'rest', (SELECT min(id) FROM users))",
                  ["{P}", json.dumps({{"location": "uploads/{queued}/orders.csv"}})])
    ingest.purge_uploads(c, now={time.time() + days * 86400})
    c.rollback()  # the queued load was never there
""")
    assert r.returncode == 0, r.stderr[-2000:]
    return {d.name for d in (files / "uploads").iterdir() if d.is_dir()}


def test_a_deleted_projects_uploads_go_with_its_lake_never_one_a_live_project_reads(tmp_path):
    """S's upload, which no one else loads, is kept as long as S's lake (30 days) and then goes; so does O's, deleted
    before 1.2 listed what a deletion leaves. Q's upload stays after Q is deleted while P loads it; P's first file for a
    table stays in P's history after a second replaces it and goes with P (unless a queued load reads it); P's
    published copy keeps the files its sources load after P is deleted, until it is deleted too."""
    drop([p for p in ALL if p in projects()])  # an interrupted run's
    files, f = tmp_path / "files", tmp_path / "orders.csv"
    f.write_text("order_id,amount\n1,10\n2,20\n")
    try:
        for p in (S, Q, O):
            abi.call("create_project", project=p, name=p)
        abi.new_project("demo", P)  # in a workspace, as Publish needs; it commits each change (the others don't)
        up = {}
        for p in (S, Q, O, P):
            abi.upload(p, "orders", f, abi.CSV_SAFE)
            up[p] = source(p, "orders")["location"].split("/")[1]
        abi.call("add_data", project=P, table="theirs", location=source(Q, "orders")["location"], csv=abi.CSV_SAFE)
        abi.upload(P, "orders", f, abi.CSV_SAFE)  # a new file for the table: the first is in P's history only
        up["P2"] = source(P, "orders")["location"].split("/")[1]
        pub, bundle, _ = app.publish_bundle(P)
        sure("import_project", project=pub, bundle=bundle)  # Publish
        for u in up.values():
            shutil.copytree(UPLOADS / u, files / "uploads" / u)
        before = set(UPLOADS.glob("_deleted_*.json"))
        listed = lambda: {f.name.split("_", 3)[3][:-5]: json.loads(f.read_text())  # noqa: E731
                          for f in set(UPLOADS.glob("_deleted_*.json")) - before}
        drop([S, Q, O])
        assert listed() == {S: [up[S]], Q: [up[Q]], O: [up[O]]}
        assert purge(files, 29) == set(up.values()), "kept while the deleted projects' lakes are"
        assert purge(files, 31) == {up[Q], up[P], up["P2"]}, "S's and O's go with their lakes; P loads Q's, its history its first"

        drop([P])
        assert listed()[P] == sorted([up[P], up["P2"], up[Q]])
        keep = {up[Q], up["P2"]}  # what the published copy's sources load
        if engine("from abi import db; print(db.connect().execute(\"SELECT to_regclass('load_runs') AS t\").fetchone()['t'])").stdout.strip() != "None":
            assert purge(files, 31, queued=up[P]) == keep | {up[P]}, "a queued load reads P's first file"  # M3.2 in
        assert purge(files, 31) == keep, "P's first file goes with its lake; the published copy loads the others"
        drop([pub])
        assert purge(files, 29) == keep, "kept while the published copy's lake is"
        assert purge(files, 31) == set()
    finally:
        drop([p for p in ALL if p in projects()])


def test_a_project_whose_history_cant_be_read_still_deletes():
    """Deleting is the way out of a broken project: with its Git objects gone it still deletes, without a list, so
    its lake holds back every upload no list names (as a deletion before 1.2 does) instead of naming none."""
    t = BROKEN
    drop([p for p in (t,) if p in projects()])  # an interrupted run's
    abi.call("create_project", project=t, name=t)
    for f in (abi.ABI_PROJECTS / t / ".git/objects").glob("??/*"):
        f.unlink()
    before = set(UPLOADS.glob(f"_deleted_*_{t}.json"))
    drop([t])
    assert t not in projects() and set(UPLOADS.glob(f"_deleted_*_{t}.json")) == before
