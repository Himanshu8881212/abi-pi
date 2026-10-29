#!/usr/bin/env python3
"""Outside the app: the streaming side. Moves the day files convert.py left in pending/date/ into stream/date/, one
every --every seconds, oldest first, so ABI sees new Parquet files arrive (its worker picks them up within a minute).

    converters/.venv/bin/python converters/replay.py <out> [--every 30] [--count N]

<out> is the same local folder or s3://bucket/prefix as for convert.py (S3 keys from the environment only).
"""
import argparse
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent))
from convert import Out  # noqa: E402


def pending(out: Out) -> list[str]:
    if out.s3:
        from pyarrow import fs  # noqa: PLC0415
        infos = out.fs.get_file_info(fs.FileSelector(f"{out.root}/pending/date", allow_not_found=True))
        return sorted(i.path.rsplit("/", 1)[-1] for i in infos if i.path.endswith(".parquet"))
    return sorted(p.name for p in (out.root / "pending/date").glob("*.parquet"))


def move(out: Out, name: str) -> str:
    src = f"pending/date/{name}"
    if out.s3:
        t = pq.read_table(f"{out.root}/{src}", filesystem=out.fs)
        dest = out.write(f"stream/date/{name}", t)
        out.fs.delete_file(f"{out.root}/{src}")
        return dest
    t = pq.read_table(out.root / src)
    dest = out.write(f"stream/date/{name}", t)
    (out.root / src).unlink()
    return dest


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--every", type=float, default=30)
    ap.add_argument("--count", type=int, default=0, help="stop after N files (0 = all)")
    a = ap.parse_args(argv)
    out = Out(a.out)
    todo = pending(out)[: a.count or None]
    if not todo:
        raise SystemExit("Nothing pending: run convert.py first.")
    for i, name in enumerate(todo):
        print(f"{time.strftime('%H:%M:%S')}  {move(out, name)}", flush=True)
        if i < len(todo) - 1:
            time.sleep(a.every)


if __name__ == "__main__":
    main(sys.argv[1:])
