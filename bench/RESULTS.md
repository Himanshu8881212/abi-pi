
## 20261029-013322 (runs=2)

| variant | task | pass | median s | turns | tools | tool errors | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|
| baseline | ask | 2/2 | 14.8 | 3.0 | 2.0 | 0.0 | 11434.0 | 18816.0 | 156.5 |
| baseline | why | 2/2 | 69.65 | 6.0 | 7.5 | 0.0 | 11842.5 | 59648.0 | 1727.5 |
| baseline | report | 2/2 | 32.05 | 6.0 | 6.0 | 0.0 | 21253.5 | 43904.0 | 445.5 |

## 20261029-014251 (runs=2)

| variant | task | pass | median s | turns | tools | tool errors | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|
| baseline | ask | 2/2 | 12.3 | 2.0 | 1.0 | 0.0 | 9591.0 | 9344.0 | 150.5 |
| baseline | why | 0/2 | 81.6 | 6.5 | 9.5 | 0.0 | 15194.0 | 63744.0 | 2065.5 |
| baseline | report | 2/2 | 30.25 | 5.0 | 4.0 | 0.0 | 26165.0 | 26112.0 | 364.5 |
| low | ask | 2/2 | 11.05 | 2.0 | 1.0 | 0.0 | 9591.0 | 9344.0 | 150.0 |
| low | why | 1/2 | 82.94999999999999 | 7.0 | 7.0 | 0.5 | 20329.5 | 57024.0 | 1974.5 |
| low | report | 2/2 | 30.9 | 5.0 | 4.0 | 0.0 | 21094.5 | 31232.0 | 360.5 |

## 20261029-014920 (runs=2)

| variant | task | pass | median s | turns | tools | tool errors | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|
| baseline | ask | 2/2 | 10.05 | 2.0 | 1.0 | 0.0 | 14609.0 | 4736.0 | 139.0 |
| baseline | why | 1/2 | 74.80000000000001 | 6.5 | 8.5 | 1.0 | 24995.0 | 55296.0 | 1802.0 |
| baseline | report | 2/2 | 34.5 | 5.0 | 4.0 | 0.0 | 21607.0 | 31616.0 | 361.0 |

## 20261029-015318 (runs=3)

| variant | task | pass | median s | turns | tools | tool errors | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|
| baseline | why | 0/3 | 74.2 | 6 | 7 | 0 | 15881 | 55552 | 1747 |

## 1.1.0 baseline (pve1 LXC 100: 6 cores, 10 GB RAM)

3 Nov 2026, bench/perf.py and bench/brewline.py of 309ee47. Method: the image the live install runs (2fcf2e3d5762, 2.84 GB;
the 252 repo files in it are byte-identical to `git archive v1.1.0`) as a throwaway compose install beside the live one
on the same LXC (own volume and port, removed after), 1.1.0's defaults (engine DuckDB limit 1500MB), no model
configured. The bench ran inside the container (`docker cp bench`, `docker exec`). Pages: Brewline 0.01 (73,941 orders)
loaded with abi.load, its starter Overview report, a 297 KB page. Load and isolation: generated orders in the files
folder, loaded with the engine's add_data on a server path. Process MB are perf.py's RSS sums (shared pages count more
than once); container MB is the cgroup's memory.current (every 2 s under load; with chats its memory.peak).

| pages: 50 sessions, 50 people | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first | 44.2 | 84.7 | 88.3 | 88.3 | 0 | 0 |
| right after a commit | 45.7 | 87.8 | 91.4 | 91.4 | 0 | 0 |

| memory MB | app | engine | worker | Postgres | Pi | container |
|---|---|---|---|---|---|---|
| idle, the pages project loaded | 30.8 | 352.7 | 359.7 | 181.7 (12 processes) | 0 | 620 |
| 8 chats started (3 runs) | 32.2 / 32.9 / 33.5 | 341.7 / 340.4 / 338.5 | 359.7 / 359.7 / 355.8 | 182.0 / 182.2 / 183.1 | 1250.5 / 850.4 / 845.5 | 1756 peak |
| idle after pages, loads and isolation | 73.6 | 370.4 | 326.3 | 285.7 (16 processes) | 0 | 706 |

Per chat (app + Pi after start; no model, so no answer): 156.5 MB in the first run after the container started, then
106.4 and 105.8 MB.

