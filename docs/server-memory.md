# Server memory

Runway uses one server process. Usage events are authoritative; reducing RAM
does not require deleting history or reducing collection frequency.

Window closing aggregates numeric columns in SQLite and loads only the grouped
results. Data Health's unpriced-event scan reads 1,000 rows at a time. Recost
reads only pricing inputs in 1,000-row pages, ordered by timestamp and ID, and
writes changed costs in batches. Pagination continues across commits without
skipping events whose cost changes, and timestamp index bounds avoid rescanning
previous pages. The event limit is captured when the scan begins.

## Deployment controls

`SQLITE_TEMP_STORE` accepts `MEMORY` (the default) or `FILE`. It applies to every
new SQLite connection and takes effect on restart. `FILE` allows large temporary
query results to spill to disk; it can lower peak RAM but may slow queries. Page
cache size, mmap size, WAL, and transaction behavior are unchanged.

The Docker image defaults to `MALLOC_ARENA_MAX=4`, limiting glibc allocator arenas
to reduce memory retained across worker threads. Compose environment variables
can override it. Native installations keep their allocator defaults unless this
variable is set before starting Python. Changing it requires a container
recreation or process restart. Test overrides against your workload: tighter
limits can increase allocator contention. A Docker memory limit caps usage but
does not make queries allocate less memory.

## Reproduce measurements

```bash
.venv/bin/python scripts/benchmark_server_memory.py --baseline-ref <commit> --matrix
```

Requires Python 3.12 or newer, matching the project's virtual environment.
The benchmark archives the baseline application code and runs both versions
with the same dependencies. Every sample uses a fresh process and a private copy
of a synthetic SQLite fixture; production data, credentials, and network
collectors are not used. The default fixture has 100,000 events, 100,000 quota
snapshots, four models, five sidecar identities, and representative raw payloads.
Fixture creation and imports are outside measured operations.

Cases cover window closing, classification, recost preview/apply, cumulative
usage and forecast queries, ingestion, and repeated concurrent reads. Output
digests must match. Recost apply measures event updates separately from derived
view rebuilding, which window-closing and existing integration tests cover.

`--events`, `--snapshots`, `--runs`, and `--rounds` control scale and repetition.
`--case` selects one or more cases. `--trace-python` measures Python allocations
with tracemalloc; its timings should not be compared with untraced runs.

Reports include peak RSS, final RSS/PSS, private anonymous memory, elapsed time,
and the median per-process p95 operation latency. With the default three rounds,
p95 is the slowest operation in each process: this is a coarse benchmark,
not a production latency percentile. Use more rounds/runs for tuning decisions.
Anonymous memory distinguishes retained allocations from resident database
mappings. Compare settings with the optimized application's default settings,
and reject tuning that regresses measured latency by more than 10%.

For rollout, record the image revision, database row counts, initial RAM and
peak RAM. After one controlled restart, observe normal ingestion and dashboard
usage for 24 hours, recording RAM, query latency, and errors. Compare equivalent
workloads rather than an idle freshly restarted process with a busy older one.

## Measurements, 2026-10-12

Baseline: `e44385938eb6a0809cbc89e652311e4560133af8`. Candidate: the SQL aggregation,
bounded scans and bulk updates in this change, using the shipped Docker profile
(`MALLOC_ARENA_MAX=4`, `SQLITE_TEMP_STORE=MEMORY`). All workload output digests match.

The table reports median peak RSS across three fresh processes per profile.
Individual cases use three rounds per process; concurrent reads use ten rounds.

| Workload | Baseline peak RSS | Candidate peak RSS | Baseline p95 | Candidate p95 |
| --- | ---: | ---: | ---: | ---: |
| Window closing | 923 MiB | 205 MiB | 5.132 s | 0.164 s |
| Unpriced classification | 269 MiB | 197 MiB | 0.910 s | 0.711 s |
| Recost preview | 637 MiB | 198 MiB | 3.528 s | 1.218 s |
| Recost apply | 940 MiB | 220 MiB | 59.279 s | 3.193 s |
| Dashboard reads | 250 MiB | 250 MiB | 0.845 s | 0.803 s |
| Ingestion | 75 MiB | 75 MiB | 0.894 s | 0.789 s |
| Concurrent reads/window closes | 1,614 MiB | 952 MiB | 5.642 s | 6.025 s |

The concurrent workload's retained anonymous memory falls from **545 MiB to
122 MiB**, and PSS from **723 MiB to 295 MiB**. RSS includes repeated resident
database mappings, so it is larger than private memory. These measurements are
not a prediction of the deployed service's steady-state RSS.

The arena-4 profile stays within 10% of the optimized allocator default's latency
in every case. Arena-2 fails that gate for classification in the shorter matrix;
FILE does not consistently reduce memory in the extended workload. Retain
MEMORY and use arena-4 as the Docker default. Native behavior stays opt-in.

The uncapped optimized profile in the shorter matrix measured dashboard and
ingest p95 at 0.969 s and 1.036 s: 14.7% and 15.9% slower than baseline, despite
unchanged query/ingestion paths. The table above reports the selected arena-4
profile, not these uncapped results. The coarse samples do not establish a
production latency change; native users and the deployed Docker service still
need the equivalent-workload comparison in #618.

Separately, tracing a single 100,000-event window close shows peak Python
allocations dropping from **425 MiB to 2 MiB**. Traced timings are not used for
tuning. Raw results are in [server-memory-results.json](server-memory-results.json).
The 24-hour deployed-service comparison is tracked in
[#618](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/618).
