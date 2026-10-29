#!/usr/bin/env python3
"""Outside the app: turn a YouTube Studio export (.xls/.xlsx/.csv, one sheet per report) into Parquet, the only input
format ABI reads for this project. Values and column names stay as Studio wrote them (bronze is "as landed");
cleaning, typing and renaming happen in ABI (silver).

    converters/.venv/bin/python converters/convert.py <export.xls> <out> [--stream-days 60]

<out> is a local folder or s3://bucket/prefix. For S3 (RustFS) the keys come from the environment only:
AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT (e.g. http://127.0.0.1:9100); they are never printed.

Layout written:
    batch/<report>/<report>.parquet      every sheet; for `date`, the days before the stream window
    pending/date/<day>.parquet           the last --stream-days days, one file per day, for replay.py to drop into
                                         stream/date/ (simulating rows that arrive later)
"""
import argparse
import io
import os
import re
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def sheets(path: Path) -> dict[str, pd.DataFrame]:
    if path.suffix.lower() == ".csv":
        return {slug(path.stem): pd.read_csv(path)}
    book = pd.read_excel(path, sheet_name=None, engine="xlrd" if path.suffix.lower() == ".xls" else None)
    return {slug(k): v for k, v in book.items()}


def table(df: pd.DataFrame) -> pa.Table:
    """Parquet needs one type per column: text-like columns become strings, nothing else changes."""
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: None if pd.isna(v) else str(v))
    df.columns = [str(c) for c in df.columns]
    return pa.Table.from_pandas(df, preserve_index=False)


class Out:
    """A local folder or s3://bucket/prefix (credentials from the environment only)."""

    def __init__(self, root: str):
        self.s3 = root.startswith("s3://")
        if self.s3:
            from pyarrow import fs  # noqa: PLC0415
            missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_ENDPOINT") if not os.environ.get(k)]
            if missing:
                raise SystemExit(f"Set {', '.join(missing)} in your shell first (they are read, never printed).")
            ep = os.environ["S3_ENDPOINT"]
            self.fs = fs.S3FileSystem(access_key=os.environ["AWS_ACCESS_KEY_ID"], secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                                      endpoint_override=re.sub(r"^https?://", "", ep), scheme="https" if ep.startswith("https") else "http",
                                      region=os.environ.get("S3_REGION", "us-east-1"))
            self.root = root.removeprefix("s3://").rstrip("/")
        else:
            self.root = Path(root).expanduser()

    def write(self, rel: str, t: pa.Table) -> str:
        if self.s3:
            buf = io.BytesIO()
            pq.write_table(t, buf)
            with self.fs.open_output_stream(f"{self.root}/{rel}") as f:
                f.write(buf.getvalue())
            return f"s3://{self.root}/{rel}"
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(t, p)
        return str(p)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", type=Path)
    ap.add_argument("out")
    ap.add_argument("--stream-days", type=int, default=60)
    a = ap.parse_args(argv)
    out = Out(a.out)
    for name, df in sheets(a.export).items():
        if name == "date" and a.stream_days:
            days = pd.to_datetime(df["Date"], errors="coerce")
            cut = days.dropna().max() - pd.Timedelta(days=a.stream_days - 1)
            later = df[days >= cut]
            df = df[~(days >= cut)]
            for day, part in later.groupby(days[days >= cut].dt.strftime("%Y-%m-%d")):
                out.write(f"pending/date/{day}.parquet", table(part))
            print(f"pending/date/: {len(later)} day file(s) from {cut.date()} for replay.py")
        print(f"{out.write(f'batch/{name}/{name}.parquet', table(df))}  ({len(df)} rows, {len(df.columns)} columns)")


if __name__ == "__main__":
    main(sys.argv[1:])
