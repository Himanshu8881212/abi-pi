
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

3 Nov 2026, bench/perf.py and bench/brewline.py of 309ee47. Method: the image the live install runs (2fcf2e3d5762,
2.84 GB by the size method below; the 252 repo files in it are byte-identical to `git archive v1.1.0`) as a throwaway
compose install beside the live one on the same LXC (own volume and port, removed after), 1.1.0's defaults (engine
DuckDB limit 1500MB), no model configured. The bench ran inside the container (`docker cp bench`, `docker exec`).
Pages: Brewline 0.01 (73,941 orders) loaded with abi.load, its starter Overview report, a 297 KB page. Load and
isolation: generated orders in the files folder, loaded with the engine's add_data on a server path. Process MB are
perf.py's RSS sums (shared pages count more than once); container MB is the cgroup's memory.current (every 2 s under
load; with chats its memory.peak).

| pages: 50 sessions, 50 people | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first | 44.2 | 84.7 | 88.3 | 88.3 | 0 | 0 |
| right after a commit | 45.7 | 87.8 | 91.4 | 91.4 | 0 | 0 |

"first" is each person's first page in the app, not a cold one: the engine caches results by SQL and lake snapshot,
people without row-level security share them, and the owner's page had filled them before the round. With no rule in
this project, 1.1's numbers are the app computing 50 pages in turn under its one page lock; a cache per role and rule
(M3.3) would compute this project's pages twice. `perf.py pages --rls` (every account a Viewer with its own café)
measures the cost per person; it wasn't run on pve1 (the Mac's numbers are below).

| memory MB | app | engine | worker | Postgres | Pi | container |
|---|---|---|---|---|---|---|
| idle, the pages project loaded | 30.8 | 352.7 | 359.7 | 181.7 (12 processes) | 0 | 620 |
| 8 chats started (3 runs) | 32.2 / 32.9 / 33.5 | 341.7 / 340.4 / 338.5 | 359.7 / 359.7 / 355.8 | 182.0 / 182.2 / 183.1 | 1250.5 / 850.4 / 845.5 | 1756 peak |
| idle after pages, loads and isolation | 73.6 | 370.4 | 326.3 | 285.7 (16 processes) | 0 | 706 |

Per chat (app + Pi after start; no model, so no answer): 156.5 MB in the first run after the container started, then
106.4 and 105.8 MB. Each run counted 8 Pi processes for its 8 chats.

| load | orders | orders add_data s | rows/s | 4 tables s | Ready s | lake bytes | revenue by region | container peak MB |
|---|---|---|---|---|---|---|---|---|
| 10M | 9,999,949 | 46.51 | 215,006 | 48.34 | 54.31 | 251,216,670 | exact to the cent | 1565 |
| 50M | 49,999,905 | 224.32 | 222,895 | 226.20 | 252.43 | 1,181,105,645 | exact to the cent | 2408 |

