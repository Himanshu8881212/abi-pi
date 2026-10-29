#!/usr/bin/env python3
"""Outside the app: the streaming side. Moves the day files waiting in pending/<report>/ into stream/<report>/, one day
every --every seconds (all reports of that day together), oldest first, so ABI sees new Parquet files arrive (its
worker picks them up within a minute).

    converters/.venv/bin/python converters/replay.py <out> [--every 30] [--count N]

<out> is a local folder or s3://bucket[/prefix]; for the local RustFS the keys come from the Keychain (infra.py).
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from convert import Out  # noqa: E402


def files(out: Out, folder: str) -> list[str]:
    if out.s3:
        from pyarrow import fs  # noqa: PLC0415
        infos = out.fs.get_file_info(fs.FileSelector(f"{out.root}/{folder}", recursive=True, allow_not_found=True))
        return sorted(i.path.removeprefix(f"{out.root}/") for i in infos if i.path.endswith(".parquet"))
    return sorted(p.relative_to(out.root).as_posix() for p in (out.root / folder).rglob("*.parquet"))


def move(out: Out, rel: str) -> str:
    dest = "stream/" + rel.removeprefix("pending/")
    if out.s3:
        out.fs.copy_file(f"{out.root}/{rel}", f"{out.root}/{dest}")
        out.fs.delete_file(f"{out.root}/{rel}")
        return f"s3://{out.root}/{dest}"
    (out.root / dest).parent.mkdir(parents=True, exist_ok=True)
    (out.root / rel).rename(out.root / dest)
    return str(out.root / dest)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--every", type=float, default=30)
    ap.add_argument("--count", type=int, default=0, help="stop after N days (0 = all)")
    a = ap.parse_args(argv)
    if a.out.startswith("s3://") and "AWS_ACCESS_KEY_ID" not in os.environ:
        import infra  # noqa: PLC0415 (the local RustFS: keys from the Keychain)
        os.environ.update(infra.env())
    out = Out(a.out)
    by_day: dict[str, list[str]] = {}
    for rel in files(out, "pending"):
        by_day.setdefault(Path(rel).stem, []).append(rel)
    days = sorted(by_day)[: a.count or None]
    if not days:
        raise SystemExit("Nothing pending.")
    for i, day in enumerate(days):
        moved = [move(out, rel) for rel in by_day[day]]
        print(f"{time.strftime('%H:%M:%S')}  {day}: {len(moved)} file(s) into stream/", flush=True)
        if i < len(days) - 1:
            time.sleep(a.every)


if __name__ == "__main__":
    main(sys.argv[1:])
