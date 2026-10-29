"""H-04 operations: readiness, Prometheus text metrics, JSON logs with secret scrubbing (SC-4, SC-4-LOG)."""

import hmac
import json
import logging
import logging.config
import os
import re
import threading
import time
from pathlib import Path

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from abi import db

router = APIRouter()
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Histogram:
    """Cumulative-bucket histogram per label tuple, in this process (the app; the worker serves no HTTP, its queue
    and LLM figures come from Postgres at scrape time)."""

    def __init__(self, name: str, doc: str, labels: tuple[str, ...]):
        self.name, self.doc, self.labels, self.data, self.lock = name, doc, labels, {}, threading.Lock()

    def observe(self, seconds: float, *values: str) -> None:
        with self.lock:
            row = self.data.setdefault(values, [0] * len(BUCKETS) + [0, 0.0])  # buckets..., count, sum
            for i, b in enumerate(BUCKETS):
                row[i] += seconds <= b
            row[-2] += 1
            row[-1] += seconds

    def text(self) -> list[str]:
        out = [f"# HELP {self.name} {self.doc}", f"# TYPE {self.name} histogram"]
        with self.lock:
            items = sorted((k, list(v)) for k, v in self.data.items())
        for values, row in items:
            ls = "".join(f'{k}="{_esc(v)}",' for k, v in zip(self.labels, values))
            out += [f'{self.name}_bucket{{{ls}le="{b}"}} {c}' for b, c in zip(BUCKETS, row)]
            out += [f'{self.name}_bucket{{{ls}le="+Inf"}} {row[-2]}', f"{self.name}_sum{{{ls[:-1]}}} {row[-1]}",
                    f"{self.name}_count{{{ls[:-1]}}} {row[-2]}"]
        return out


HTTP = Histogram("abi_http_request_duration_seconds", "HTTP request time by route template and status.",
                 ("method", "route", "status"))
QUERY = Histogram("abi_query_duration_seconds", "Semantic engine read time (semantic.execute).", ("cached", "outcome"))


