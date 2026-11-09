# Memory

Runway is a single server process plus one sidecar per host. Usage events are authoritative, so keeping RAM low never requires deleting history or collecting less often.

## Server

The heavy operations are bounded by design. Window closing aggregates numeric columns in SQLite and loads only the grouped results. The Data Health unpriced-event scan and recost read 1,000 rows at a time (recost reads only pricing inputs and writes changed costs in batches), so peak memory does not grow with history size.

Two knobs are left to the operator:

| Variable | Default | Effect |
|---|---|---|
| `SQLITE_TEMP_STORE` | `MEMORY` | `MEMORY` or `FILE`, applied to every new SQLite connection on restart. `FILE` lets large temporary query results spill to disk, which can lower peak RAM but may slow queries. Page cache, mmap, WAL and transactions are unchanged. |
| `MALLOC_ARENA_MAX` | `4` in the Docker image; unset natively | Limits glibc allocator arenas to reduce memory retained across worker threads. Override it in the Compose environment, or export it before starting Python natively. Tighter limits can increase allocator contention. |

Both are documented in [`.env.example`](../.env.example). Test an override against your own workload and keep it only if latency does not regress noticeably. A Docker memory limit caps usage but does not make queries allocate less.

### Benchmark

```bash
.venv/bin/python scripts/benchmark_server_memory.py --baseline-ref <commit> --matrix
```

The benchmark archives the baseline code and runs both versions with the same dependencies. Every sample uses a fresh process and a private copy of a synthetic SQLite fixture (default 100,000 events and 100,000 quota snapshots); no production data, credentials or collectors are involved. Cases cover window closing, classification, recost preview and apply, cumulative-usage and forecast queries, ingestion, and concurrent reads, and the output digests of both versions must match.

- `--events`, `--snapshots`, `--runs` and `--rounds` set scale and repetition; `--case` (repeatable) selects cases.
- `--trace-python` adds tracemalloc allocation figures. Its timings must not be compared with untraced runs.
- Reports give peak RSS, final RSS/PSS, private anonymous memory, elapsed time and the median per-process p95 latency. With the default three rounds the p95 is a coarse figure, not a production percentile; use more rounds or runs for tuning decisions.

Raw results from the acceptance run are in [`evidence/acceptance/server-memory-results.json`](evidence/acceptance/server-memory-results.json).

### Measuring a deployment

Record the image revision, row counts, initial RAM and peak RAM. After one controlled restart, watch normal ingestion and dashboard use for 24 hours, recording RAM, query latency and errors. Compare equivalent workloads, not an idle fresh process against a busy older one.

## Sidecar

The sidecar releases event models during serialization, prepares delivery batches lazily, iterates SQLite cursors, bounds diagnostic log buffers, and streams offline replay through an atomic queue rewrite. There are no operator knobs.

```bash
.venv/bin/python scripts/benchmark_sidecar_memory.py --baseline-ref <commit> --runs 3
.venv/bin/python scripts/benchmark_sidecar_memory.py --baseline-ref <commit> --runs 3 --trace-python
```

The tool exports the baseline's source to a temporary directory and alternates baseline and current child processes. It uses no real provider files, credentials, network or sidecar state. `--case` selects one workload. On Windows use `python` instead of the venv path.

Output reports RSS/PSS on Linux, or working set, peak working set and committed private bytes on Windows; these are not interchangeable with each other or with Task Manager's memory column. `--trace-python` raises process memory, so do not mix its RSS with untraced runs. Both modes include the benchmark's own imports: they compare revisions and are not the packaged app's baseline.
