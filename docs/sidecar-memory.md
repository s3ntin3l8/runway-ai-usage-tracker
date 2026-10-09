# Sidecar memory measurements

The sidecar retains Pydantic validation, provider support, event payloads, polling
intervals, account attribution, and delivery/watermark behavior. The optimization
releases event models during serialization, prepares delivery batches lazily,
iterates SQLite cursors, bounds diagnostic log buffers, and streams offline replay
through an atomic queue rewrite. Desktop components now share one sidecar module.

## Linux results

Measured on 2026-10-09 against `510eb785`, using Python 3.12.3, Pydantic 2.14.0,
and PyInstaller 6.22.3. Values are medians of three runs. Both revisions used the
same interpreter/dependencies. Synthetic histories and an isolated configuration
were used; the installed sidecar and its data were left running unchanged.

The packaged CLI collected 10,000 Claude events into a local mock server, then
completed three empty heartbeats before the idle measurement. Each run had a
fresh home/config/temp directory, disabled keep-alive and automatic installation,
and a local proxy rejecting external HTTPS connections. All 10,000 event payloads
matched across revisions; there were no collection errors.

| Packaged Linux CLI metric | Before (MiB) | After (MiB) |
|---|---:|---:|
| Warmed idle RSS after backfill | 88.3 | 82.1 |
| Process peak RSS | 88.3 | 83.4 |
| Warmed idle PSS | 85.9 | 79.8 |
| Launcher RSS, reported separately | 2.18 | 2.17 |

Median time from launch to final event ingest was 0.892 seconds before and 0.913
seconds after. This small workload does not establish a universal memory ceiling;
large individual log records, history sizes, native libraries, and allocator
retention still affect the footprint.

The source benchmark covers additional workloads without allocation tracing:

| Source workload | Before peak RSS (MiB) | After peak RSS (MiB) |
|---|---:|---:|
| 10,000-event extraction and delivery | 64.4 | 56.2 |
| Twenty 1,000-event collection cycles | 41.9 | 41.1 |
| Twenty tails of a roughly 5 MiB log | 36.7 | 32.0 |
| Replay of roughly 9 MiB of failed queue entries | 48.2 | 32.0 |
| 10,000 SQLite messages with 2 KiB source content | 80.7 | 62.8 |

Payload/log/retained-queue checksums matched for every source workload. Native
workload times were comparable or improved. Startup memory was essentially
unchanged; desktop imports loaded one collector module instead of two.

## Reproduce the source comparison

From a checkout containing the optimization and its dependencies:

```bash
.venv/bin/python scripts/benchmark_sidecar_memory.py --baseline-ref 510eb785 --runs 3
.venv/bin/python scripts/benchmark_sidecar_memory.py --baseline-ref 510eb785 --runs 3 --trace-python
```

The tool exports the baseline's source into a temporary directory and alternates
baseline/current child processes. It does not use real provider files, credentials,
network requests, or sidecar state. `--case` selects one workload. Windows can use
`python` instead of the Unix venv path.

Default output reports native RSS/PSS on Linux, or working set/peak working set
and committed private bytes on Windows. `--trace-python` adds Python allocation
measurements and increases process memory, so its RSS numbers must not be mixed
with the untraced results above. Both source modes include benchmark-tool imports;
they are comparative measurements, not the packaged app's baseline.

Windows tray measurements and platform-specific smoke testing are tracked in
[#597](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/597). Linux RSS,
Windows working set, and Task Manager's memory column should not be treated as
interchangeable. The reported Windows observation of roughly 65 MB remains a
separate baseline until native builds are compared.
