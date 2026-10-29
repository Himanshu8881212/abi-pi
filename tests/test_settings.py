import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import settings  # noqa: E402


def test_provider_keys_are_encrypted_masked_and_validated(tmp_path, monkeypatch):
    monkeypatch.delenv("ABI_PI_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_PI_WORKSPACES", str(tmp_path))
    settings.demo(tmp_path)
