#!/usr/bin/env python3
"""Compare server memory/latency with an archived revision and synthetic data.

No production data or credentials are used. Each sample runs in a fresh
process against its own database copy; fixture creation is outside measurement.
Run: .venv/bin/python scripts/benchmark_server_memory.py --baseline-ref origin/main
Add --matrix to compare allocator arena limits and SQLite temporary stores.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import logging
import math
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_START = datetime(2026, 9, 1, tzinfo=UTC)
_END = datetime(2026, 10, 1, tzinfo=UTC)
_CASES = ("window", "classify", "recost-preview", "recost-apply", "dashboard", "ingest", "mixed")
_MODELS = ("bench-a", "bench-b", "bench-c", "bench:free")


def _memory() -> dict[str, float]:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {"peak_rss_mib": peak / (2**20 if sys.platform == "darwin" else 1024)}
    if sys.platform.startswith("linux"):
        for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
            key = line.partition(":")[0]
            if key in ("Rss", "Pss", "Anonymous"):
                result[key.lower() + "_mib"] = int(line.split()[1]) / 1024
    return result


def _normalise(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _normalise(asdict(value))
    if isinstance(value, dict):
        return {
            str(k): _normalise(v)
            for k, v in sorted(value.items(), key=lambda x: str(x[0]))
            if k != "generated_at"
        }
    if isinstance(value, set):
        return sorted((_normalise(x) for x in value), key=str)
    if isinstance(value, (tuple, list)):
        return [_normalise(x) for x in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 6)
    return value


def _seed(db_path: Path, events: int, snapshots: int) -> None:
    from sqlmodel import Session, SQLModel

    from app.core.db import engine
    from app.models.db import ProviderPricing, QuotaSnapshot, UsageEvent
    from app.services.period_rollups import rebuild_rollups_for_providers

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for offset in range(0, events, 1000):
            conn.execute(
                UsageEvent.__table__.insert(),
                [
                    {
                        "provider_id": "anthropic",
                        "account_id": "benchmark",
                        "event_id": f"event-{i}",
                        "sidecar_id": f"host-{i % 5}",
                        "model_id": _MODELS[i % len(_MODELS)],
                        "ts": _START + timedelta(seconds=i),
                        "kind": "error" if i % 101 == 0 else "message",
                        "tokens_input": 1200,
                        "tokens_output": 300,
                        "tokens_cache_read": 400,
                        "session_id": f"session-{i // 20}",
                        "project": f"project-{i % 8}",
                        "raw_json": json.dumps({"message": "x" * 512}),
                    }
                    for i in range(offset, min(offset + 1000, events))
                ],
            )
        for offset in range(0, snapshots, 1000):
            conn.execute(
                QuotaSnapshot.__table__.insert(),
                [
                    {
                        "provider_id": "anthropic",
                        "account_id": "benchmark",
                        "window_type": "monthly",
                        "variant": "",
                        "model_id": _MODELS[i % len(_MODELS)],
                        "ts": _START + timedelta(seconds=i),
                        "pct_used": (i % 100) / 100,
                        "reset_at": _END,
                    }
                    for i in range(offset, min(offset + 1000, snapshots))
                ],
            )
        conn.execute(
            ProviderPricing.__table__.insert(),
            [
                {
                    "provider_id": "anthropic",
                    "model_id": model,
                    "effective_from": date(2020, 1, 1),
                    "input_per_mtok": 0.0 if model.endswith(":free") else 2.0,
                    "output_per_mtok": 0.0 if model.endswith(":free") else 4.0,
                }
                for model in _MODELS
            ],
        )
    with Session(engine) as session:
        rebuild_rollups_for_providers(session, ["anthropic"])
        session.commit()
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
    engine.dispose()
    assert db_path.exists()


def _operation(case: str, sequence: int = 0) -> Any:
    from sqlalchemy import func
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.models.db import UsageEvent, UsageWindow
    from app.models.schemas import UsageEventPush
    from app.services.data_health.checks.unpriced_models import _classify_events
    from app.services.event_ingestor import EventIngestor
    from app.services.maintenance.recost import apply_recost, plan_recost
    from app.services.queries.cumulative import query_cumulative_live
    from app.services.queries.forecast import query_pct_snapshot_buckets_batch
    from app.services.queries.global_stats import query_global_stats
    from app.services.window_closer import close_window

    with Session(engine) as session:
        if case == "window":
            close_window(
                session,
                provider_id="anthropic",
                account_id="benchmark",
                window_type="monthly",
                window_start=_START,
                window_end=_END,
            )
            rows = session.execute(
                select(
                    UsageWindow.model_id,
                    UsageWindow.sidecar_id,
                    UsageWindow.msgs,
                    UsageWindow.tokens_input,
                    UsageWindow.tokens_output,
                    UsageWindow.tokens_cache_read,
                    UsageWindow.cost_usd,
                ).order_by(UsageWindow.model_id, UsageWindow.sidecar_id)
            ).all()
            session.commit()
            return [tuple(row) for row in rows]
        if case == "classify":
            return _classify_events(session)
        if case == "recost-preview":
            return plan_recost(session, None)
        if case == "recost-apply":
            result = apply_recost(session, None, skip_rollups=True, skip_windows=True)
            return [result, session.execute(select(func.sum(UsageEvent.cost_usd))).scalar_one()]
        if case == "dashboard":
            return [
                query_cumulative_live(session, since=_START, until=_END),
                query_pct_snapshot_buckets_batch(
                    session, since=_START, until=_END, bucket_seconds=300
                ),
                query_global_stats(session),
            ]
        if case == "ingest":
            pushes = [
                UsageEventPush(
                    provider_id="anthropic",
                    account_id="benchmark",
                    account_source="local",
                    event_id=f"push-{sequence}-{i}",
                    model_id="bench-a",
                    ts=_START.isoformat(),
                    tokens_input=1200,
                    tokens_output=300,
                )
                for i in range(25)
            ]
            return EventIngestor(session).ingest(pushes, sidecar_id=f"host-{sequence % 5}")
    raise ValueError(case)


def _worker(case: str, rounds: int, trace_python: bool = False) -> dict[str, Any]:
    logging.disable(logging.CRITICAL)
    # Imports are not request latency, and should not dominate short cases.
    for module in (
        "app.services.event_ingestor",
        "app.services.window_closer",
        "app.services.maintenance.recost",
        "app.services.data_health.checks.unpriced_models",
        "app.services.queries.cumulative",
        "app.services.queries.forecast",
        "app.services.queries.global_stats",
    ):
        importlib.import_module(module)
    if trace_python:
        import tracemalloc

        tracemalloc.start()
    latencies = []
    digest = hashlib.sha256()
    started = time.perf_counter()
    if case == "mixed":
        # Exercise several long-lived worker threads, including post-query
        # retained memory. Writes stay on the calling thread (SQLite single writer).
        with ThreadPoolExecutor(max_workers=5) as executor:
            for _ in range(rounds):
                futures = [
                    executor.submit(_timed, "dashboard" if i % 2 else "classify", 0)
                    for i in range(5)
                ]
                for future in futures:
                    elapsed, output = future.result()
                    latencies.append(elapsed)
                    digest.update(json.dumps(_normalise(output), sort_keys=True).encode())
                elapsed, output = _timed("window", 0)
                latencies.append(elapsed)
                digest.update(json.dumps(_normalise(output), sort_keys=True).encode())
    else:
        for i in range(rounds):
            elapsed, output = _timed(case, i)
            latencies.append(elapsed)
            digest.update(json.dumps(_normalise(output), sort_keys=True).encode())
    result = {
        "elapsed_seconds": time.perf_counter() - started,
        "p95_seconds": sorted(latencies)[math.ceil(len(latencies) * 0.95) - 1],
        "digest": digest.hexdigest(),
        **_memory(),
    }
    if trace_python:
        result["python_peak_mib"] = tracemalloc.get_traced_memory()[1] / 2**20
    return result


def _timed(case: str, sequence: int) -> tuple[float, Any]:
    start = time.perf_counter()
    output = _operation(case, sequence)
    return time.perf_counter() - start, output


def _child(
    root: Path, db: Path, args: list[str], arena: str = "", temp_store: str = "MEMORY"
) -> dict:
    env = dict(os.environ)
    env.pop("MALLOC_ARENA_MAX", None)
    if arena:
        env["MALLOC_ARENA_MAX"] = arena
    env.update(
        RUNWAY_CONFIG_DIR=str(db.parent),
        DATABASE_PATH=str(db),
        SQLITE_TEMP_STORE=temp_store,
        APP_HOST="127.0.0.1",
    )
    command = [sys.executable, str(Path(__file__).resolve()), "--source-root", str(root), *args]
    child = subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, text=True)
    return json.loads(child.stdout or "{}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="origin/main")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--events", type=int, default=100000)
    parser.add_argument("--snapshots", type=int, default=100000)
    parser.add_argument("--case", choices=_CASES, action="append")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument(
        "--trace-python", action="store_true", help="Measure Python allocations (slower)"
    )
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--seed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.runs, args.rounds, args.events, args.snapshots) < 1:
        parser.error("runs, rounds, events and snapshots must be positive")
    if args.source_root:
        sys.path.insert(0, str(args.source_root))
        if args.seed:
            _seed(Path(os.environ["DATABASE_PATH"]), args.events, args.snapshots)
        else:
            print(json.dumps(_worker(args.case[0], args.rounds, args.trace_python)))
        return

    baseline_sha = subprocess.run(
        ["git", "rev-parse", args.baseline_ref],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "archive", baseline_sha, "app"], cwd=_ROOT, check=True, capture_output=True
    ).stdout
    with tempfile.TemporaryDirectory(prefix="runway-server-memory-") as temporary:
        temp = Path(temporary)
        baseline = temp / "baseline"
        baseline.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as archived:
            archived.extractall(baseline, filter="data")
        fixture = temp / "fixture.db"
        _child(
            _ROOT,
            fixture,
            ["--seed", "--events", str(args.events), "--snapshots", str(args.snapshots)],
        )
        variants = [("baseline", baseline, "", "MEMORY"), ("current", _ROOT, "", "MEMORY")]
        if args.matrix:
            variants.extend(
                [
                    (f"arena-{arena}-{mode.lower()}", _ROOT, arena, mode)
                    for arena, mode in [
                        ("2", "MEMORY"),
                        ("4", "MEMORY"),
                        ("", "FILE"),
                        ("2", "FILE"),
                    ]
                ]
            )
        for case in args.case or _CASES:
            results = {name: [] for name, *_ in variants}
            for _ in range(args.runs):
                for name, root, arena, mode in variants:
                    with tempfile.TemporaryDirectory(dir=temp) as sample_dir:
                        db = Path(sample_dir) / "runway.db"
                        shutil.copyfile(fixture, db)
                        worker_args = ["--case", case, "--rounds", str(args.rounds)]
                        if args.trace_python:
                            worker_args.append("--trace-python")
                        result = _child(root, db, worker_args, arena, mode)
                        results[name].append(result)
            digests = {run["digest"] for runs in results.values() for run in runs}
            if len(digests) != 1:
                raise AssertionError(f"Output changed for {case}: {results}")
            summary = {
                name: {
                    key: round(statistics.median(run[key] for run in runs), 3)
                    for key in runs[0]
                    if key != "digest"
                }
                for name, runs in results.items()
            }
            print(
                json.dumps(
                    {
                        "case": case,
                        "events": args.events,
                        "snapshots": args.snapshots,
                        "runs": args.runs,
                        "rounds": args.rounds,
                        "baseline_ref": args.baseline_ref,
                        "baseline_sha": baseline_sha,
                        "output_identical": True,
                        **summary,
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
