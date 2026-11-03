#!/usr/bin/env python3
"""The agent's model settings an admin changes in the app (Govern › Settings): the providers (API type, base URL, API
key, models), the default model and its reasoning level. Kept in workspaces/_settings/ (the container's volume).

API keys are encrypted at rest (Fernet) with $ABI_PI_SECRET_KEY, or with a key file made there on first use (0600),
as n8n does with its encryption key. The page only ever sees "set, ends in 1234"; Pi reads a key at call time through
`settings.py key <provider>`; nothing logs one.

    settings.py public            the settings without keys (JSON)
    settings.py key <provider>    that provider's API key (for Pi's apiKey command; never shown to a person)
    settings.py engine            give ABI's engine the agent's model (its custom agents use it)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

APIS = {"openai-responses": "OpenAI Responses (OpenAI)", "openai-completions": "OpenAI-compatible (OpenRouter, Ollama, LM Studio, vLLM, proxies)",
        "anthropic-messages": "Anthropic", "google-generative-ai": "Google Gemini"}
LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
PID = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
MID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,120}$")


def home() -> Path:
    return Path(os.environ.get("ABI_PI_WORKSPACES", Path(__file__).resolve().parent.parent / "workspaces")) / "_settings"


def _fernet():
    from cryptography.fernet import Fernet  # noqa: PLC0415
    if k := os.environ.get("ABI_PI_SECRET_KEY"):
        return Fernet(k.encode())
    f = home() / "secret.key"
    if not f.exists():
        home().mkdir(parents=True, exist_ok=True)
        f.write_bytes(Fernet.generate_key())
        f.chmod(0o600)
    return Fernet(f.read_bytes())


def load() -> dict:
    f = home() / "settings.json"
    s = json.loads(f.read_text()) if f.exists() else {}
    return {"providers": s.get("providers", []), "model": s.get("model"), "thinking": s.get("thinking", "medium"),
            "fallbacks": s.get("fallbacks", []), "mail": s.get("mail")}


def note(key: str, value) -> None:
    """A setting the app shows but ABI keeps (the account mail server, never its password)."""
    f = home() / "settings.json"
    s = json.loads(f.read_text()) if f.exists() else {}
    s[key] = value
    _save(s)


def _save(s: dict) -> None:
    home().mkdir(parents=True, exist_ok=True)
    f = home() / "settings.json"
    f.write_text(json.dumps(s, indent=1) + "\n")
    f.chmod(0o600)


def public() -> dict:
    s = load()
    return {**s, "apis": APIS, "levels": LEVELS,
            "providers": [{k: v for k, v in p.items() if k != "key"} | {"key_set": bool(p.get("key")) or bool(_builtin(p["base_url"])),
                                                                     "key_builtin": not p.get("key") and bool(_builtin(p["base_url"])), "key_ends": p.get("ends", "")}
                          for p in s["providers"]]}


def safe_url(base: str) -> bool:
    """A provider is a model API: never a cloud metadata service, a link-local or an unspecified address (a test
    connection would send the key there). Local endpoints (Ollama, LM Studio) stay allowed."""
    import ipaddress  # noqa: PLC0415
    import socket  # noqa: PLC0415
    import urllib.parse  # noqa: PLC0415
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if not host or host in ("metadata.google.internal", "metadata", "instance-data") or host.endswith(".internal") and "metadata" in host:
        return False
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, None)}
        except OSError:
            return True  # not resolvable here (yet): the test connection will say so
    return not any(a.is_link_local or a.is_unspecified or a.is_multicast for a in addrs)


def save_provider(p: dict, key: str | None = None) -> dict:
    """Add or change a provider; a key given replaces the stored one, none keeps it."""
    pid, api, base = str(p.get("id", "")), p.get("api"), str(p.get("base_url", "")).rstrip("/")
    models = [m for m in (p.get("models") or []) if isinstance(m, str) and m.strip()]
    if not PID.match(pid) or api not in APIS or not re.match(r"^https?://[^\s]+$", base) or not models or not all(MID.match(m) for m in models):
        raise ValueError("A provider needs an ID (lowercase letters, digits, -), an API type, an http(s) base URL and at least one model ID.")
    if not safe_url(base):
        raise ValueError("That address is a metadata, link-local or unspecified address, not a model API.")
    s = load()
    old = next((x for x in s["providers"] if x["id"] == pid), {})
    rec = {"id": pid, "name": str(p.get("name") or pid)[:80], "api": api, "base_url": base, "models": models[:40],
           "key": old.get("key"), "ends": old.get("ends", "")}
    if key:
        rec["key"], rec["ends"] = _fernet().encrypt(key.encode()).decode(), key[-4:]
    s["providers"] = [x for x in s["providers"] if x["id"] != pid] + [rec]
    _save(s)
    return public()


def remove_provider(pid: str) -> dict:
    s = load()
    s["providers"] = [x for x in s["providers"] if x["id"] != pid]
    if (s.get("model") or "").split("/")[0] == pid:
        s["model"] = None
    s["fallbacks"] = [m for m in s["fallbacks"] if m.split("/")[0] != pid]
    _save(s)
    return public()


def set_default(model: str | None, thinking: str, fallbacks: list | None = None) -> dict:
    """The agent's model and reasoning, and the models it moves to, in order, when one fails (quota, outage)."""
    s = load()
    if thinking not in LEVELS:
        raise ValueError(f"Reasoning is one of {', '.join(LEVELS)}.")
    fallbacks = [] if fallbacks is None else fallbacks
    if not isinstance(fallbacks, list) or len(fallbacks) > 5:
        raise ValueError("At most 5 fallback models.")
    for m in [model, *fallbacks] if model else fallbacks:
        pid, _, mid = str(m).partition("/")
        if not any(p["id"] == pid and mid in p["models"] for p in s["providers"]):
            raise ValueError("Pick a model of a configured provider.")
    s["model"], s["thinking"], s["fallbacks"] = model or None, thinking, [m for m in dict.fromkeys(fallbacks) if m != model]
    _save(s)
    return public()


