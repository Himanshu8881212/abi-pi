"""Python SDK over the REST action API. PRD PC-14, SE-9, §9 "Python SDK over the REST action API".

Stdlib only, so the file can be copied into any script. Every call is `POST /api/actions/<name>`, so roles,
confirmations and the audit log hold exactly as for the UI and the CLI (principle 1). Nothing here logs.

    c = Client.login("http://localhost:8000", "etl-bot", password)
    c.semantic_query(project="shop", measures=["sales"], dimensions=["products.category"])
    try:
        c.delete_report(project="shop", report="old")
    except ConfirmationRequired as e:  # MA-3: a person confirms, then the call is repeated with the token
        c.delete_report(project="shop", report="old", confirm=e.confirm_token)
"""

import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request


class ActionError(Exception):
    """The app refused or failed the call: `status` is the HTTP status, `body` the error detail (always a dict)."""

    def __init__(self, status: int, body: dict):
        super().__init__(f"{status}: {body.get('message')}")
        self.status, self.body = status, body


class ConfirmationRequired(ActionError):
    """409: the action needs a person's confirmation; repeat the call with `confirm=confirm_token`."""

    def __init__(self, status: int, body: dict):
        super().__init__(status, body)
        self.message, self.confirm_token, self.preview = body["message"], body["confirm_token"], body.get("preview")


class Client:
    def __init__(self, url: str, token: str):
        self.url, self._token = url.rstrip("/"), token

    def __repr__(self):  # never show the token
        return f"Client({self.url!r})"

    @classmethod
    def login(cls, url: str, name: str, password: str, code: str | None = None) -> "Client":
        """Signs in; `code` is a TOTP or recovery code when the account has two-step sign-in (SC-10)."""
        body = {"name": name, "password": password} | ({"code": code} if code else {})
        return cls(url, _request(f"{url.rstrip('/')}/api/login", body)["token"])

    def call(self, action: str, /, confirm: str | None = None, **args) -> dict:
        """Run action `action` with `args` as its input."""
        return _request(f"{self.url}/api/actions/{action}", args, self._token, confirm)

    def actions(self) -> list[dict]:
        """Every action: name, description, input JSON Schema, role, destructive."""
        return _request(f"{self.url}/api/actions", None, self._token)

    def export(self, project: str, query: dict, path: str, format: str = "arrow") -> None:
        """SE-9: stream a semantic query's full result to the file `path` as an Arrow IPC stream or Parquet."""
        url = f"{self.url}/api/projects/{urllib.parse.quote(project)}/export?format={urllib.parse.quote(format)}"
        with _open(url, query, self._token) as r, open(path, "wb") as f:
            shutil.copyfileobj(r, f)

    def run_flow(self, project: str, flow: str, key: str | None = None, wait: bool = False, timeout: float = 600) -> dict:
        """FL-7: start a flow run (`key`: a retried call starts no second run). `wait`: poll every 2 s until the run
        ends and return it (status ok, failed, stopped or rejected); TimeoutError after `timeout` seconds."""
        r = self.call("run_flow", project=project, flow=flow, **({"key": key} if key else {}))
        deadline = time.monotonic() + timeout
        while wait:
            [run] = self.call("flow_runs", project=project, run=r["run"])["runs"]
            if run["status"] in ("ok", "failed", "stopped", "rejected"):
                return run
            if time.monotonic() > deadline:
                raise TimeoutError(f"Run {r['run']} is still {run['status']} after {timeout} s.")
            time.sleep(2)
        return r

    def __getattr__(self, action: str):
        if action.startswith("_"):
            raise AttributeError(action)
        return lambda confirm=None, **args: self.call(action, confirm, **args)


def _request(url: str, body: dict | None, token: str | None = None, confirm: str | None = None):
    with _open(url, body, token, confirm) as r:
        return json.load(r)


def _open(url: str, body: dict | None, token: str | None = None, confirm: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if confirm:
        headers["X-Abi-Confirm"] = confirm
    data = None if body is None else json.dumps(body).encode()
    try:
        return urllib.request.urlopen(urllib.request.Request(url, data, headers))
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            detail = json.loads(text)["detail"]
        except (ValueError, KeyError, TypeError):
            detail = text
        body = detail if isinstance(detail, dict) else {"message": detail if isinstance(detail, str) else json.dumps(detail)}
        raise (ConfirmationRequired if e.code == 409 and "confirm_token" in body else ActionError)(e.code, body) from None