| load | orders | orders add_data s | rows/s | 4 tables s | Ready s | lake bytes | revenue by region | container peak MB |
|---|---|---|---|---|---|---|---|---|
| 10M | 9,999,949 | 46.51 | 215,006 | 48.34 | 54.31 | 251,216,670 | exact to the cent | 1565 |
| 50M | 49,999,905 | 224.32 | 222,895 | 226.20 | 252.43 | 1,181,105,645 | exact to the cent | 2408 |

The lake keeps each table twice, in raw/ and model/ (85 + 85 MB at 10M, 389 + 389 MB at 50M on ZFS's compressed
disk; lake bytes are the files' sizes). 50M stayed inside the app's and the agent's timeout (they wait 900 s for an
engine call, scripts/abi.py) and nothing failed. A server path isn't open to 1.1's app or agent (uploads and s3://
Parquet only); those doors weren't measured.

| isolation: project B (Brewline 0.005) uncached semantic_query | queries | p50 ms | p95 ms |
|---|---|---|---|
| idle | 30 | 101.9 | 108.8 |
| while project A loads 50M orders (249.41 s) | 1193 | 230.0 (x2.26) | 317.3 (x2.92) |

## 1.1.0 baseline (MacBook Air, 8 cores, 24 GB; model proxy/claude-sonnet-5-5)

3-4 Nov 2026, 309ee47 (v1.1.0's app, engine and harness). Method: bench/bench.py at scale 0.01, --model
proxy/claude-sonnet-5-5 --fallback proxy/claude-opus-5-5 (ask and why 10 runs, report 5, load 3; Sonnet answered all
56 runs, no model call failed), once on a quiet Mac (load average 1.8-4.4) and once beside another session's Rust
build (32-68); perf.py memory --chats 3 (Settings' model Sonnet for it) and pages --sessions 20 (quiet Mac) through a
local app on Brewline 0.01 loaded with abi.load, a 300 KB page.

Quiet Mac (4 Nov 00:17-00:27):

| task | pass | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|
| ask | 1/10 | 9.4 | 3 | 2 | 0 | 1 | 1064 | 68327.5 | 722.5 |
| why | 9/10 | 33.9 | 6.5 | 10 | 0 | 0 | 18201 | 172544.5 | 3902 |
| report | 5/5 | 20.5 | 6 | 8 | 0 | 0 | 34163 | 182473 | 1732 |
| load | 3/3 | 12 | 4 | 3 | 0 | 0 | 16400 | 99724 | 647 |

Beside the Rust build (3 Nov 23:29-23:43):

| task | pass | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|
| ask | 0/10 | 15.8 | 3 | 2 | 0 | 1 | 1092.5 | 68321 | 747.5 |
| why | 3/10 | 35.6 | 6.5 | 10 | 0 | 1 | 18847.5 | 176358 | 4003.5 |
| report | 3/5 | 27.5 | 6 | 7 | 0 | 0 | 53545 | 172061 | 1773 |
| load | 3/3 | 24.5 | 4 | 3 | 0 | 0 | 16395 | 99726 | 688 |

Every miss was a masked word, not a wrong figure. ask stated all four 2025 regions right in 20 of 20 and masked its own
filter's end date ("from 2025-01-01 to [not verified]") in 19. why named North in 20 of 20; its 8 misses masked a word
in the closing offer ("save this as an [not verified] analysis", 6 runs), a café name (2) or a word before "orders" (1).
The 2 report misses masked a page title and a visual count.

| pages: 20 sessions, 20 people (3 runs) | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first | 6.0 / 10.5 / 6.6 | 11.3 / 20.1 / 12.3 | 11.9 / 21.1 / 13.0 | 11.9 / 21.1 / 13.0 | 0 | 0 |
| right after a commit | 13.2 / 7.1 / 6.4 | 20.1 / 13.3 / 12.2 | 20.8 / 14.0 / 12.9 | 20.8 / 14.0 / 12.9 | 0 | 0 |

| memory MB | app | engine | worker | Postgres | Pi |
|---|---|---|---|---|---|
| idle, the pages project loaded | 38.8 | 845.8 | 1193.1 | 495.0 (30 processes) | 0 |
| 3 chats started | 42.7 | 846.1 | 1193.3 | 496.3 | 463.4 |
| after each answered (claude-sonnet-5-5) | 42.7 | 846.1 | 1193.3 | 496.3 | 463.4 |

Per chat (app + Pi): 155.8 MB after start and after the answer.
