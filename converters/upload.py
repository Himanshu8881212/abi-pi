#!/usr/bin/env python3
"""Outside the app: copy a local folder of Parquet files into the local RustFS bucket (keys from the Keychain via
infra.py, never printed). Skips files already there with the same size.

    converters/.venv/bin/python converters/upload.py <local folder> s3://yt-cms [--only batch]
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import infra  # noqa: E402

os.environ.update(infra.env())
from convert import Out  # noqa: E402
from pyarrow import fs  # noqa: E402


def main(argv: list[str]) -> None:
    src, dest = Path(argv[0]).resolve(), argv[1]
    only = argv[argv.index("--only") + 1] if "--only" in argv else ""
    out = Out(dest)
    files = sorted(p for p in (src / only).rglob("*.parquet"))
    have = {i.path: i.size for i in out.fs.get_file_info(fs.FileSelector(out.root, recursive=True, allow_not_found=True))}

    def put(p: Path) -> int:
        key = f"{out.root}/{p.relative_to(src).as_posix()}"
        if have.get(key) == p.stat().st_size:
            return 0
        with p.open("rb") as f, out.fs.open_output_stream(key) as o:
            o.write(f.read())
        return 1

    with ThreadPoolExecutor(8) as ex:
        n = sum(ex.map(put, files))
    print(f"{n} uploaded, {len(files) - n} already there, to {dest}")


if __name__ == "__main__":
    main(sys.argv[1:])
