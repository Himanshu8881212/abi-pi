"""Calculation tool sandbox. PRD §7.1 TL-5, §9 "Restricted subprocess", DECISIONS D-35, work/validation/X-05.md decision 1.

User code (`tools/<id>.py`, defining `calculate(rows, inputs)`) never runs in the app or worker process. `run` starts
a child process (`python -I sandbox.py`, empty environment, CPU/open-file/file-size rlimits, 10 s wall timeout) that
loads CPython built for WASI under wasmtime: fuel bounds CPU, the store caps guest memory at 128 MB, and the only
thing the guest sees is the read-only stdlib at /lib (no other files, no sockets, no environment, no processes).
Rows and inputs go in as JSON on stdin; result rows come back as JSON on stdout (at most 1 MB and 100k rows).

This file is also the child's program and the image's install step, so it imports only the stdlib and wasmtime
(`-I` puts no project path on sys.path): `python sandbox.py --install <dir>` downloads the pinned interpreter,
checks its sha256 and precompiles it (compiling costs ~390 MB once; loading the `.cwasm` costs 3 ms, D-17).
"""

import fcntl
import hashlib
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from importlib.metadata import version
from pathlib import Path

from wasmtime import (
    Config,
    Engine,
    ExitTrap,
    Linker,
    Module,
    Store,
    Trap,
    WasiConfig,
    WasmtimeError,
)

URL = "https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.13.15/python-3.13.15-wasi_sdk-24.zip"
SHA256 = "67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d"  # also pinned in the Dockerfile's ADD
WALL, CPU_S, FUEL, MEMORY = 10, 10, 20_000_000_000, 128 << 20
MAX_OUT, MAX_ROWS = 1 << 20, 100_000
SLOTS = threading.BoundedSemaphore(2)  # ponytail: per process (app and worker each run 2), a Postgres slot table if D-17 needs a global cap
# The guest's whole program: user code gets its own namespace; only its return value is printed.
RUNNER = """import json, sys
data, out = json.load(sys.stdin), sys.stdout
sys.stdout = sys.stderr  # the code's own prints never mix into the result
try:
    ns = {"__name__": "tool"}
    exec(compile(data["code"], "tool.py", "exec"), ns)
    rows = ns["calculate"](data["rows"], data["inputs"])
    out.write(json.dumps(rows, allow_nan=False))
except MemoryError:
    sys.exit(4)

"""


class SandboxError(Exception):
    """The calculation failed or broke a limit; the message is safe to show to its caller."""


def _no_constant(name: str):
    raise ValueError(name)  # NaN/Infinity aren't JSON (RUNNER dumps with allow_nan=False)


def _finite(s: str) -> float:
    if (f := float(s)) in (float("inf"), float("-inf")):  # 1e999 overflows to inf
        raise ValueError(s)
    return f


def _cwasm(d: Path) -> Path:
    return d / f"python-wasmtime-{version('wasmtime')}.cwasm"  # a .cwasm only loads in the wasmtime that wrote it


def _engine():
    cfg = Config()
    cfg.consume_fuel = True
    return Engine(cfg)


