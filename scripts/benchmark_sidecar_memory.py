#!/usr/bin/env python3
"""Compare sidecar memory against a git revision using isolated synthetic workloads.

Run: .venv/bin/python scripts/benchmark_sidecar_memory.py --baseline-ref <base-sha>
No provider files, credentials, network requests, or live sidecar state are used.
Both revisions use the same interpreter and dependencies. Add --trace-python for
Python allocation peaks (this increases RSS). Measure packaged processes separately
for deployment sizing.
"""

import argparse
import ctypes
import hashlib
import io
import json
import logging
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

_CASES = (
    "startup",
    "desktop_import",
    "backfill",
    "repeated",
    "log_tail",
    "offline_queue",
    "sqlite",
)
_ROOT = Path(__file__).resolve().parent.parent


def _process_memory() -> dict[str, float | None]:
    """Report native memory metrics without adding a production dependency."""
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return {
            "working_set_mib": counters.WorkingSetSize / 2**20,
            "peak_working_set_mib": counters.PeakWorkingSetSize / 2**20,
            "private_bytes_mib": counters.PrivateUsage / 2**20,
        }
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result: dict[str, float | None] = {
        "peak_rss_mib": peak / (2**20 if sys.platform == "darwin" else 1024)
    }
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            if line.startswith(("Rss:", "Pss:")):
                result[line.split(":")[0].lower() + "_mib"] = int(line.split()[1]) / 1024
    return result


