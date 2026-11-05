
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

## 1.2 M3 round 1 (pve1)

4 Nov 2026, 17:01-18:22 UTC: M3 (M3.1-M3.5, release-1.2 at ac999af) measured on pve1 as part 2 measured 1.1.0.
`git archive ac999af` built on LXC 100 (`docker compose build`, every layer before the repo's own files from the
build cache of earlier builds: image abi-m3:ac999af, 2,843,792,690 bytes by the size method above) ran as a throwaway
compose install beside the live one: project, container and volume abi-m3, port 8796 on the LXC's loopback, the
owner's password in a root-only file. It was removed afterwards, and the live container's start time, restart count
and image were the same before and after. This branch's docker-compose.yml defaults (a DuckDB limit of 1500MB per
project in each engine process, ABI_INLINE_ROWS 4,400,000, ABI_HEAVY_CONCURRENCY 1, ABI_PG_POOL 20, ABI_MAX_AGENTS 8,
ABI_QUERY_TIMEOUT 60), no model. bench/perf.py of this commit ran inside the container (`docker cp bench`,
`docker exec`) with part 2's subcommands; isolation gained `--door app` for this round, so the other project's load is
load --door app's (four tables through the app's upload, the model, Ready). One timed run at a time, each started at
an LXC 1-minute load average of 0.30-1.47 with no docker build, no other bench and no container but abi and lake
beside it; [a-b] below is the 1-minute load average's range, sampled every 2 s during the run. Container MB is the
cgroup's memory.current, sampled every 2 s. Pages, the 0.01 Publish and the kills ran on perf_brewline: Brewline 0.01
(73,941 orders) made by `perf.py project`, its Overview report, a 261 KB page (part 2's was 296 KB: docs left the
page data in M3.3).

| M3 target (PLAN.md) | 1.1.0 (part 2) | 1.2 M3 round 1 | |
|---|---|---|---|
| 50 sessions right after a commit: page p95 <= 2 s | 87.6 s | 1.40 s | met |
| the same with `--rls cafes.cafe_id` (50 Viewers, a café each): 3x better, <= 50.9 s | 152.7 s | 25.8 s (5.9x) | met |
| 100M orders through the app's upload: no client timeout, Ready with a model, exact | orders in, then build_model out of memory: no model | Ready at 526.3 s, exact to the cent | met |
| another project's uncached queries during that load: <= 1.5x idle | x2.26 p50, x2.92 p95 (part 1: 50M, server path) | x2.38 p50, x2.98 p95 | missed |
| measure-only Publish on Brewline 1 (7.4M orders): <= 15 s, no source reloaded | 39.83 s, 4 reloaded | 1.96 s, none | met |
| kill -9 of the engine, then a worker: /api/ready and a page again <= 60 s | never (300 s watched each) | 7.1 s; 1.0 s | met |
| 8 chats + 50M orders through the app inside mem_limit 4g, no swap: no OOM kill, exact | no kill, exact, peak 4,259,540,992 bytes | no kill, exact, peak 4,145,115,136 bytes (on ac999af; 28e5153 in round 3: 3,912,454,144) | met |

| pages: 50 sessions, 50 people | p50 s | p95 s | p99 s | max s | errors | dropped |
|---|---|---|---|---|---|---|
| first: Viewers and Developers, no rule | 1.6 | 1.7 | 1.7 | 1.7 | 0 | 0 |
| right after a commit | 1.3 | 1.4 | 1.4 | 1.4 | 0 | 0 |
| first: `--rls cafes.cafe_id`, 50 Viewers with a café each | 15.4 | 27.2 | 27.5 | 27.5 | 0 | 0 |
| right after a commit | 14.8 | 25.8 | 26.2 | 26.2 | 0 | 0 |

Started at load 0.47 [0.47-1.99] and 1.24 [1.20-5.08]. The worker's query copies were written before the first round,
and the lake snapshot was the same at the start and end of every round. Without a rule, 50 people of two roles need
two page computations (M3.3 keys page data by role and rule), where 1.1.0 made 50 in turn under one lock. With the
rule, every Viewer's report rows are their own computation, four at a time (scripts/app.py's _PAGES), with the rest of
the page computed once for all of them: 25.8 s meets M3's target and is 5.9x 1.1.0's, but it is 5x M3.6's 5 s.

| load through the app's upload, one file a table | orders | orders s | rows/s | Ready s | lake bytes | revenue by region | container peak MB |
|---|---|---|---|---|---|---|---|
| 100M | 99,999,922 | 468.17 | 213,597 | 526.33 | 2,303,588,044 | exact to the cent | 3,793 |
| 100M, isolation's (queries beside it all the time) | 99,999,922 | 522.53 | 191,376 | 599.94 | 2,873,616,405 | exact to the cent | 4,734 |

Started at load 1.39 [1.28-9.16] and 1.17 [1.15-13.05]. The upload's last piece now answers at once with a run (the
file is past ABI_INLINE_ROWS) and the heavy worker loads it; perf.py waits as the page does, asking load_runs every
2 s, so "orders s" is again from the first piece until the table is in, as in part 2 (1.1.0: 466.94 s). In the second
run the heavy worker's job took 496.8 s of it (queued 0.02 s), the pieces the rest. build_model at 100M orders now
fits the 1500MB (M3.2): Ready 57 and 76 s after the orders (1.1.0: out of memory, twice). The first run's lake bytes
equal 1.1.0's to the byte; the second run's holds 570 MB more, the worker's query copies of it being written within
its minute as build() measured (that project's archived lake holds 934,605,230 bytes in query/). The container peak
is ABI's part, from the first piece on, as part 2 counted it (1.1.0 at 100M: 3,233 / 3,361 MB): at the end, during
build_model and its checks. Before that, perf.py's own generator and its one-file copy of the months peaked at 4,221
and 4,878 MB (part 2: 4,067 / 4,071).

| isolation: project B (Brewline 0.005) uncached semantic_query | queries | p50 ms | p95 ms |
|---|---|---|---|
| idle | 30 | 79.9 | 95.0 |
| while project A loads 100M orders through the app, to Ready (599.95 s) | 3,393 | 189.9 (x2.38) | 283.0 (x2.98) |
| diagnostic: the same again, the heavy worker's threads at nice 10 (idle 30: 77.3 / 83.1) | 3,941 | 160.2 (x2.07) | 230.0 (x2.77) |

Missed. 1.1.0's part 1 (50M orders on a server path, loaded inside the engine) was x2.26 and x2.92: the load has left
the engine for the heavy worker (M3.1, M3.2), so B's queries no longer wait on its process, but both processes still
share the LXC's 6 cores, and each process's DuckDB runs 8 threads (`threads` is never set, and DuckDB counts the
host's 8 CPUs: os.cpu_count() 8, sched_getaffinity 6 inside the container). The load average sat at 10-11 while the
orders loaded. The diagnostic (not a setting) ran the same isolation again, last, in the container recreated without
the limit, with each of the heavy worker's 59 threads reniced to 10 (checked every 30 s): x2.07 and x2.77, so CPU
priority alone doesn't bring it to 1.5x, and the load's write pipeline is already one thread (storage._ordered).
What else B's queries wait for meanwhile is not found yet: the disk (the lake's 2.3 GB written, the upload's file),
memory bandwidth and Postgres are the candidates, and a CPU and I/O sample during the load would tell them apart.

| Publish, as the button (the owner) | orders | Publish answers s | a Viewer's page shows it s | the copy's tables in s | tables reloaded |
|---|---|---|---|---|---|
| first | 73,941 | 0.72 | 1.08 | 24.68 | 4: cafes, customers, orders, products |
| measure-only: one measure's description | 73,941 | 0.41 | 0.89 | | none |
| first | 7,400,054 | 0.87 | 1.22 | 65.42 | 4: the same |
| measure-only: one measure's description | 7,400,054 | 1.96 | 3.08 | | none |

Started at load 1.47 [1.25-1.51] and 1.17 [1.17-4.88]. Publish no longer reloads (M3.2): the import marks the tables it
changed and the heavy worker loads exactly those within its minute. So a measure-only Publish reloads nothing, and a
first Publish answers at once while the copy's tables arrive later: 24.68 and 65.42 s (1.1.0 answered after reloading
them, in 4.35 and 36.13 s). Until then the Viewer's page comes from a copy without its data (not measured: what it
shows meanwhile). Round 3 saw 46.83 and 83.21 s. This is a 1.1 to 1.2 behaviour change for the upgrade notes: after a
first Publish, or one that changes a source, Viewers see the copy's data a minute or so later.

