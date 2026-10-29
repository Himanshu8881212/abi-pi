# Converters (outside the app)

Parquet is the only input ABI reads for the demo projects. These scripts make and land the Parquet files; ABI never
runs them and they never see ABI.

## YouTube CMS demo (`yt_cms`)

```sh
converters/.venv/bin/python converters/infra.py up            # local RustFS on 127.0.0.1:9100, bucket yt-cms (read-only for ABI)
.venv/bin/python converters/gen_cms.py scratch/cms             # 89M rows on YouTube Reporting API content owner schemas (generated)
converters/.venv/bin/python converters/upload.py scratch/cms s3://yt-cms
converters/.venv/bin/python converters/replay.py s3://yt-cms --every 30   # the stream: one day of basic + revenue per 30 s
.venv/bin/python scripts/build_cms.py youtube yt_cms           # bronze -> silver -> gold -> model -> measures, through ABI
```

Bucket layout: `batch/<report>/<day>.parquet` (history), `batch/<report>/<report>.parquet` (dimensions),
`batch/reference/*` (countries, YouTube's code lists), `pending/<report>/<day>.parquet` (the last 30 days, waiting),
`stream/<report>/<day>.parquet` (days the replayer has landed). The upload keys live only in the macOS Keychain
(service `abi-pi-rustfs`); ABI reads the bucket anonymously from 127.0.0.1.

## A Studio export (`convert.py`)

```sh
converters/.venv/bin/python converters/convert.py views_report_lifetime.xls s3://yt-studio   # keys from your env
```