RETRY = re.compile(r"^(429|5\d\d|an error)$")  # quota, rate limit, outage or no status: worth another model; 401/400 are not


def next_model(failed: str, tried: list) -> str | None:
    """The fallback to try after a model answered `failed` (its HTTP status): the first one not tried in this turn."""
    if not RETRY.match(failed):
        return None
    return next((m for m in load()["fallbacks"] if m not in tried), None)


def _builtin(base: str) -> str:
    """The built-in connection's key, only for its own address (creds.py: the container's environment, else the proxy
    credentials file): a provider saved for that address without a key of its own uses it; any other address never
    sees it."""
    import creds  # noqa: PLC0415 (sibling script)
    try:
        own = creds.proxy("API base URL").rstrip("/")
        return creds.proxy("API key") if own and base.rstrip("/") == own else ""
    except (OSError, SystemExit):  # no built-in connection here
        return ""


def engine_sync() -> str:
    """ABI's engine runs the custom agents people build (and flows' agent steps) with its own LLM providers: give it the
    model this app's agent uses, so they answer too. The key goes in as an engine secret bound to that one host; the
    engine's confirmation is the admin's own choice of that address (Settings, or ABI_PI_LLM_BASE at deployment).
    OpenAI-compatible providers only (the engine speaks /chat/completions). Returns what it did, never a key."""
    import urllib.parse  # noqa: PLC0415
    import abi  # noqa: PLC0415 (sibling script)
    import creds  # noqa: PLC0415
    s = load()
    if s["model"]:
        pid, _, mid = s["model"].partition("/")
        p = next((x for x in s["providers"] if x["id"] == pid), None)
        if not p or p["api"] not in ("openai-completions", "openai-responses"):
            return "skipped: the default model's provider is not OpenAI-compatible"
        base, key = p["base_url"].rstrip("/"), key_of(pid)
    else:
        try:
            base, key = creds.proxy("API base URL").rstrip("/"), creds.proxy("API key")
        except (OSError, SystemExit):
            return "skipped: no model connected"
        mid = os.environ.get("ABI_PI_LLM_MODEL") or json.loads((Path(__file__).resolve().parent.parent / ".pi/settings.json").read_text())["defaultModel"]
    if not base:
        return "skipped: no model connected"
    args = {"id": "abi-pi", "base_url": base, "models": {"small": mid, "large": mid}}
    if key and key != "none":
        abi.call("set_secret", name="ABI_PI_LLM", value=key, allowed_hosts=[urllib.parse.urlsplit(base).hostname])
        args["api_key"] = "${secret:ABI_PI_LLM}"
    try:
        abi.call("set_llm_provider", **args)
    except abi.ActionError as e:
        if e.status != 409 or not e.body.get("confirm_token"):
            raise
        abi.call("set_llm_provider", confirm=e.body["confirm_token"], **args)
    return f"engine provider abi-pi: {base} model {mid}"