| kill -9 inside the container | the engine's /api/ready | the owner's page | the process back | serving again | health, every 2 s |
|---|---|---|---|---|---|
| the engine (uvicorn) | from 7.1 s | 503 until 7.1 s, then 200 | a new pid at 1.1 s | 7.1 s | Docker: healthy throughout; `engine.sh health`: "The engine isn't ready" for 3 samples (about 6 s), then healthy |
| the worker (procrastinate, default queue) | throughout | 200 throughout | a new pid at 1.0 s | 1.0 s | healthy throughout, both; its next minute's jobs ran at 17:14:00 |

Started at load 1.22 and 0.53. The supervisor (M3.4) logged each: "the engine (pid 62) ended with status 137 after
603 s; it starts again in 0 s". `perf.py kill` watched from the kill until serving again; the health was sampled from
10 s before until 90 s after, with Docker's own check (every 30 s) passing at each of its probes; none fell in the 6 s
the engine was away. 1.1.0 brought back neither within 300 s, until `docker restart`.

| inside 4 GiB, no swap | chats with a Pi | orders s | Ready s | revenue by region | peak from the chats on | OOM kills | restarts |
|---|---|---|---|---|---|---|---|
| 8 idle chats + 50M orders through the app | 8 at the start, 8 after the load | 241.75 | 271.67 | exact to the cent | 4,145,115,136 bytes | 0 | 0 |