def install(d: str | Path) -> Path:
    """Download (unless the zip is already there, e.g. from the Dockerfile's ADD), check the sha256, unpack the
    stdlib and precompile python.wasm for this wasmtime version. Idempotent and safe to run in parallel."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _cwasm(d).exists():
            return d
        z = d / URL.rsplit("/", 1)[1]
        if not z.exists():
            with urllib.request.urlopen(URL, timeout=60) as r, open(f"{z}.part", "wb") as f:  # pinned https URL
                shutil.copyfileobj(r, f)
            os.replace(f"{z}.part", z)
        if hashlib.sha256(z.read_bytes()).hexdigest() != SHA256:
            z.unlink()
            raise RuntimeError(f"{z.name} doesn't match its pinned sha256; downloaded again on the next install")
        shutil.rmtree(d / "lib", ignore_errors=True)
        with zipfile.ZipFile(z) as f:
            f.extractall(d, [n for n in f.namelist() if n.startswith("lib/") or n in ("python.wasm", "LICENSE")])
        for old in d.glob("*.cwasm"):
            old.unlink()
        Path(f"{_cwasm(d)}.part").write_bytes(Module.from_file(_engine(), str(d / "python.wasm")).serialize())
        os.replace(f"{_cwasm(d)}.part", _cwasm(d))
        (d / "python.wasm").unlink()
    return d


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUT, MAX_OUT))  # stdout/stderr are files: a bigger write fails


def run(code: str, rows: list, inputs: dict) -> tuple[list[dict], int]:
    """Runs `calculate(rows, inputs)` in a fresh sandbox: (result rows, the child's peak RSS in bytes). Raises
    SandboxError with a plain reason on any failure or limit."""
    d = Path(os.environ.get("ABI_SANDBOX_PATH", "/opt/abi/sandbox"))  # the Dockerfile installs it there
    if not _cwasm(d).exists():
        raise SandboxError(f"The calculation sandbox isn't installed in {d}; run `python abi/tools/sandbox.py --install {d}`.")
    with SLOTS, tempfile.TemporaryFile() as i, tempfile.TemporaryFile() as o, tempfile.TemporaryFile() as e:
        i.write(json.dumps({"code": code, "rows": rows, "inputs": inputs}).encode())
        i.seek(0)
        p = subprocess.Popen([sys.executable, "-I", __file__, str(d)], stdin=i, stdout=o, stderr=e, env={},
                             cwd=d, preexec_fn=_limits, close_fds=True)  # noqa: PLW1509 - rlimits need it; nothing else runs there
        timer = threading.Timer(WALL, p.kill)
        timer.start()
        _, status, usage = os.wait4(p.pid, 0)
        timed_out = not timer.is_alive()
        timer.cancel()
        p.returncode = rc = os.waitstatus_to_exitcode(status)
        peak = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        full = e.tell() >= MAX_OUT
        o.seek(0)
        e.seek(-min(e.tell(), 2000), os.SEEK_END)
        out, err = o.read(), e.read().decode(errors="replace").strip()
    if timed_out or rc in (-signal.SIGKILL, -signal.SIGXCPU):
        raise SandboxError(f"The calculation ran out of its time budget ({WALL} s).")
    if rc == 3:
        raise SandboxError("The calculation ran out of its CPU budget (an endless loop?).")
    if rc == 4:
        raise SandboxError(f"The calculation ran out of memory ({MEMORY >> 20} MB).")
    if full or "File too large" in err:  # RLIMIT_FSIZE: CPython ignores SIGXFSZ, so the guest's write fails with EFBIG
        raise SandboxError(f"The calculation's output is over {MAX_OUT >> 20} MB.")
    if rc:
        raise SandboxError("The calculation failed: " + (err.splitlines()[-1][:300] if err else f"exit code {rc}"))
    try:
        result = json.loads(out, parse_constant=_no_constant, parse_float=_finite)  # guest code may write stdout itself (H-01.5-fix-7)
    except (ValueError, RecursionError):
        raise SandboxError("The calculation printed something besides its result; return the rows instead.") from None
    if not isinstance(result, list) or not all(isinstance(r, dict) for r in result):
        raise SandboxError("calculate(rows, inputs) must return a list of {column: value} rows.")
    if len(result) > MAX_ROWS:
        raise SandboxError(f"The calculation returned more than {MAX_ROWS} rows.")
    return result, peak


def _child(d: Path) -> int:
    """Runs in the child process: guest stdin/stdout/stderr are this process's (the parent's temp files)."""
    engine = _engine()
    module = Module.deserialize_file(engine, str(_cwasm(d)))  # our own build-time file only (untrusted bytes are unsafe)
    store = Store(engine)
    store.set_fuel(FUEL)
    store.set_limits(memory_size=MEMORY)
    w = WasiConfig()
    w.argv = ["python", "-I", "-S", "-c", RUNNER]
    w.env = [("PYTHONHOME", "/"), ("PYTHONDONTWRITEBYTECODE", "1")]
    w.preopen_dir(str(d / "lib"), "/lib", False)  # read-only; nothing else of the host exists for the guest
    w.inherit_stdin()
    w.inherit_stdout()
    w.inherit_stderr()
    store.set_wasi(w)
    linker = Linker(engine)
    linker.define_wasi()
    try:
        linker.instantiate(store, module).exports(store)["_start"](store)
    except ExitTrap as x:
        return x.code
    except (Trap, WasmtimeError) as x:
        if "fuel" in str(x):
            return 3
        print(f"Trap: {str(x).splitlines()[0]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1] == "--install":
        print(install(sys.argv[2]))
    else:
        sys.exit(_child(Path(sys.argv[1])))