def key_of(pid: str) -> str:
    p = next((x for x in load()["providers"] if x["id"] == pid), None)
    if p and p.get("key"):
        return _fernet().decrypt(p["key"].encode()).decode()
    return _builtin(p["base_url"]) if p else ""


def models(p: dict, key: str | None) -> dict:
    """The model IDs a provider offers, for the form to pick from: with the typed key, else the stored one (same ID),
    else the built-in connection's for its own address. The key is used here and never returned."""
    base, pid = str(p.get("base_url", "")).rstrip("/"), str(p.get("id", ""))
    key = key or (key_of(pid) if any(x["id"] == pid and x["base_url"] == base for x in load()["providers"]) else "") or _builtin(base)
    out = _list(str(p.get("api") or "openai-completions"), base, key)
    return {"ok": out["ok"], "models": out.get("models", []), "message": out["message"] if not out["ok"] else f"{len(out['models'])} models available."}


def test(pid: str) -> dict:
    """Can the app reach the provider with its key? Lists its models (the key never leaves this process)."""
    p = next((x for x in load()["providers"] if x["id"] == pid), None)
    if not p:
        return {"ok": False, "message": "No such provider."}
    return _list(p["api"], p["base_url"], key_of(pid))


def _list(api: str, base: str, key: str) -> dict:
    if not re.match(r"^https?://[^\s]+$", base) or not safe_url(base):  # checked at every request: a name can resolve elsewhere later
        return {"ok": False, "message": "That address is not allowed for a model API."}
    if api == "anthropic-messages":
        req = urllib.request.Request(f"{base}/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif api == "google-generative-ai":
        req = urllib.request.Request(f"{base}/models", headers={"x-goog-api-key": key})
    else:
        req = urllib.request.Request(f"{base}/models", headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read() or b"{}")
        ids = [m.get("id") or m.get("name") for m in (d.get("data") or d.get("models") or []) if isinstance(m, dict)]
        return {"ok": True, "message": f"Connected: {len(ids)} models available.", "models": [i for i in ids if i][:200]}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"The provider answered {e.code}: {'check the API key' if e.code in (401, 403) else 'check the base URL'}."}
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"Can't reach {base}: {type(e).__name__}."}


def demo(tmp: Path) -> None:
    os.environ["ABI_PI_WORKSPACES"] = str(tmp)
    save_provider({"id": "openrouter", "name": "OpenRouter", "api": "openai-completions", "base_url": "https://openrouter.ai/api/v1/",
                   "models": ["anthropic/claude-x"]}, "sk-test-123456789")
    raw = (home() / "settings.json").read_text()
    assert "sk-test-123456789" not in raw and "6789" in json.dumps(public()) and "sk-test" not in json.dumps(public())
    assert key_of("openrouter") == "sk-test-123456789"
    save_provider({"id": "openrouter", "api": "openai-completions", "base_url": "https://openrouter.ai/api/v1", "models": ["m2"]})
    assert key_of("openrouter") == "sk-test-123456789", "no key given keeps the stored one"
    set_default("openrouter/m2", "high")
    assert load()["model"] == "openrouter/m2" and load()["thinking"] == "high"
    save_provider({"id": "openrouter", "api": "openai-completions", "base_url": "https://openrouter.ai/api/v1", "models": ["m2", "m3"]})
    set_default("openrouter/m2", "high", ["openrouter/m3", "openrouter/m2"])
    assert load()["fallbacks"] == ["openrouter/m3"], "the default is never its own fallback"
    assert next_model("429", ["openrouter/m2"]) == "openrouter/m3" and next_model("503", ["openrouter/m3"]) is None
    assert next_model("401", []) is None, "a wrong key is not fixed by another model"
    for bad in ({"id": "X!", "api": "openai-completions", "base_url": "https://x", "models": ["m"]},
                {"id": "x", "api": "nope", "base_url": "https://x", "models": ["m"]}, {"id": "x", "api": "openai-completions", "base_url": "ftp://x", "models": ["m"]}):
        try:
            save_provider(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    remove_provider("openrouter")
    assert load()["model"] is None and load()["fallbacks"] == [] and (home() / "settings.json").stat().st_mode & 0o077 == 0
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:2] == ["public"]:
        print(json.dumps(public()))
    elif sys.argv[1:2] == ["engine"]:
        print(engine_sync())
    elif sys.argv[1:2] == ["key"] and len(sys.argv) == 3:
        print(key_of(sys.argv[2]))
    else:
        raise SystemExit(__doc__)
