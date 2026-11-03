"""Drive the harness like a person at the terminal: `pi --mode rpc` in this folder, one prompt, confirmations answered
by `confirm` (and recorded), until the agent settles. Used by the live tests.

    ABI_PI_LIVE_MODEL=proxy/claude-sonnet-5-5      run them on another model (e.g. a cheap one in CI)
    ABI_PI_LIVE_FALLBACK=proxy/a,proxy/b           when the model fails (quota, outage), ask these in order, as the app does
"""
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from settings import RETRY  # noqa: E402 (the app's own rule: which failures another model may fix)


@dataclass
class Run:
    prompt: str
    answer: str = ""
    tools: list = field(default_factory=list)  # (name, args, is_error, ms, result text)
    confirms: list = field(default_factory=list)  # (title, message)
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0, "cacheRead": 0, "turns": 0})
    seconds: float = 0.0
    events: int = 0
    model: str = ""  # provider/model of the last reply (a fallback's when the first model failed)

    def called(self, name: str) -> list:
        return [t for t in self.tools if t[0].endswith(name)]


def ask(prompt: str, confirm: bool = False, timeout: float = 1200, session: str | None = None, extra: list | None = None,
        env: dict | None = None) -> Run:
    """One prompt; `session` keeps context across calls (a session file path), else ephemeral."""
    model = os.environ.get("ABI_PI_LIVE_MODEL")
    fallbacks = [m for m in os.environ.get("ABI_PI_LIVE_FALLBACK", "").split(",") if m]
    args = ["pi", "--mode", "rpc", *(["--model", model] if model else []), *(extra or [])] + (["--session", session] if session else ["--no-session"])
    p = subprocess.Popen(args, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                         env={**os.environ, **(env or {})})
    run, t0, started, failed = Run(prompt), time.time(), {}, ""
    p.stdin.write(json.dumps({"id": "p1", "type": "prompt", "message": prompt}) + "\n")
    p.stdin.flush()
    try:
        for line in p.stdout:
            run.events += 1
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "extension_ui_request" and e.get("method") in ("confirm", "select", "input", "editor"):
                run.confirms.append((e.get("title"), e.get("message")))
                reply = {"type": "extension_ui_response", "id": e["id"]}
                reply |= {"confirmed": confirm} if e["method"] == "confirm" else {"cancelled": True}
                p.stdin.write(json.dumps(reply) + "\n")
                p.stdin.flush()
            elif t == "tool_execution_start":
                started[e["toolCallId"]] = (e["toolName"], e.get("args"))
            elif t == "tool_execution_end":
                name, a = started.get(e["toolCallId"], (e.get("toolName"), None))
                res = "".join(c.get("text", "") for c in (e.get("result") or {}).get("content", []) if isinstance(c, dict))
                run.tools.append((name, a, e.get("isError"), e.get("durationMs"), res))
            elif t == "message_end" and (m := e.get("message", {})).get("role") == "assistant":
                u = m.get("usage") or {}
                for k in ("input", "output", "cacheRead"):
                    run.usage[k] += u.get(k, 0) or 0
                run.usage["turns"] += 1
                run.model = f"{m.get('provider')}/{m.get('model')}"
                txt = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
                if txt.strip():
                    run.answer = txt
                if m.get("stopReason") == "error":
                    failed = (re.search(r"\((\d{3})\)", m.get("errorMessage") or "") or re.search("()", "")).group(1) or "an error"
            elif t == "agent_settled":
                if not run.answer and failed and RETRY.match(failed) and fallbacks:
                    pid, _, mid = fallbacks.pop(0).partition("/")
                    for cmd in ({"type": "set_model", "provider": pid, "modelId": mid}, {"type": "prompt", "message": prompt}):
                        p.stdin.write(json.dumps(cmd) + "\n")
                    p.stdin.flush()
                    failed = ""
                    continue
                break
            if time.time() - t0 > timeout:
                break
    finally:
        p.kill()
    run.seconds = round(time.time() - t0, 1)
    return run
