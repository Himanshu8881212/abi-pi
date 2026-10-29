import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import settings  # noqa: E402


def test_provider_keys_are_encrypted_masked_and_validated(tmp_path, monkeypatch):
    monkeypatch.delenv("ABI_PI_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_PI_WORKSPACES", str(tmp_path))
    settings.demo(tmp_path)


def test_a_provider_url_cannot_point_at_metadata_or_nowhere(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("ABI_PI_WORKSPACES", str(tmp_path))
    for url in ("http://169.254.169.254/latest", "http://0.0.0.0:80", "http://[fe80::1]/v1", "http://metadata.google.internal/"):
        with pytest.raises(ValueError):
            settings.save_provider({"id": "x", "api": "openai-completions", "base_url": url, "models": ["m"]})
    settings.save_provider({"id": "ollama", "api": "openai-completions", "base_url": "http://host.docker.internal:11434/v1", "models": ["m"]})
