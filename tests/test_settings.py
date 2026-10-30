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


def test_models_are_fetched_from_the_provider_with_a_key_that_never_comes_back(tmp_path, monkeypatch):
    """Fetch models: the typed key, else the stored one, else the built-in connection's for its own address; the list
    of model IDs comes back, never the key. A metadata address is refused before any request."""
    import http.server
    import threading
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            body = b'{"data": [{"id": "gpt-x"}, {"id": "gpt-y"}]}'
            self.send_response(200 if self.headers.get("Authorization") == "Bearer k-123456789" else 401)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/v1"
    monkeypatch.setenv("ABI_PI_WORKSPACES", str(tmp_path))
    monkeypatch.setenv("ABI_PI_LLM_BASE", base)  # creds.proxy reads the environment first, as in the container
    monkeypatch.setenv("ABI_PI_LLM_KEY", "k-123456789")
    p = {"id": "proxy", "api": "openai-completions", "base_url": base}
    out = settings.models(p, None)  # the built-in connection's address: its key
    assert out == {"ok": True, "models": ["gpt-x", "gpt-y"], "message": "2 models available."} and "k-123456789" not in str(out)
    assert settings.models({**p, "base_url": base.replace("/v1", "/other")}, None)["ok"] is False  # another address: no key
    assert settings.models(p, "wrong-key")["ok"] is False
    assert settings.models({**p, "base_url": "http://169.254.169.254/v1"}, "k")["ok"] is False
    settings.save_provider({**p, "models": ["gpt-x"]})
    assert settings.test("proxy")["ok"] is True  # saved without a key: the built-in one, for its own address
    pub = next(x for x in settings.public()["providers"] if x["id"] == "proxy")
    assert pub["key_set"] and pub["key_builtin"] and "key" not in pub  # Pi is given `settings.py key proxy`, not "none"
    srv.shutdown()
