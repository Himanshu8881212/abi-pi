import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import transfer  # noqa: E402


def test_zip_round_trip_and_names_stay_in_their_folder():
    transfer.demo()


def test_a_zip_bomb_is_refused_before_it_is_read():
    import io
    import zipfile
    import pytest
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", '{"format": "abi-pi-export/1", "needs": {}}')
        z.writestr("abi/docs/big.md", "0" * (transfer.MAX_UNPACKED + 1))
    with pytest.raises(ValueError, match="too large"):
        transfer.read_zip(buf.getvalue())


def test_pack_file_names_stay_in_the_pack():
    import pack
    assert pack.safe_id("overview") and pack.safe_id("rev_2026-q1")
    assert not pack.safe_id("../../x") and not pack.safe_id("a/b") and not pack.safe_id("")


def test_an_import_never_brings_steps_waiting_for_a_yes(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "places", lambda project: {"app": tmp_path / "app", "team": tmp_path / "team", "vault": tmp_path / "v", "abi": tmp_path})
    z = {"app": {}, "vault": {}, "team": {"plan.json": b"{}", "pending.json": b'[{"id": "p1", "action": "delete_source"}]'}}
    transfer.extras("x", z)
    assert (tmp_path / "team" / "plan.json").exists() and not (tmp_path / "team" / "pending.json").exists()