Started at load 1.47 [0.65-7.85]. Part 2's method: the container recreated with `mem_limit: 4g` and `memswap_limit: 4g`
(memory.max 4 GiB, swap.max 0), `perf.py load --rows 50000000 --door app`, and once its first piece reached the app,
`perf.py memory --chats 8 --hold 420` (no model: each chat's Pi up and idle), the cgroup's memory.peak reset then. The
peak is 96.5% of the limit, 143 MiB short (1.1.0: 34 MiB), during the load's end steps; the 2 s samples show 3,891 MB
there, 3,783 MB of it anonymous. memory.events counted no oom, oom_kill or max, Docker's OOMKilled stayed false, the
container never restarted and the supervisor logged no part ending. The parts by perf.py's RSS sums, idle and with the
chats started: engine 316.5 / 341.4 MB, the two workers 600.9 / 616.6 MB (1.1.0's one: 303.7 / 310.7), Postgres
576.0 MB in 36 processes / 715.7 MB in 44 (1.1.0: 265.0 in 17 / 251.3 in 16; three engine processes now, each with
M3.3's pool), the chats' Pi 1,215.3 MB at the start and 875.0 MB after the load (1.1.0: 809.1 and 668.0), 165.8 MB a
chat at the start (1.1.0: 109.6).

Seen on the way, for round 2:
- The heavy worker died once, with status 139 (SIGSEGV; the kernel: "procrastinate[...]: segfault at 0 ip
  0000000000000000"), at 17:49:41, inside the minute's build_copies job, which had started at 17:49:01: the isolation
  run had deleted its 100M project at 17:49:28 (delete_project moves the lake), and that project's archived lake
  holds the copies' 934,605,230 bytes. The supervisor started a new worker in 0 s and the next minute's jobs ran.
  Not reproduced. A heavy job running beside it (a load, with ABI_HEAVY_CONCURRENCY 2) would die with it, and a
  load's run then stays "running" (M3.2's open risk).
- A load's "rows so far" (DuckDB's query_progress of the read times the estimate) reached 98% two minutes into the
  isolation run's 8.3-minute job and stayed there for the rest (the model table, the snapshot, the checks).
- The volume held 13,991,192,971 bytes at the end (apparent size): 9.76 GB of lake (perf_brewline, perf_brew1 and 8
  deleted projects' lakes, kept for their retention) and 4.14 GB of uploads, which M3.5 purges with them after 30 days.

## 1.2 M3 round 2 (pve1): isolation

Round 1 missed one target: another project's uncached queries during a 100M load. Found and fixed here. A throwaway
compose install of this branch (abi-m3r2, its own port, volume and image, removed after; the live abi and lake
containers' start time, restarts and image unchanged), each run at an LXC load average under 1.5. `diag` is
perf.py isolation --door app with each of B's queries followed by the parts it is made of, and each engine
process's CPU and the container's cpu/io pressure sampled every 2 s.

What B's query waits for (20M orders; idle -> during the load, p50 ms): the whole semantic_query 78.0 -> 175.8; the
same SQL on a DuckDB of another process 18.5 -> 38.1; a lake snapshot read 6.1 -> 10.8; a new Postgres connection
3.6 -> 4.9; a WAL-flushing commit 0.2 -> 0.2. I/O pressure stayed 0 and CPU pressure rose to 30-45 %; the heavy
worker ran at 374 % CPU. Every part that runs on a CPU got about 2x slower, so the cause is CPU, not disk or
Postgres. pve1 is a Ryzen 5 PRO 2400G: 4 cores, 8 threads. The container may use 6 of them, but DuckDB counts the
host's 8, and every project's lake in every process ran 8 threads. A load occupied every core, so B's threads shared
their cores with it. Renicing it doesn't help (round 1: x2.07) because a lower priority still shares the core.

| one project's lake threads (20M orders) | load to Ready s | x p50 | x p95 |
|---|---|---|---|
| 8, DuckDB's count (round 1's code) | 118.50 | 2.25 | 2.41 |
| 6 (the container's CPUs) | 125.51 | 1.85 | 2.00 |
| heavy worker 4, engine 6 | 143.56 | 1.37 | 1.45 |
| heavy worker 3, engine 6 | 157.39 | 1.23 | 1.21 |

At 100M, with only the heavy worker at 3, the rest at 6: x1.24 p50 and x1.56 p95. B's queries over time showed
where the tail came from: during the heavy worker's load they stayed at p95 104-114 ms, and from 720 s on they
rose to p95 293-405 ms. That is when build_model ran in the engine itself (66 s at 455-488 % CPU), on A's lake of
6 threads beside B's. So the cap belongs to every lake, not only to the heavy worker's: `storage.THREADS`, half the
CPUs the process may use (ABI_DUCKDB_THREADS overrides it), set when a lake is attached (never `SET threads` at run
time, D-04-fix-2).

| 100M orders through the app's upload, to Ready | orders s | Ready s | idle p50 / p95 ms | during p50 / p95 ms | x p50 | x p95 |
|---|---|---|---|---|---|---|
| 1.1.0 (part 1: 50M, server path) | | | | | 2.26 | 2.92 |
| round 1 (every lake 8 threads) | 522.53 | 599.94 | 79.9 / 95.0 | 189.9 / 283.0 | 2.38 | 2.98 |
| heavy worker 3, engine 6 | 719.31 | 785.15 | 75.7 / 81.4 | 93.8 / 130.8 | 1.24 | 1.61 |
| every lake 3 (diag) | 725.58 | 824.85 | 78.1 / 86.8 | 92.5 / 118.7 | 1.18 | 1.37 |
| every lake 3, this commit's image | 729.83 | 824.88 | 77.0 / 153.5 | 91.1 / 119.3 | 1.18 | (0.78) |

Borderline, not met as first written here. In the last run one idle query in 30 took 153 ms, so its own x p95 is 0.78;
scored against the other runs' idle p95 (81.4-86.8 ms) its during-load p95 (119.3 ms) is x1.37-1.47, but that swaps
the denominator, which 1.1.0's and round 1's scores never did. Scored each against its own idle, only the diag run met
1.5x (x1.37), and round 3 below (this commit's image, built clean) gave x1.52. A 30-query idle sample's p95 is about
its second-highest query, so the next round needs a larger idle sample (for example 200). The 1.1.0 row is part 1's
50M on a server path, not 100M through the app's upload: not like for like as a ratio. Every load was exact to the cent. The cost: a 100M
load alone takes 38 % longer (orders in 730 s, Ready at 825 s against 522 and 600), and build_model 96 s against
66 s. A small query alone is no slower (idle p50 77 ms against 80). One big query of one project gets half the
CPUs. ABI_DUCKDB_THREADS set to the CPU count gives back round 1's speed and round 1's isolation. Container peak
4,540 MB with no memory limit.

## 1.2 M3 round 3 (pve1): 28e5153 built clean

4 Nov 2026, 20:50-21:34 UTC: `git archive 28e5153` built on LXC 100 as a throwaway compose install (abi-m3, port 8796,
its own volume; the live abi and lake containers untouched), perf.py inside it as in round 1, one run at a time at an
LXC load average of 0.9-1.5 at each start. Recorded here after the review (the run's own report wasn't written down
before the Mac's disk filled); the numbers are the install's logs (/opt/abi-m3/logs on LXC 100).

| run | result |
|---|---|
| isolation: B's uncached queries while A loads 100M orders through the app, to Ready | idle (30) p50 76.0 / p95 83.5 ms, during (8,459) 93.1 / 127.3 ms: x1.22 p50, x1.52 p95. Orders in 739.0 s, Ready 842.89 s, exact |
| Publish, first: the copy's tables in | 46.83 s (Brewline 0.01), 83.21 s (Brewline 1, 7.4M orders); measure-only reloads none (0.92 s, 2.41 s) |
| inside 4 GiB, no swap: 8 idle chats + 50M orders through the app | orders 326.31 s, Ready 364.65 s, exact; memory.peak from the chats on 3,912,454,144 bytes; memory.events oom 0, oom_kill 0, max 0; OOMKilled false, 0 restarts |

Isolation, scored against its own idle as 1.1.0 and round 1 were: x1.52 p95 misses 1.5x by 0.02; with the diag run's
x1.37 it is borderline (round 2). The 4 GiB target holds on 28e5153, with 365 MiB to spare (round 1 on ac999af:
143 MiB). The no-limit isolation run's container memory.peak was 4,940,763,136 bytes.

## 1.2 M3 round 3 (pve1): c2c7437, the final M3 code

4-5 Nov 2026, 22:36-23:49 UTC: release-1.2 at c2c7437 (M3.1-M3.5, round 2's thread cap and the review fixes),
`git archive c2c7437` built on LXC 100 as round 1's throwaway install (project, container and volume abi-m3, port
8796 on the LXC's loopback, the owner's password in a root-only file; image abi-m3:c2c7437, 2,843,797,743 bytes),
removed afterwards; the live abi and lake containers' start time, restart count and image were the same before and
after. The 28e5153 install the round above left running was removed first. bench/perf.py ran inside the container
with round 1's subcommands and scripts, one run at a time, each started at an LXC 1-minute load average of 0.10-1.49
with no build, no other bench and no container but abi and lake beside it; [a-b] is the load average's range during
the run. Two changes of method: isolation's idle sample is 200 queries (round 2: a 30-query p95 is its second-highest
query), and `perf.py memory` opens each chat as its own person (this commit's fix: since c2c7437 one person runs at
most half of ABI_MAX_AGENTS, so 8 chats of one account started 4 Pis, peak 3,728,089,088 bytes, not counted).

| M3 target (PLAN.md) | 1.1.0 (part 2) | 1.2 M3 round 3 (c2c7437) | |
|---|---|---|---|
| 50 sessions right after a commit: page p95 <= 2 s | 87.6 s | 1.44 s [1.20-2.50] | met |
| the same with `--rls cafes.cafe_id` (50 Viewers, a café each): 3x better, <= 50.9 s | 152.7 s | 25.59 s (6.0x) [1.35-4.14] | met |
| 100M orders through the app's upload: no client timeout, Ready with a model, exact | build_model out of memory: no model | orders in 733.26 s, Ready at 826.96 s, exact to the cent | met |
| another project's uncached queries during that load: <= 1.5x idle | x2.26 p50, x2.92 p95 (part 1: 50M, server path) | idle (200) 76.7 / 88.0 ms, during (8,434) 92.4 / 126.2 ms: x1.20 p50, x1.43 p95 [1.23-6.36] | met |
| measure-only Publish on Brewline 1 (7.4M orders): <= 15 s, no source reloaded | 39.83 s, 4 reloaded | answers in 1.43 s, a Viewer's page shows it at 2.32 s, none reloaded [1.01-3.93] | met |
| kill -9 of the engine, then a worker: /api/ready and a page again <= 60 s | never (300 s watched each) | 7.1 s; 1.0 s | met |
| 8 chats (8 people) + 50M orders through the app inside mem_limit 4g, no swap: no OOM kill, exact | no kill, exact, peak 4,259,540,992 bytes | no kill, exact, peak 3,920,732,160 bytes (357 MiB to spare) [0.10-5.52] | met |

Pages: first opening p95 1.73 s without a rule and 27.52 s with it; 0 errors and 0 dropped in all four rounds; page
261 / 263 KB. Publish on Brewline 0.01: measure-only answers in 0.48 s (page 1.09 s), none reloaded; the first
Publish's copy tables arrive 27.27 s (0.01) and 82.38 s (7.4M orders) later, all four tables, the behaviour change
round 1 notes. The isolation load's lake holds 2,303,588,044 bytes (round 1's first run to the byte), container peak
4,194 MB with no limit. Kills: the supervisor logged "the engine (pid 63) ended with status 137 after 573 s; it starts
again in 0 s" and the same for the worker; `engine.sh health` said "The engine isn't ready" for 2 samples (about 4 s)
after the engine's kill and was healthy otherwise, Docker's check healthy at every probe, through both. 4 GiB:
orders in 329.64 s, Ready at 369.46 s; memory.events oom 0, oom_kill 0, max 0; OOMKilled false, 0 restarts; the 8
chats' Pi 805.3 MB at their start (112.6 MB a chat with the app's side), 667.5 MB after the 420 s hold.

Open, for the next fix: the heavy worker segfaults when a big project is deleted while the minute's build_copies
reads it. It died with status 139 twice here, each time inside build_copies, 6-13 s after perf.py deleted the 50M
project it had just loaded (build_copies started 23:23:00 and 23:48:00; delete_project answered 23:23:13 and
23:48:08; the kernel: "procrastinate[...]: segfault ... in _duckdb.cpython-313-x86_64-linux-gnu.so"), the same as
round 1's at 17:49. The supervisor started a new worker in 0 s and every run here ended right, but a heavy job beside
it would die with it (a load's run is then failed by retry_stalled_runs within about a minute, c2c7437).

## 1.2 M3.6 round 1 (pve1)

5 Nov 2026, 02:13-02:19 UTC: release-1.2 at b378753 (M3.6: shared partials for restricted reads, the per-person
caches, dimension keys) measured on pve1 with M3 round 3's method. `git archive b378753` built on LXC 100 as a
throwaway compose install (project, container and volume abi-m36, port 8798 on the LXC's loopback, the owner's
password in a root-only file; image abi-m36:b378753, 2,843,813,666 bytes), no memory limit as in M3's pages runs,
this branch's docker-compose.yml defaults (ABI_RLS_SHARED_ROWS 1,000,000). Removed afterwards; the live abi and lake
containers' start time, restart count and image were the same before and after. bench/perf.py of this commit ran
inside the container: `perf.py project perf_brewline --scale 0.01` (73,941 orders, its Overview report, a 261-264 KB
page), then each `pages` run, one at a time, each started at an LXC 1-minute load average of 0.68-1.38 with no build,
no other bench and no container but abi and lake beside it; [a-b] is the load average's range during the run, and
peak is the cgroup's memory.peak, reset at each run's start.

| M3.6 target (PLAN.md) | 1.1.0 (part 2) | M3 round 3 (c2c7437) | 1.2 M3.6 round 1 (b378753) | |
|---|---|---|---|---|
| 50 sessions right after a commit with `--rls cafes.cafe_id` (50 distinct restricted Viewers): page p95 <= 5 s | 152.7 s | 25.59 s | 9.14 s [0.59-2.14]; again last, 9.38 s [1.29-3.63] (2.8x M3, 16.7x 1.1.0) | missed |
| 50 sessions right after a commit, no rule: no worse than M3 | 87.6 s | 1.44 s | 1.30 s [1.12-1.77] | met |
| run 1's memory peak within M3's mem_limit 4g | | | 2,217,697,280 bytes (2.07 GiB; 2,345,668,608 in the repeat), no OOM event | met |

| pages, Brewline 0.01 | first p50 / p95 s | right after a commit p50 / p95 / max s | errors | peak bytes |
|---|---|---|---|---|
| `--rls cafes.cafe_id`, 10 Viewers | 2.33 / 2.53 | 2.07 / 2.26 / 2.26 | 0 | 2,330,951,680 |
| `--rls cafes.cafe_id`, 25 Viewers | 3.75 / 4.98 | 3.45 / 4.93 / 4.95 | 0 | 2,321,993,728 |
| `--rls cafes.cafe_id`, 50 Viewers | 7.15 / 11.67 | 5.62 / 9.14 / 9.21 | 0 | 2,217,697,280 |
| `--rls cafes.cafe_id`, 50 Viewers, repeated last | 6.19 / 9.83 | 5.88 / 9.38 / 9.41 | 0 | 2,345,668,608 |
| `--rls cafes.region` (4 values, people share), 50 Viewers | 3.04 / 3.12 | 2.04 / 2.13 / 2.13 | 0 | 2,330,439,680 |
| no rule, 50 people (Viewers and Developers) | 1.36 / 1.48 | 1.23 / 1.30 / 1.31 | 0 | 2,343,247,872 |

0 dropped in every run; the lake snapshot was the same at each round's start and end. Missed by 4.1-4.4 s at 50 café
Viewers. It grows with people at about 0.15-0.2 s per Viewer (10: 2.26 s, 25: 4.93 s, 50: 9.14 s), so 25 meets 5 s.
What the time is: the query log (the visuals' reads in these runs) shows nearly every person's visual read as an SE-7
hit, and a hit still took 138-216 ms on average (misses 208-337 ms), at 50 people at once in one engine; the shared
partials were built 5 times in all (89 ms on average), and the day chart's (72,961 groups for 73,941 orders) is over
half the fact's rows, so that visual takes each person's own read as designed. So at this scale DuckDB isn't what
costs: what's left is the engine's work around each read, 6 a person (auth, role, audit, query_log, the RLS context,
a catalog connection for the snapshot), which is what step 1's L4 removes (one engine read per person per page instead of one per
visual, and the snapshot read once per action), not built in b378753. Not measured here: a 7.4M-order project right
after a refresh, the cold morning case the shared partials are for.

## 1.2 M2 round 1 (MacBook Air, 8 cores, 24 GB; model proxy/claude-sonnet-5-5)

4 Nov 2026 08:57-09:13, release-1.2 at 7e68c71: M2.3's change measures (2c55e2d), M2.1's expressions (377a868), M2.2's
grounding (8fd7a9d) and the second planted "why". Method as the 1.1.0 baseline's: bench/bench.py at scale 0.01,
--model proxy/claude-sonnet-5-5 --fallback proxy/claude-opus-5-5, through a scratch wrapper that keeps every run's RPC
lines and transcript (it would ask again a run no model answered; none needed it). why ran 10 times on each planted
dataset: north, the baselines' data (`--plant north`: North's orders halve in December 2025, 82% of the chain's
-14.55%), and retail (`--plant retail`: every café's Retail orders halve that month, 93% of the chain's -19.6%, no
region over 31%); ask 10 and report 5 on north. Sonnet answered all 35 runs and no model call failed. Load average
2.2-6.9, nothing else building. The engine was the one started at 06:07 with M2.1's code (M2.2's change to the
engine's grounding.py isn't on this path: the harness checks Pi's answers with scripts/ground.py), and the harness's
files had the same sha256 at the start and the end.

| task | data | pass | runs masked | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ask | north | 10/10 | 0 | 6.2 | 2 | 2 | 0 | 0 | 4 | 47690 | 428 |
| why | north | 8/10 | 1 | 44.5 | 7 | 11.5 | 1 | 0 | 19089.5 | 195991 | 4053 |
| why | retail | 10/10 | 0 | 24.4 | 6 | 10 | 0 | 0 | 16245.5 | 160663.5 | 3004 |
| why | both | 18/20 | 1 | 34.1 | 6.5 | 10.5 | 0.5 | 0 | 17872 | 175717.5 | 3299 |
| report | north | 0/5 | 3 | 20.6 | 6 | 7 | 0 | 1 | 51777 | 164156 | 1677 |

Against the 1.1.0 quiet-Mac baseline (ask 1/10 with 9 runs masked, 9.4 s, 3 turns; why 9/10 with 1 masked, 33.9 s,
6.5 turns; report 5/5 as scored then, 3/5 as now, 20.5 s, 6 turns): ask passes every run, unmasked, in 6.2 s and 2
turns; why passes 18 of 20 in 34.1 s and 6.5 turns; report fails all 5 (below) in 20.6 s and 6 turns.

M2's gate is not met. Met: why 18 of 20, ask 10 of 10 with no run masked, and ask's and report's medians within 10% of
the baseline's (6.2 s against 9.4, 20.6 s against 20.5). Not met: one why run masked (north 8), why's median 34.1 s
(30 s or less) and 6.5 turns (4 or fewer). By the plan that calls for M2.4 (explain_change). Report's pass rate fell
from 3/5 to 0/5, which the gate doesn't count.

Where the time goes, from the RPC lines' arrival times: the model's turns take 97% of a why run (median 32.1 of
33.6 s) and ABI's part 1.1 s (tool calls 0.5 s, the harness's grounding check 0.6 s). Of the model's time, 16.7 s a
run is waiting for the first streamed token: 26.4 s in the north runs (09:00-09:06) and 10.1 s in the retail runs
(09:08-09:13), on the same code, most of the gap between their medians. One turn of north 4 waited 17.2 s for its
first token; one of retail 2 took 33.0 s to stream 278 tokens. A turn's model time is 5.0 s at the median, so the
time target turns on the turn count: 4 turns are about 20 s. What takes the turns in the 20 why runs:
- each run reads the months first (1-3 queries to find the latest complete month), then breaks the change down by
  2-5 dimensions and most drill into the top member: 10.5 tool calls in 6.5 turns. Every run broke it down by region and
  by category, some also by city, format, café or product. M2.1's expressions were used in 11 runs
  and M2.3's governed *_change measures in 11. The fastest run (retail 9, 3 turns, 21.0 s) asked the months with
  revenue_change, then the days, the categories and the regions with revenue_change at once, then answered;
- the planner's "add calendar.year (or .day)" for a change grouped by calendar.month without the year: 12 times in 8
  runs;
- the grounding retry, one more turn of 5-9 s: in 10 of 20 runs the first answer stated a figure no row holds, and the
  rewrite was clean in 9 of them. What it named: the model's own shares (82% and 93% of the fall, "about 1,685 of the
  2,051 drop", "about 2,574 of the 2,756"), differences (North's 1,685 and Retail's 2,574: the rows held both months,
  not the change), bounds and ranges ("by less than 190", "about 90 to 190 each", "150 or less"), counts ("three",
  "two", "all four regions") and a day no row has (north 8);
- a tool that doesn't exist, `mcp__abi__ship_abi_vault` (the harness's abi_vault under ABI's prefix), in 3 runs.

Every run that failed or was masked:
- why north 4 (55.3 s, 7 turns, unmasked): right in substance (both months' totals, North's 3,271.28 to 1,586.08 under
  "Where the drop sits", "The other regions moved much less", North's cities and weeks), but bench.why_ok's driver
  check finds no sentence naming North with a word like "most". The first answer had one ("The North region caused
  most of it."), and also "about 1,685 of the 2,051 drop" and "fell by about 90 to 190 each", its own difference and
  range; the rewrite after the grounding retry dropped those and the driver sentence with them.
- why north 8 (56.5 s, 9 turns, 1 masked): "there are no rows for [not verified] and 26 December": from the daily rows
  (24 December, then 27 to 31) the model named the two missing days. No row holds 25, so the first answer was flagged,
  the rewrite kept the sentence and the final answer has it masked. "26" passed only because rows held 26.10 and 26.15
  (November's discount, a North day's revenue): backed by chance. The rest is right: both months, North as the
  driver with its two months.
- report 0-4 (all fail): every run saved the build-report playbook's example nearly as it stands (visuals
  kpi-revenue, rev-region and rev-month on page "main", named "Overview"), whose line is on calendar.month alone: 12
  points that add 2023, 2024 and 2025 together, where bench.report_ok wants one point per month of every year (36). In
  1.1.0 every run called semantic_model before saving (the digest listed only calendar.day) and 3 of 5 grouped it by
  calendar.year and calendar.month; now the digest lists calendar.month (M2.2's fix 3e), no run calls semantic_model
  and none adds the year. Every first answer was also flagged for "three" (its count of the three visuals asked for)
  and "Overview" (the page name, in the agent's own save_report call and the playbook, in no ABI result until
  list_reports). After the retry, runs 0, 2 and 3 still counted them ("with [not verified] visuals", "All [not
  verified] use the revenue measure") and ended masked; runs 1 and 4 ended clean and fail on the line alone.

ask: all 10 clean, each region's 2025 figure after its name, in 2 turns (semantic_query and abi_vault, then the answer);
the query's own date range grounds now (1.1 masked its end date in 19 of 20). The two slow runs waited for the model's
first token: 21.0 s (run 6, 25.1 s in all) and 7.2 s (run 4, 11.9 s). No question changed the project: the 30 ask and
why runs called only semantic_query (182 times), abi_vault (27), data_quality (16), abi_show (1) and the missing tool
(3), and the bench found each passing run's project as it was. The runs' records: logs/bench-1104085745.jsonl (north)
and logs/bench-1104090801.jsonl (retail), on this Mac only.

## 1.2 M2 round 2 (MacBook Air, 8 cores, 24 GB; model proxy/claude-sonnet-5-5)

4 Nov 2026 10:00-10:09, release-1.2 at 8c5bdc1: round 1's code plus M2.4's explain_change. Method as round 1's:
bench/bench.py at scale 0.01, --model proxy/claude-sonnet-5-5 --fallback proxy/claude-opus-5-5, through the same
scratch wrapper (every run's RPC lines and transcript kept); why 10 times on each planted dataset (north, retail),
ask 10 and report 5 on north, both datasets in one hold of the bench lock. Sonnet answered all 35 runs, no model call
failed and no run was asked again. Load average 1.5-3.1, nothing else building. The engine was the one started at
09:41 on M2.4's code; HEAD, the engine's two processes and the sha256 of the 40 files the harness and the bench read
(index.ts, ground.py, SYSTEM.md, mcp.json, the skills and agents, abi.py, pi_driver.py, bench.py, brewline.py, the
engine's semantic modules) were the same at the start, between the datasets and at the end.

| task | data | pass | runs masked | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ask | north | 10/10 | 0 | 6.8 | 2 | 2 | 0 | 0 | 4 | 50512 | 436.5 |
| why | north | 9/10 | 1 | 15.8 | 3 | 1 | 0 | 0 | 1881 | 78076.5 | 1373 |
| why | retail | 7/10 | 3 | 14.2 | 3 | 1 | 0 | 0 | 1885.5 | 85448 | 1376.5 |
| why | both | 16/20 | 4 | 15.1 | 3 | 1 | 0 | 0 | 1883 | 84865.5 | 1376.5 |
| report | north | 0/5 | 0 | 23.2 | 6 | 8 | 0 | 0 | 52518 | 172255 | 1780 |

Against round 1 (why 18/20 with 1 run masked, 34.1 s, 6.5 turns, 10.5 tool calls; ask 10/10, 6.2 s; report 0/5 with 3
masked, 20.6 s) and the 1.1.0 quiet-Mac baseline (ask 1/10, 9.4 s, 3 turns; why 9/10, 33.9 s, 6.5 turns; report
20.5 s, 6 turns): why takes under half the time, turns and tokens (15.1 s in 3 turns and one tool call) but passes 16 of
20 with 4 runs masked; ask is as in round 1; report fails all 5 again, now with none masked.

M2's gate is not met. Met: why's median 15.1 s (30 s or less) and 3 turns (4 or fewer), ask 10 of 10 with no run
masked, ask's median 6.8 s against the baseline's 9.4. Not met: why passes 16 of 20 (18 or more) with 4 runs masked
(none allowed), and report's median, 23.2 s, is 13% over the baseline's 20.5 s (10% allowed), in the same 6 turns and
8 tool calls: its 5 runs (11.1, 14.2, 23.2, 26.1 and 27.3 s) waited 15.6 s a run for the model's first tokens, round 1's
11.8 s.

Where the time goes, from the RPC lines' arrival times (both datasets): a why run is 14.8 s at the median, 94% of it
the model's turns (6.2 s of them waiting for first tokens), 0.5 s the harness's grounding check and 0.4 s the tool calls
(explain_change answered in 171-233 ms). Every why run called explain_change once with its defaults (measure revenue,
nothing else) and 3 also called abi_vault; none needed semantic_query, data_quality or semantic_model, none met the
planner's "add calendar.year", and none called a tool that doesn't exist (round 1: 10.5 calls, 2-5 breakdowns, 12
"add calendar.year", a missing tool in 3 runs). Turn 1 asks (2.9 s), turn 2 answers (5.7 s). In 18 of 20 runs the
grounding check flagged that answer and the hidden retry took a third turn (5.3 s, 4.4-7.5). What it named: a count of
members in 17 ("three" cities, formats or payment methods, "two" retail products: counts never ground, D-22), a range's
lower bound written without its % sign in 3 ("Each fell 44–61%", "each down about 54–55%": the rows hold -0.4395 and
-0.6058, and a bare 44 is no row value), and "Within" opening the text after a bold label in 2 ("**Cities:** Within
North, ...", "**City:** Within the cities listed ..."). 14 of the 18 rewrites were clean.

Every run that failed or was masked:
- why north 6 (17.0 s, 3 turns, 1 masked): the first answer's "Each fell 44–61%" was flagged; the rewrite dropped it and
  wrote "All three formats declined", a count of the three formats the rows list, masked in the final answer ("All [not
  verified] formats declined"). The rest is right: both months, North where the drop sits with 3,271.28 and 1,586.08.
- why retail 6 (13.3 s, 3 turns, 1 masked): the first answer's "mostly two retail products ... each down about 54–55%"
  was flagged ("two", "54"); the rewrite wrote "all three methods fell" of the payment methods, masked. Otherwise right:
  both months, Retail from 4,719.60 to 2,145.70, 93% of the change.
- why retail 8 (19.9 s, 3 turns, 1 masked): the first answer said "Card holds the largest share of the drop, 72%, but
  all three fell"; the retry named "three", and the rewrite kept the count ("**Payment method:** all three fell."),
  masked.
- why retail 9 (13.1 s, 3 turns, 1 masked; also fails the driver check): the first answer's "The two Retail products"
  was flagged; the rewrite opened "**Where the drop sits:** Almost all of it is in Retail." scripts/ground.py reads a
  capitalized word after a label's colon as a name unless FUNCTION or STATUS lists it (_caps), and "Almost" isn't
  listed, so it was masked as a name no row holds: a false mask, the same rule that flagged "Within" above. It was the
  answer's one driver phrase, so bench.why_ok's driver check fails too ("That is 93% of the total change" has no driver
  word).
- report 0-4 (all fail, none masked): every run saved the build-report playbook's example as it stands (kpi-revenue,
  rev-region and rev-month on page "main", named "Overview"), its line on calendar.month alone: 12 points adding 2023,
  2024 and 2025 together, where bench.report_ok wants 36. The KPI (425,548.61) and the bar by region were right to the
  cent in the 4 runs that read their visuals back (run 2 read none; its queries were the same). 4 first answers were
  flagged for "three" (the visuals) or "Overview", as in round 1, and all 4 rewrites were clean (round 1: 3 stayed
  masked).

So the why gate now turns on counting: 3 of the 4 masked runs are a rewrite counting the members explain_change listed,
after a retry that says "name members instead of counting them", and the fourth is ground.py's closed word list. ask:
all 10 clean, each region's 2025 figure after its name, in 2 turns (semantic_query and abi_vault, then the answer). No
question changed the project: the 30 ask and why runs called only semantic_query (10 times), explain_change (20) and
abi_vault (13), and the bench found each passing run's project as it was. The runs' records:
logs/bench-1104100043.jsonl (north) and logs/bench-1104100635.jsonl (retail), on this Mac only.

## 1.2 M2 round 3 (MacBook Air, 8 cores, 24 GB; model proxy/claude-sonnet-5-5)

4 Nov 2026 11:41-11:50, release-1.2 at ffc2648: round 2's code plus round 3's fixes (a range's lower bound and a
label's opening word ground as written; SYSTEM.md, the hidden retry and explain_change's description say to write no
number in words and count nothing the rows list; explain_change's `partial`; the build-report playbook's line on
calendar.year and calendar.month; save_report returns what it saved; abi_show says in a terminal that it shows
nothing). Method as rounds 1-2: bench/bench.py at scale 0.01, --model proxy/claude-sonnet-5-5 --fallback
proxy/claude-opus-5-5, through the same scratch wrapper (every run's RPC lines and transcript kept); why 10 times on
each planted dataset (north, retail), ask 10 and report 5 on north, both datasets in one hold of the bench lock. Sonnet
answered all 35 runs, no model call failed and no run was asked again. Load average 2.4-6.2 over the runs (9.6 over the
15 minutes before the start). The engine was the one started at 11:33 on round 3's code; HEAD, the engine's two
processes and the sha256 of the 43 files the harness and the bench read (round 2's 40, plus the engine's reports
module, grounding.py and app.py) were the same at the start, between the datasets and at the end.

| task | data | pass | runs masked | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ask | north | 10/10 | 0 | 7.4 | 2 | 2 | 0 | 0 | 310.5 | 50638 | 443.5 |
| why | north | 10/10 | 0 | 12.9 | 3 | 1 | 0 | 0 | 1946.5 | 72528 | 1216 |
| why | retail | 9/10 | 1 | 13.8 | 3 | 1 | 0 | 0 | 1909 | 72443.5 | 1328.5 |
| why | both | 19/20 | 1 | 13.4 | 3 | 1 | 0 | 0 | 1909 | 72528 | 1218 |
| report | north | 4/5 | 1 | 23.3 | 5 | 5 | 0 | 0 | 34018 | 134167 | 1211 |

Against round 2 (why 16/20 with 4 runs masked, 15.1 s, 3 turns; ask 10/10, 6.8 s; report 0/5, 23.2 s, 6 turns, 8 tool
calls) and the 1.1.0 quiet-Mac baseline (ask 1/10, 9.4 s, 3 turns; why 9/10, 33.9 s, 6.5 turns; report 20.5 s, 6 turns,
8 tool calls): why passes 19 of 20 with 1 run masked, in 13.4 s and 3 turns; ask passes every run unmasked in 7.4 s;
report passes 4 of 5 (round 2: none) with 1 masked, in 23.3 s, 5 turns and 5 tool calls.

M2's gate is not met, by one masked why run and report's time. Met: why 19 of 20 (18 or more), its median 13.4 s (30 s
or less) and 3 turns (4 or fewer), ask 10 of 10 with no run masked, ask's median 7.4 s against the baseline's 9.4. Not
met: why retail 3 is masked (none allowed), and report's median, 23.3 s, is 13.7% over the baseline's 20.5 s (10%
allowed: 22.55 s), in fewer turns and tool calls than the baseline's (5 and 5 against 6 and 8). Its runs took 16.0,
17.0, 23.3, 25.2 and 28.7 s (round 2: 11.1-27.3 s). From their RPC lines, 22.1 s of a run's 23.0 s at the median is the
model's turns, 15.4 s of that the wait for first streamed tokens (round 2: 15.6 s), against 0.9 s in ABI and the
harness (tool calls 0.5 s, the grounding check 0.4 s).

Where a why run's time goes (both datasets, from the RPC lines): 13.1 s at the median, 12.4 s of it the model's turns
(7.8 s waiting for first tokens), 0.5 s the grounding check and 0.4 s the tool call. Every why run called explain_change
once with its defaults (measure revenue, nothing else; 169-239 ms) and no other tool (round 2: 3 also called
abi_vault). Turn 1 calls it (2.3 s), turn 2 answers (6.2 s). 8 of 20 first answers passed the grounding check, so the
run ended in 2 turns (round 2: 2 of 20). The other 12, 6 on each dataset, were flagged, each for a count of the members
explain_change listed: the city breakdown's "three" cities in the 6 north runs ("These three cities hold 29.0%, 27.1%
and 26.0% of the total change", "all three of its cities"); "two" Retail products, "the two months" or "all three"
payment methods in the retail runs, and once a name no row holds ("a Black Friday-type lift", retail 0). The hidden
retry took a third turn (6.0 s) and 11 of the 12 rewrites were clean (round 2: 14 of 18). None of round 2's other flags
came back (a range's bare lower bound, "Within" or "Almost" after a bold label).

Every run that failed or was masked:
- why retail 3 (16.8 s, 3 turns, 1 masked): right in substance: both months (14,059.04 in 2025-11, 11,303.05 in
  2025-12), "Most of the drop sits in the Retail category", Retail from 4,719.60 to 2,145.70 and 93.4% of the total
  change, then its products, payment types and customers. The first answer was flagged for "two" ("I haven't checked
  why those two products fell", "across the two months"); the rewrite dropped both and wrote a new count in its closing
  summary, masked in the final answer: "The decline comes mainly from [not verified] Retail products, Reusable Cup and
  House Beans 250g" (it wrote "two").
- report 0 (23.3 s, 5 turns, 1 masked): the report is right: its save_report call was the passing runs' but for the ID
  (kpi-revenue, rev-region on cafes.region and rev-month on calendar.year and calendar.month, on page "main" named
  "Overview"), and the answer gave the KPI (425,548.61) and every region's revenue to the cent. The first answer counted
  the visuals ("with three visuals", "All three use the existing `revenue` measure"); the retry named "three" and the
  rewrite kept both ("Its Overview page holds [not verified] visuals", "All [not verified] use ..."), so the run fails
  as not clean before report_ok is checked. Report 1's and report 2's first answers counted them too ("three visuals",
  "the two charts"); both rewrites were clean.

One pass was backed by chance: report 3's answer says "It has one page, Overview, with three visuals" and wasn't
flagged. The harness grounds a harness tool's JSON output as one text cell whose embedded digits count (index.ts
ingest, ground.py), and that run's abi_sync output named the commit a3c24de: its 3 backed "three". With the other 4
runs' commits (38bb765, 10aa5ee, 33ca45a, fa0357e) the same sentence is flagged. It is the only number word left in
the 35 final answers.

So both misses and the chance pass are counts of what the rows list, which the model still writes in 12 of 20 why first
answers and 3 of 5 report ones, and writes again after the retry in 1 of 12 why rewrites and 1 of 3 report ones. ask:
all 10 clean at the first answer, each region's 2025 figure after its name, in 2 turns (semantic_query and abi_vault,
then the answer). No question changed the project: the 30 ask and why runs called only semantic_query (10 times),
abi_vault (10) and explain_change (20), and the bench found each passing run's project as it was (the one failed
question, why retail 3, called explain_change alone). The report runs called abi_tools, save_report and abi_sync 5
times each, visual_data 5 (2 in report 0, none in report 3), abi_vault 3 and abi_show 2 (each told that nothing was
shown; both answers were in words). The runs' records: logs/bench-1104114136.jsonl (north) and
logs/bench-1104114703.jsonl (retail), on this Mac only.

Read again after the M2 review (4 Nov):
- In sample. Round 3's fixes were tuned over 90 live why runs of this same prompt, seed (42), scale and both plants (9
  scratch batches of 10 while building ffc2648), and ground.py against the 313 recorded drafts of rounds 1-3; the
  measured round then asked the same question of the same data, and all 20 why runs called explain_change with its
  defaults alone. So 19 of 20 shows that this one question works after tuning, not that "why" does. bench/bench.py now
  takes --seed and --wording (a second why question) to judge a fix on data and words it wasn't tuned on.
- False statements in passing answers. The grounding checks figures and names, not claims, and why_ok checked the
  driver, not the rest, so these passed: why retail 4 and 5 (and round 2's retail 2) say Bakery "is the only category
  that rose", but Food (1,286.12 to 1,374.60) and Tea (1,411.10 to 1,486.22) rose too, in rows explain_change didn't
  show (`partial`); why north 0 and 9 say "Every format fell" and north 6 "each format fell" of the partial
  cafes.format, true only by chance (campus, not shown, fell from 2,052.50 to 1,728.69); why retail 2 says "The data has
  no price, stock or promotion detail to test that", but orders.unit_price and orders.discount are in the model; why
  north 6 and 8 conclude that the drop "shows mostly in card payments", noise (bench/brewline.py draws the payment per
  order, whatever the region); report 1 says "The change is uncommitted" and report 4 "Changes are not committed yet",
  false (auto-commit is on; abi_sync named the folder's own skeleton commit, not ABI's). The review's fixes: an
  "(others)" row per partial breakdown, only the dimensions where the change sits most, abi_sync naming ABI's commit.
- How the gate was read. PLAN.md: 'Target: "why" ≥ 18/20 runs pass (10 per planted dataset) with zero [not verified]
  and the right top member, median ≤ 30 s, ≤ 4 turns; ask passes ≥ 9/10 with zero masks; ask and report medians no
  worse than baseline.' Rounds 1-3 here read "with zero [not verified]" as no masked run at all, stricter than its words,
  which make it a condition of each passing run (a masked run fails, so round 3's 19 of 20 meets it); and they read "no
  worse than baseline" with a 10% allowance for noise that PLAN.md doesn't give (round 1's report, 20.6 s against 20.5,
  was "Met" only by it). Under PLAN.md's words round 3 meets why and ask and misses report's 23.3 s against 20.5 s. Neither
  reading separates the provider's latency from the harness: the baseline ran 11 hours earlier, and the wait for first
  tokens moved from 26.4 s to 10.1 s within 10 minutes on the same code (round 1). Deciding report's time needs 1.1.0
  (a 309ee47 worktree) and release-1.2 report runs interleaved in one window, at least 10 each: not done.

## 1.2 M2 after review (MacBook Air, 8 cores, 24 GB; model proxy/claude-sonnet-5-5)

4 Nov 2026 13:20-13:23, release-1.2 at d2ffd0d with the M2 review's fixes (committed with this section). What the agent
sees changed: semantic_query's inline expressions take fixed shapes (a measure under time functions and IGNORE, two of
those subtracted, or DIVIDE of those; no number, no WHERE, at most 4 distinct periods or IGNOREs); explain_change ends a
partial breakdown with an "(others)" row, shows only the dimensions scoring at least a third of the leader's (region
and city here; category and product on retail), gives shares only for sums and counts, and counts a period complete
once it is over and the data reaches into its last days; SYSTEM.md asks for explain_change on the row count too (for a
sum), the person's words, and explain_change (not *_change) to rank members; the grounding reads no expression's MEL,
no figure from what echoes the model's own input (sql_query, abi_prefs and the saved-item tools), percentage points
as percents, and "MoM", "Christmas Eve", "Boxing Day", "between 44 and 61%" as ordinary words. A check, not a gate
(13 runs): the same scratch wrapper as round 3; why 5 and ask 3 on north at seed 42 with round 3's question, then why
5 on retail at seed 7 with the second wording ("what drove the change in revenue in the last full month of data,
against the month before?"), data and words no round was tuned on. why_ok is now the review's stricter one (the
member's share after its name, no other member named as the driver), and "orders stated" counts the answers that give
the planted member's orders in both months (not in the pass). Sonnet answered all 13 runs. Load average 34.5-76.1 from
other work on this Mac (round 3: 2.4-6.2), so the times aren't comparable with earlier rounds'; HEAD, the engine and the
43 files the harness and the bench read were the same at the start, between the datasets and at the end.

| task | data | pass | runs masked | median s | turns | tools | tool errors | masked | input tok | cached tok | output tok | orders stated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ask | north, seed 42 | 3/3 | 0 | 9.5 | 2 | 2 | 1 | 0 | 4 | 51610 | 458 | - |
| why | north, seed 42 | 4/5 | 1 | 15.4 | 2 | 2 | 0 | 0 | 14484 | 58273 | 777 | 5/5 |
| why | retail, seed 7, second wording | 4/5 | 1 | 10.4 | 2 | 2 | 0 | 0 | 1903 | 53677 | 715 | 5/5 |
| why | both | 8/10 | 2 | 13.9 | 2 | 2 | 0 | 0 | 7049 | 55975 | 768.5 | 10/10 |

Every why run called explain_change twice in its first step, for revenue and for orders_rows, and nothing else; 6 of
10 first answers passed the grounding check (2 turns; round 3: 8 of 20). The other 4 were flagged only for counts of
what the rows listed ("three" cities, "two" Retail products), as in round 3; 2 rewrites were clean and 2 kept the count,
masked: why north 1 ("[not verified] cities lost the most") and retail 2 ("[not verified] Retail products hold most of
it"). Both are right in substance. Every answer now says what moved it, in orders (north 0: "Its orders fell from 576
to 280, so the cause is volume, not smaller orders"; retail 3: "Retail orders went from 313 to 130, so fewer sales
explain it, not smaller ones"), and none says "the only", "every" or "all" of a partial breakdown's members; they
quote its "(others)" row ("The other cities together went from 10,823.76 to 10,457.56"). No answer singles out a
payment type any more (4 name it among the breakdowns checked). Still wrong in words:
retail 2 lists Banana Bread, a product, among "Other categories"; 2 answers still say "rows" ("Order rows dropped",
"The rows show where the drop sits"). ask: 3 of 3 clean in 2 turns; each called a tool that doesn't exist
(mcp__abi__ship_abi_vault, with abi_vault's arguments: a model slip round 1's logs show too) and semantic_query. No
question changed the project (the bench found each passing run's project as it was). The runs' records:
logs/bench-1104132002.jsonl and logs/bench-1104132213.jsonl, on this Mac only.

Against PLAN.md's gate as worded (above) these 10 why runs are 8 passes, short of 18 in 20 at this rate, both misses the
count words D-22 masks; 5 runs a dataset can't decide it either way.

## 1.2 M3.3 pages (MacBook Air, 8 cores, 24 GB)

4 Nov 2026 14:03-15:21, release-1.2 after 56df773 with M3.3 uncommitted beside the other M3 items' work in the same
tree (M3.1's per-project locks, committed at 14:51 as 04e84c0; M3.2's and M3.4's not yet), on this machine's engine
restarted with it (14:03, and at 15:14 for the last step). `perf.py project perf_brewline`: Brewline 0.01, 73,941 orders, its Overview
report of six visuals; `perf.py pages --sessions 20` (20 people: Viewers and Editors), then `--rls cafes.cafe_id` (20
Viewers with a café each), 3 runs each, through an app started here (signed in) on port 8797. Every round's lake
snapshot was the same at its start and end (9), no error and no dropped connection. The Mac was shared with other
agents' builds and test suites the whole time: the 1-minute load average at each run's start was 15-128 (M0's: 1.7-2.6),
so the 1.1.0 rows (a quiet Mac) aren't comparable as times; HEAD's app (the 1.1 page cache: one global lock, one
computation per person, every ABI call one after another) ran on the same engine and load as a comparison.

| 20 sessions right after a commit, p95 s (3 runs) | no rule | `--rls cafes.cafe_id` |
|---|---|---|
| 1.1.0 (M0, quiet Mac) | 9.4 / 9.9 / 10.1 | 14.9 / 15.9 / 16.8 |
| HEAD's app, this engine (load 15-19; with the rule 29-64) | 40.5 / 37.9 / 55.5 | 91.2 / 125.4 / 92.8 |
| M3.3 first cut: shared entries, single flight, 6 calls at once, docs in the page (load 15-25; 16-31) | 2.23 / 2.14 / 2.63 | 42.6 / 39.0 / 38.8 |
| + docs asked by its page, restricted people share the rest (load 26-43; 39-57) | 0.87 / 0.95 / 0.96 | 38.8 / 39.4 / 42.8 |
| + one row-level security build per person (this commit; load 121-128; 121-126) | 0.90 / 0.82 / 1.79 | 11.5 / 12.9 / 11.9 |

The first round of each run (the people's first pages, after the owner's) was the same within 0.2 s, but for one first
cut run at 5.6 s and HEAD's rule runs (126-368 s). What each step took off: the 11 lists and six visuals of a page
computation came to 0.29-0.39 s once asked 6 at once, but `docs` alone took 1.77 s (a worked example is a query per
measure), so the page no longer carries it (the Docs page asks when shown; the page went from 375 to 268 KB). With
the rule, 20 people still needed 20 computations: everything but the report rows is now one computation for all
restricted people (none of it depends on a person's values), and each person's own part is their rows and rules; and a
person's six visuals asked at once each built that person's row-level security views (rls.context): six cold
visuals for one restricted Viewer took 1.96 s at once against 1.10 s one after another, and 0.68 s now that their
reads wait for one build (1.53 s one after another at that load). Engine: Postgres connections pooled (20), a
connection out of the pool in 0.23 ms against 3 ms for a new one (200 in a loop). PLAN.md's page target is for pve1
at 50 sessions (M3's measure step).
