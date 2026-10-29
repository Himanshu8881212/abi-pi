# Converters (outside the app)

Parquet is the only input ABI reads for `yt_studio`. These scripts turn a YouTube Studio export into Parquet and
put it in the RustFS bucket; ABI never runs them and they never see ABI.

```sh
python3 -m venv converters/.venv && converters/.venv/bin/pip install xlrd pyarrow pandas
export AWS_ACCESS_KEY_ID=… AWS_SECRET_ACCESS_KEY=… S3_ENDPOINT=http://127.0.0.1:9100   # your RustFS keys
converters/.venv/bin/python converters/convert.py views_report_lifetime.xls s3://yt-studio   # batch/ + pending/
converters/.venv/bin/python converters/replay.py s3://yt-studio --every 30                   # stream/: one day per 30 s
```

Bucket layout: `batch/<report>/<report>.parquet` (history and every breakdown), `stream/date/<day>.parquet` (one file
per day as it "arrives"), `pending/date/` (days not yet replayed). A local folder works in place of `s3://…`.