def _worker(case: str, source_root: Path, trace_python: bool = False) -> dict:
    sys.path.insert(0, str(source_root))
    logging.disable(logging.CRITICAL)
    if trace_python:
        tracemalloc.start()
    started = time.perf_counter()
    from scripts import sidecar

    digest = hashlib.sha256()
    count = 0
    with tempfile.TemporaryDirectory(prefix="runway-memory-fixtures-") as temporary:
        root = Path(temporary)
        if case == "desktop_import":
            from sidecar_app import config, daemon, updater

            updater._resolve_channel()
            count = len({id(config._sidecar), id(daemon._sidecar), id(sidecar)})
        elif case in ("backfill", "repeated"):
            from app.models.schemas import UsageEventPush
            from scripts.sidecar_pkg import event_watermark

            class Watermark:
                def __init__(self, _path):
                    pass

                def advance(self, *args):
                    pass

            size = 10000 if case == "backfill" else 1000

            def extract(*args, **kwargs):
                return [
                    UsageEventPush(
                        provider_id="anthropic",
                        account_id="fixture",
                        event_id=str(i),
                        ts="2026-10-09T00:00:00Z",
                        tokens_input=i,
                        tokens_output=100,
                    )
                    for i in range(size)
                ]

            def post(url, payload, key, **kwargs):
                nonlocal count
                for event in payload.get("events", []):
                    digest.update(json.dumps(event, sort_keys=True).encode())
                    count += 1
                return True, {}, 200

            runner = sidecar.DaemonRunner(
                {
                    "api_url": "http://fixture.invalid",
                    "api_key": "fixture",  # pragma: allowlist secret
                }
            )

            def collect(*args, **kwargs):
                payloads = []
                sidecar._extract_events_for_provider(
                    "anthropic", ["fixture"], watermark=None, bootstrap_days=90, out_events=payloads
                )
                return sidecar.CollectionResult([], payloads, 0, [])

            with (
                patch.object(sidecar, "_make_account_extractor", lambda *a: extract),
                patch.object(sidecar, "run_collection", collect),
                patch.object(sidecar, "http_post_signed_with_retry", post),
                patch.object(sidecar, "queue_flush", lambda *a, **k: 0),
                patch.object(sidecar, "_tail_log", lambda *a: []),
                patch.object(event_watermark, "EventWatermark", Watermark),
            ):
                for _ in range(1 if case == "backfill" else 20):
                    assert runner.run_once()
        elif case == "log_tail":
            log = root / "sidecar.log"
            with log.open("w") as stream:
                for i in range(50000):
                    stream.write(f"{i:06d}: " + "fixture" * 14 + "\n")
            with patch.object(sidecar, "get_log_path", lambda: log):
                for _ in range(20):
                    lines = sidecar._tail_log(20)
                    count += len(lines)
                    digest.update(json.dumps(lines).encode())
        elif case == "offline_queue":
            queue = root / "queue"
            queue.mkdir()
            entry = (
                json.dumps(
                    {"payload": {"events": [{"event_id": "fixture", "raw_json": "x" * 900}]}}
                )
                + "\n"
            )
            queue_file = queue / "2026-10-09.jsonl"
            with queue_file.open("w") as stream:
                for _ in range(10000):
                    stream.write(entry)
            with (
                patch.object(sidecar, "get_queue_dir", lambda: queue),
                patch.object(sidecar, "ensure_dirs", lambda: None),
                patch.object(
                    sidecar, "http_post_signed_with_retry", lambda *a, **k: (False, {}, 503)
                ),
            ):
                assert sidecar.queue_flush("http://fixture.invalid", "fixture") == 0
            with queue_file.open() as stream:
                for line in stream:
                    digest.update(json.dumps(json.loads(line), sort_keys=True).encode())
                    count += 1
        elif case == "sqlite":
            from scripts.sidecar_pkg.event_extractors.opencode import parse_opencode_events

            db = root / "opencode.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE message(id, session_id, time_created, data)")
            data = json.dumps(
                {"role": "assistant", "tokens": {"input": 10, "output": 20}, "content": "x" * 2000}
            )
            conn.executemany(
                "INSERT INTO message VALUES (?, ?, ?, ?)",
                ((str(i), "fixture", 1780003500000 + i, data) for i in range(10000)),
            )
            conn.commit()
            conn.close()
            events = parse_opencode_events(db, "fixture", datetime(2020, 1, 1, tzinfo=UTC))
            count = len(events)
            for event in events:
                digest.update(json.dumps(event.model_dump(mode="json"), sort_keys=True).encode())
            del events
    elapsed = time.perf_counter() - started
    live, peak = tracemalloc.get_traced_memory() if trace_python else (None, None)
    return {
        "case": case,
        "elapsed_seconds": elapsed,
        "python_live_mib": live / 2**20 if live is not None else None,
        "python_peak_mib": peak / 2**20 if peak is not None else None,
        "count": count,
        "digest": digest.hexdigest(),
        **_process_memory(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="origin/main")
    parser.add_argument("--trace-python", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--case", choices=_CASES)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    if args.source_root:
        print(json.dumps(_worker(args.case, args.source_root.resolve(), args.trace_python)))
        return
    if args.runs < 1:
        parser.error("--runs must be positive")
    archive = subprocess.run(
        ["git", "archive", args.baseline_ref, "app", "scripts", "sidecar_app", "package.json"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix="runway-memory-baseline-") as temporary:
        baseline = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive)) as archived:
            archived.extractall(baseline, filter="data")
        for case in (args.case,) if args.case else _CASES:
            results: dict[str, list[dict]] = {"baseline": [], "current": []}
            checks = []
            for _ in range(args.runs):
                # Alternate revisions so host load affects both comparably.
                for revision, root in (("baseline", baseline), ("current", _ROOT)):
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--case",
                        case,
                        "--source-root",
                        str(root),
                    ]
                    if args.trace_python:
                        command.append("--trace-python")
                    child = subprocess.run(
                        command, cwd=root, check=True, capture_output=True, text=True
                    )
                    result = json.loads(child.stdout)
                    results[revision].append(result)
                    checks.append((result["count"], result["digest"]))
            summaries = {
                revision: {
                    key: round(statistics.median(run[key] for run in runs), 3)
                    for key, value in runs[0].items()
                    if value is not None and key.endswith(("_mib", "_seconds"))
                }
                for revision, runs in results.items()
            }
            # The desktop case intentionally fixes the duplicate module count.
            if case == "desktop_import":
                if any(run["count"] != 1 for run in results["current"]):
                    raise AssertionError("Desktop still loads multiple sidecar modules")
                summaries["module_instances"] = {
                    revision: runs[0]["count"] for revision, runs in results.items()
                }
            elif len(set(checks)) != 1:
                raise AssertionError(f"Workload output changed: {case}")
            print(
                json.dumps(
                    {
                        "case": case,
                        "baseline_ref": args.baseline_ref,
                        "output_identical": True if case != "desktop_import" else None,
                        **summaries,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