The lake keeps each table twice, in raw/ and model/ (85 + 85 MB at 10M, 389 + 389 MB at 50M on ZFS's compressed
disk; lake bytes are the files' sizes). Nothing failed. These loads used a door neither 1.1's app nor its agent has:
the engine's add_data on a server path, which perf.py reaches past abi.py's Parquet-only check (M1.1 is to limit it
to the caller's own uploads). 1.1's doors are an upload (the app takes 24 MB pieces and hands the file to the engine in
64 MB pieces, the last of which runs add_data within the 900 s the app and the agent wait for an engine call,
scripts/abi.py) and an s3:// folder of Parquet, so these times are no baseline for "a 100M-row folder loads from the
app with no timeout".

Not measured for 1.1 yet, though the 1.2 targets name them: loads through the app's upload and from an s3:// folder at
10M, 50M and 100M rows; a measure-only Publish (seconds, sources reloaded); the bronze-silver-gold rebuild's lake
bytes; a killed process serving again; 8 chats and a load together inside a 4 GB limit (measured apart above: 1756
and 2408 MB container peaks). The 1.1.0 image (ghcr.io/himanshu8881212/abi:1.1.0, 2fcf2e3d5762 on pve1) stays
measurable after the code moves on. Part 2 below has all of them but the s3:// loads.

| isolation: project B (Brewline 0.005) uncached semantic_query | queries | p50 ms | p95 ms |
|---|---|---|---|
| idle | 30 | 101.9 | 108.8 |
| while project A loads 50M orders (249.41 s) | 1193 | 230.0 (x2.26) | 317.3 (x2.92) |

Image size, measured one way from now on: `docker image inspect --format '{{.Size}}'` in pve1's Docker 26.1.5
(overlay2 store), linux/amd64, layers uncompressed: 2,843,622,285 bytes for 1.1.0. Compressed (`docker save | gzip -6`)
it is 993,637,823 bytes, about what a pull downloads. REPORT.md #40's 3.98 GB is another measure: the Mac's Docker
Desktop on 29 Oct, an arm64 build of an earlier Dockerfile, not measured again here. A store that keeps the compressed
layers beside the unpacked ones (Docker Desktop's containerd store) counts both, 2.84 + 0.99 = 3.83 GB, which is
probably most of the gap. By this measure 1.1.0 already meets the plan's "default image <= 3.0 GB" (set from the
3.98 GB), so M4.3's size target needs restating against 2.84 GB.

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

Re-scored with bench.py's checks as the M0 review tightened them (any figure anywhere passed ask, any mention of North
passed why, any line passed report): from the logged answers, ask and why pass exactly as above. All 20 ask answers
state each region's figure after its own name, and all 20 why answers give both months' revenue, North's two months
after its name, and North as where the drop sits. Counted from the harness's footer, ask masked 2 figures per run,
not 1 (the end date's two parts share one marker), so its masked median is 2 in both tables. report would pass 3/5
and 2/5: by their own answers quiet runs 0 and 3 and run 1 beside the build drew the line on the month number alone
(12 points that add 2023-2025 together), which the line check now fails (the reports were deleted, so this comes from
the answers). The check that a question changes nothing looked only at uncommitted files, in a project with
auto-commit on (abi.load turns it on), so it couldn't see a change; bench.py now also compares the last commit. why
went from 9/10 to 3/10 between the two sessions on the same code: judge M2's gate on 20 runs or more, and read the
masked figures per run in the logs.

| pages: 20 sessions, 20 people (3 runs) | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first | 6.0 / 10.5 / 6.6 | 11.3 / 20.1 / 12.3 | 11.9 / 21.1 / 13.0 | 11.9 / 21.1 / 13.0 | 0 | 0 |
| right after a commit | 13.2 / 7.1 / 6.4 | 20.1 / 13.3 / 12.2 | 20.8 / 14.0 / 12.9 | 20.8 / 14.0 / 12.9 | 0 | 0 |

These runs didn't wait for the worker's query copies of the new project or note the lake snapshot, so a copy landing
mid-round may explain why the order swaps. Again 4 Nov 01:25-01:30 (load average 1.7-2.6) with perf.py's M0 review
fixes: a new Brewline 0.01 bench project (`perf.py project`), its copies written before the first round, the lake
snapshot the same at the start and end of every round, 20 sessions, 3 runs of each kind in turn:

| pages: 20 sessions, 20 people (3 runs) | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first: Viewers and Developers, no rule | 5.1 / 5.2 / 5.3 | 9.6 / 9.9 / 10.0 | 10.1 / 10.4 / 10.5 | 10.1 / 10.4 / 10.5 | 0 | 0 |
| right after a commit | 5.0 / 5.2 / 5.3 | 9.4 / 9.9 / 10.1 | 9.9 / 10.4 / 10.6 | 9.9 / 10.4 / 10.6 | 0 | 0 |
| first: `--rls cafes.cafe_id`, 20 Viewers with a café each | 8.9 / 8.2 / 9.0 | 16.9 / 15.6 / 16.7 | 17.8 / 16.4 / 17.6 | 17.8 / 16.4 / 17.6 | 0 | 0 |
| right after a commit | 7.8 / 8.3 / 8.9 | 14.9 / 15.9 / 16.8 | 15.7 / 16.7 / 17.7 | 15.7 / 16.7 / 17.7 | 0 | 0 |

| memory MB | app | engine | worker | Postgres | Pi |
|---|---|---|---|---|---|
| idle, the pages project loaded | 38.8 | 845.8 | 1193.1 | 495.0 (30 processes) | 0 |
| 3 chats, each after one short answer (claude-sonnet-5-5) | 42.7 | 846.1 | 1193.3 | 496.3 | 463.4 (4 processes) |

Per chat (app + Pi): 155.8 MB after the answer. This was one state, not two: a chat's Pi starts with its first
message, and the answers came while perf.py waited for the Pi processes to settle, so its "started" and "answered"
samples were the same. The Pi column summed every process the app had started (4 for 3 chats), so the figure per
chat is a little off; perf.py now counts only `pi --mode rpc` and their children and gives a figure per chat only with
one Pi per chat.

## 1.1.0 baseline, part 2 (pve1)

4 Nov 2026, 01:00-02:45 UTC: the 1.1 numbers part 1 left out, measured as part 1 was. The image the live install runs
(2fcf2e3d5762, v1.1.0) ran as a throwaway compose install beside the live one on LXC 100: project, container and
volume abi-bench, port 8799, the owner's password in a root-only file. It was removed afterwards, and the live
container's start time, restart count and image were the same before and after. 1.1.0's defaults (a DuckDB limit of
1500MB per engine and worker instance), no model. bench/perf.py of this commit ran inside the container
(`docker cp bench`, `docker exec`): part 1's, plus the publish, medallion and kill benches, memory --hold and load's
error record, all added for these runs. One timed run at a time, each started at an LXC 1-minute load average of
0.1-1.5 with no docker build or other bench running. Another agent's small HTTPS test install (a second ABI and Caddy)
was up from the 0.01 Publish to the worker kill (01:12-01:24). Container MB is the cgroup's memory.current, sampled
every 2 s. Pages, the 0.01 Publish and the kills ran on perf_brewline: Brewline 0.01 (73,941 orders) made by
`perf.py project`, with its Overview report, a 296 KB page.

| pages: 50 sessions, 50 people | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first: Viewers and Developers, no rule | 45.1 | 85.8 | 89.6 | 89.6 | 0 | 0 |
| right after a commit | 45.5 | 87.6 | 91.2 | 91.2 | 0 | 0 |
| first: `--rls cafes.cafe_id`, 50 Viewers with a café each | 86.1 | 167.7 | 174.7 | 174.7 | 0 | 0 |
| right after a commit | 80.7 | 152.7 | 159.4 | 159.4 | 0 | 0 |

The worker's query copies were written before the first round, and the lake snapshot was the same at the start and
end of every round. Without a rule the run repeats part 1 (p95 84.7 and 87.8 s). With a rule per person, every page
is that person's own computation, made in turn under the app's one page lock: p95 rises 1.7-2.0x. 1.2's target of at
least 3x better with --rls therefore means a p95 of 50.9 s or less right after a commit (55.9 s first) at 50 sessions.

| load through the app's upload, one file a table | orders | orders s | rows/s | Ready s | lake bytes | revenue by region | container peak MB |
|---|---|---|---|---|---|---|---|
| 10M | 9,999,949 | 47.56 | 210,260 | 54.82 | 251,283,505 | exact to the cent | 2,021 |
| 50M | 49,999,905 | 234.96 | 212,802 | 262.66 | 1,181,127,208 | exact to the cent | 2,828 |
| 100M (two runs) | 99,999,922 | 466.94 | 214,163 | never: build_model ran out of memory | 2,303,588,044 | no model to ask | 3,233 / 3,361 |

"orders s" is the orders file's upload as perf.py's browser sends it: 24 MB pieces to the app, which hands the file to
the engine in 64 MB pieces; the last piece runs add_data and answers once the table is in. It is perf.py's same
measure as part 1's "orders add_data s", and at 10M and 50M the app's door comes within 5% of part 1's server path
(46.51 and 224.32 s). At 100M the orders arrived within the 900 s the app and the agent wait for an engine call, both
times: the whole upload took 466.94 s in the second run, and the engine's part took 448.8 and 449.3 s by its log. The
first run's line was lost, because perf.py's load didn't record an engine refusal yet; it does now. Then, both times,
build_model (the model a first load builds, as abi.load and perf.py do) answered 503 within 35 s: "'build_model' ran
out of memory: this data needs more than the server's DuckDB memory limit allows. Try again later, or raise
ABI_DUCKDB_MEMORY_LIMIT." 1.1's docker-compose.yml doesn't pass that setting on (M3.4 will). So with 1.1's defaults
a 100M-order upload lands its tables (2.30 GB of lake) but gets no model: not Ready, and nothing to ask. The container
peak is ABI's part, from the first piece on: at 10M and 50M during the end steps (the model, its checks, the worker's
query copies), at 100M during the refused build_model. Before that, perf.py's own generator and its one-file copy of
the months peaked at 1,638, 2,568 and 4,067 / 4,071 MB, which a person's upload doesn't have. Loads from an s3://
folder stay unmeasured: pve1's S3 store belongs to the live install.

| Publish, as the button (the owner) | orders | Publish answers s | a Viewer's page shows it s | tables reloaded |
|---|---|---|---|---|
| first | 73,941 | 4.35 | 6.84 | 4: cafes, customers, orders, products |
| measure-only: one measure's description | 73,941 | 3.42 | 6.02 | 4: the same |
| first | 7,400,054 | 36.13 | 40.08 | 4: the same |
| measure-only: one measure's description | 7,400,054 | 39.83 | 43.82 | 4: the same |

`perf.py publish` presses Publish as the button does (POST /publish, its confirmation answered). It does this first on
a never-published project, then after a commit that only gives one measure a new description. "Shows it" is when a
Viewer's page comes from the published copy with the new description. The reloads are the published copy's
"refresh <table>" snapshots in the meantime. After the import, the app reloads every source (scripts/app.py,
published_now), though the import itself marks no table for a measure-only change. So a measure-only Publish costs a
full reload: 3.4 s at 74k orders and 39.8 s at 7.4M (Brewline 1, `perf.py project --scale 1`), against 1.2's 15 s or
less with no reload.

`perf.py medallion --rows 10000000` builds bronze, silver and gold as 1.1 can. Bronze is three sources as add_data
loads them: cafés, products, and the orders' 36 monthly files on a server path, as part 1's loads. silver_orders is
the orders cleaned (customer_id nulls to 'guest', quantity > 0) and joined with cafés and products. gold_revenue is
revenue and orders by region and month. A 1.1 table's steps start from a loaded source, so gold repeats silver's steps
(the medallion skill's recipe), and every stepped table loads the source again into its own raw table. The build took
46.81 s for bronze_orders, 51.02 s for silver_orders and 43.97 s for gold_revenue. The lake then held 375,468,448
bytes in raw/, 319,956,857 in model/ and 431,084,933 in query/ (the worker's query copies). One rebuild (refresh
bronze_orders, which reloads what reads it) took 147.52 s and wrote:

| one rebuild, 10M orders | raw/ | model/ | its loads | query/ (the worker) | in all |
|---|---|---|---|---|---|
| bytes written | 375,463,083 | 319,853,188 | 695,316,271 | 430,914,401 | 1,126,230,672 |

Its snapshots, in order: refresh bronze_orders, build query copies, refresh silver_orders, build query copies, refresh
gold_revenue. The orders source was read three times, so raw/ holds it three times, about 125 MB each. The worker
rewrote the query copies twice in the meantime, on its own minute, and its next build after the last refresh fell
outside the measure (the first run, otherwise the same, wrote 1,125,851,847 bytes in all). Gold's revenue by region
was exact to the cent. For 1.2's "60% of 1.1's lake bytes or less", the loads' 695,316,271 bytes make that 417 MB or
less. Compare like with like: the copies' share depends on where the worker's minute falls.

| kill -9 inside the container | the engine's /api/ready | the owner's page | the process back | serving again |
|---|---|---|---|---|
| the engine (uvicorn) | no answer in the 300 s watched | 200 for 58.5 s (the app's cached page and role), then 503 "ABI isn't answering right now" | never | not within 300 s |
| the worker (procrastinate) | answered throughout | 200 throughout | never | not within 300 s: its jobs (query copies, reloads of changed files, flows, schedules) stopped |

`perf.py kill --part engine|worker --wait 300` kills the process its pid file names and probes every second. 1.1
restarts neither: both run in the background of the container's entrypoint. The HEALTHCHECK (a curl of the app's /)
stayed healthy throughout, and Docker restarts only a container that exits. After each watch, `docker restart
abi-bench` (the command returns in 0.5 s) brought everything back. Counted from the command, /api/ready answered
after 9.4 s and the owner's page after 15.7 s following the engine kill, and after 11.3 and 17.0 s following the worker
kill. The owner's page was polled inside the container with perf.py's App.

For the last runs the same install and volume got `mem_limit: 4g` and `memswap_limit: 4g` (memory.max 4 GiB, no swap
beyond it). 50M orders went through the app's upload (`perf.py load --door app`). Once the first piece reached the
app, 8 chats started (`perf.py memory --chats 8 --hold 420`, no model, so each chat's Pi was up and idle) and stayed
open through the upload, the load, the model and its checks. They started after perf.py's own generator and one-file
copy, which a person's upload doesn't have.

| inside 4 GiB, no swap | chats with a Pi | orders s | Ready s | revenue by region | peak from the chats on | OOM kills | restarts |
|---|---|---|---|---|---|---|---|
| 8 idle chats + 50M orders through the app | 8 at the start, 8 after the load | 237.83 | 266.11 | exact to the cent | 4,259,540,992 bytes | 0 | 0 |

The peak is the cgroup's memory.peak, reset when the chats started. It is 99.2% of the limit, 34 MiB short of it,
during the load's end steps: the model, its checks and the worker's query copies, with the chats up. The 2 s samples
show 3,788 MB there, 3,666 MB of it anonymous. Nothing was killed: memory.events counted no oom, oom_kill or max,
Docker's OOMKilled stayed false, the container never restarted, and the host's kernel log has no OOM line. The chats'
Pi processes took 809.1 MB at the start and 668.0 MB after the load (RSS sums; 109.6 MB a chat at the start). These
chats were idle; a model answering would add each turn's memory. So 1.1 meets "8 chats and a load in 4 GB without OOM"
here, but with almost no room. An earlier identical run peaked at 3,837 MiB over the whole run, with no kill either.
Its record counted no Pi, because on Linux Pi renames its process `pi` about a second after it starts, and perf.py's
memory, since the M0 review, matched only `pi --mode rpc`. Its chats were up all the same (the cgroup held 1,385 MB
with them after the load), and perf.py now counts `pi` too.

The volume held 13 GB at the end: 9.6 GB of lake (perf_brewline and 11 deleted projects' lakes, kept for their
retention) and 2.8 GB of uploads that no project reads any more (M3.5).