class Timed:
    """ASGI middleware feeding HTTP. The route label is the matched template (never the raw path: no IDs or
    project names, bounded label set); anything unmatched (UI files, 404s) is `other`. 500 when no response started."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start, status = time.perf_counter(), [500]

        async def sent(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, sent)
        finally:
            route = getattr(scope.get("route"), "path", "other")
            method = scope["method"] if scope["method"] in METHODS else "other"  # h11 takes any token: bounded labels
            HTTP.observe(time.perf_counter() - start, method, route, str(status[0]))


METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _db_metrics() -> list[str]:
    """Queue depth, live workers and LLM tokens across app and worker, read from Postgres per scrape."""
    out = []
    with db.connect() as conn:
        if conn.execute("SELECT to_regclass('procrastinate_jobs') IS NOT NULL AS ok").fetchone()["ok"]:
            out += ["# HELP abi_jobs Procrastinate jobs by queue and status (todo/doing = queue depth).",
                    "# TYPE abi_jobs gauge"]
            out += [f'abi_jobs{{queue="{_esc(r["queue_name"])}",status="{r["status"]}"}} {r["n"]}' for r in conn.execute(
                "SELECT queue_name, status::text, count(*) AS n FROM procrastinate_jobs"
                " WHERE status IN ('todo', 'doing', 'failed') GROUP BY 1, 2 ORDER BY 1, 2")]
            n = conn.execute("SELECT count(*) AS n FROM procrastinate_workers"
                             " WHERE last_heartbeat > now() - interval '1 minute'").fetchone()["n"]
            out += ["# HELP abi_workers Workers with a heartbeat in the last minute.", "# TYPE abi_workers gauge",
                    f"abi_workers {n}"]
        # ponytail: sums the whole llm_usage table per scrape (one row per LLM call); keep running totals if it grows
        rows = conn.execute("SELECT provider, model, sum(prompt_tokens) AS p, sum(completion_tokens) AS c"
                            " FROM llm_usage GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
    out += ["# HELP abi_llm_tokens_total LLM tokens by provider, model and kind.", "# TYPE abi_llm_tokens_total counter"]
    for r in rows:
        ls = f'provider="{_esc(r["provider"])}",model="{_esc(r["model"])}"'
        out += [f'abi_llm_tokens_total{{{ls},kind="prompt"}} {r["p"] or 0}',
                f'abi_llm_tokens_total{{{ls},kind="completion"}} {r["c"] or 0}']
    return out


@router.get("/api/ready")
def ready() -> JSONResponse:
    """Readiness: Postgres (app DB, DuckLake catalog, queue) answers. No error text: it can name hosts."""
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
    except (psycopg.Error, OSError):
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ready"})


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request) -> PlainTextResponse:
    """Prometheus text format 0.0.4. Off (404) unless ABI_METRICS_TOKEN is set; then `Authorization: Bearer <it>`."""
    token = os.environ.get("ABI_METRICS_TOKEN")
    if not token:
        return PlainTextResponse("Not Found", status_code=404)
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), f"Bearer {token}".encode()):
        return PlainTextResponse("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
    body = "\n".join(HTTP.text() + QUERY.text() + _db_metrics()) + "\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


# SC-4: a closed list of credential shapes masked in every log line, a net under the call sites that scrub the
# secrets they resolved (credentials.scrub/log_text). Masks more than needed rather than less.
NAMES = (r"[\w-]*(?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key|account[_-]?key|private[_-]?key|"
         r"credential|signature)[\w-]*")
QUOTED = r"""b?'(?:\\.|[^'\\])*'?|b?"(?:\\.|[^"\\])*"?"""  # a quoted value, escapes kept inside, cut-off end allowed
VALUE = rf"""(?:{QUOTED}|[^\s&,;'"()]+)"""
SECRETS = [
    (re.compile(r"(?i)(\b(?:proxy-authorization|authorization|set-cookie|cookie)\s*[:=]\s*)[^\n]+"), r"\1***"),
    (re.compile(r"(://[^/\s:@]*:)[^@\s]+@"), r"\1***@"),  # URL userinfo password (may hold '/')
    (re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]+"), r"\1 ***"),
    # name=value, name: value, 'name': 'value', "name"="value", password = 'x' (kv logs, reprs, JSON, conninfo)
    (re.compile(rf"""(?i)((['"]?)(?<![\w-])(?:{NAMES})\2\s*[=:]\s*)(?!\*)({VALUE})"""), r"\1***"),
    # short names only as name=value (query strings), so "exit code: 1" stays readable
    (re.compile(rf"(?i)((?<![\w-])(?:key|code|state|sig|auth)=)({VALUE})"), r"\1***"),
    # --password value (command lines) and SQL options: SECRET 'x', PASSWORD 'x' (DuckDB CREATE SECRET, ATTACH)
    (re.compile(rf"(?i)((?<![\w-])--(?:{NAMES})[ =])({VALUE})"), r"\1***"),
    (re.compile(rf"(?i)((?<![\w-])(?:{NAMES})\s+)({QUOTED})"), r"\1'***'"),
    (re.compile(r"gAAAAA[\w-]{20,}=*"), "***"),  # Fernet ciphertext
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*"), "***"),  # JWT
    (re.compile(r"\b(?:sk-[\w-]{16,}|gh[pousr]_\w{20,}|github_pat_\w{20,}|xox[abprs]-[\w-]{10,})"), "***"),  # API keys
    (re.compile(r"(?i)(https://(?:hooks\.slack\.com|(?:\w+\.)?discord(?:app)?\.com/api/webhooks|[\w.-]*webhook\.office\.com)"
                r"/)\S+"), r"\1***"),  # incoming-webhook URLs
]


def scrub(text: str) -> str:
    for pattern, repl in SECRETS:
        text = pattern.sub(repl, text)
    return text


class Json(logging.Formatter):
    """One JSON object per line: time (UTC), level, logger, message, exception; message and exception scrubbed."""

    def format(self, record: logging.LogRecord) -> str:
        doc = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
               "level": record.levelname, "logger": record.name, "message": scrub(record.getMessage())}
        if record.exc_info:
            doc["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(doc, ensure_ascii=False)


LOG_CONFIG = Path(__file__).with_name("logging.json")


def setup() -> None:
    """Apply logging.json (the worker's entry; the app gets it via uvicorn --log-config)."""
    logging.config.dictConfig(json.loads(LOG_CONFIG.read_text()))
