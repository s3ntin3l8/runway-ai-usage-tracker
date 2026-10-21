"""Split the pytest suite into balanced, file-level shards for parallel CI jobs.

The native Windows gate used to run the whole suite inside a single job. On
hosted Windows runners that suite takes ~11 minutes on a fast runner and ~27 on
a slow one, which is what put the 30-minute job timeout within reach (#712).
Running the suite as N parallel shards turns that one long job into N short
ones, and running it in a job with a timeout that survives runner variance
keeps a slow runner from failing the release.

Two properties matter here.

**Whole files.** A shard owns whole test modules, never a slice of one, so the
fixed-port helpers and the import-time environment setup in ``tests/conftest.py``
keep running in a single process exactly as they did in the unsharded run.
In-process parallelism (``pytest-xdist``) only keeps that property with
``--dist loadfile``, which is what the local runner and the Linux CI job use;
the default ``--dist load`` would split a module across workers and lose it.

This file and xdist solve different problems and both are in use. xdist
parallelises *within* one runner (several workers on one machine); this
planner parallelises *across* runners (one shard per CI job, each on its own
runner). The Windows CI job needs the latter: it runs one shard per runner,
because a hosted Windows runner is slow enough that a single job would blow
its timeout.

**Balanced by measured duration.** The suite's runtime is heavily skewed - two
modules are about 30% of the total - and the number of tests inside a module
barely predicts how long that module runs (correlation ~0.14 on this suite).
Splitting by file count or by test count therefore leaves the shards minutes
apart, so the weights come from measured per-module durations kept in
``tests/shard_durations.json``.

The weights are platform-specific: a module that is slow on Linux is not
necessarily slow on Windows (``test_multi_account_provider_config.py`` takes
26s on Linux and 136s on Windows, while ``test_macos_frozen_ca.py`` goes the
other way). Measuring them on one platform and planning the other leaves the
shards minutes apart -- which is exactly what a per-platform weight file
fixes.

Refresh the weights with ``python scripts/dev_checks.py record-shard-weights``.
The plan itself is a pure function of the module list and the weight table, so
every shard job derives the identical split without passing data between jobs.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARDS = 4

# pytest prints one durations row per test phase:
#   0.01s call     tests/unit/test_foo.py::test_bar
_DURATION_ROW = re.compile(
    r"^(?P<seconds>\d+(?:\.\d+)?)s\s+(?P<phase>setup|call|teardown)\s+(?P<node_id>\S+)$"
)


def weights_path(root: Path = ROOT) -> Path:
    """Where the measured per-module durations live."""
    return root / "tests" / "shard_durations.json"


def suite_files(root: Path = ROOT) -> list[str]:
    """Every test module the tree can contain, as repo-relative POSIX paths.

    Mirrors pytest's ``python_files = test_*.py`` rule. It is a superset of
    what pytest collects: ``tests/unit/conftest.py`` keeps a few modules in
    ``collect_ignore``, and pytest skips those on its own.
    """
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("test_*.py")
        if path.is_file()
    )


def load_weights(path: Path | None = None) -> dict[str, float]:
    """Measured per-module durations in seconds, keyed by repo-relative path.

    A missing or unreadable table is not an error: the planner then falls back
    to a flat split, which is slower but still correct.
    """
    target = weights_path() if path is None else path
    if not target.is_file():
        return {}
    payload = json.loads(target.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    if not isinstance(files, dict):
        return {}
    return {str(name): float(seconds) for name, seconds in files.items()}


def unknown_module_weight(weights: dict[str, float]) -> float:
    """Weight for a module the duration table has never measured.

    The median of the measured modules: a module nobody has timed yet is far
    more likely to be typical than extreme, and the median is not dragged
    around by the handful of very slow modules.
    """
    if not weights:
        return 1.0
    return median(sorted(weights.values()))


def plan_shards(
    files: list[str],
    weights: dict[str, float],
    shards: int,
) -> list[list[str]]:
    """Assign whole test modules to shards, heaviest module first.

    Greedy longest-processing-time-first: each module goes to the shard that
    is currently lightest. Ties break on the shard index, so the same module
    list and the same weight table always produce the same split.
    """
    if shards < 1:
        raise ValueError("shards must be at least 1")
    fallback = unknown_module_weight(weights)
    assigned: list[list[str]] = [[] for _ in range(shards)]
    loads = [0.0] * shards
    for name in sorted(files, key=lambda name: (-weights.get(name, fallback), name)):
        target = min(range(shards), key=lambda index: (loads[index], index))
        assigned[target].append(name)
        loads[target] += weights.get(name, fallback)
    return assigned


def shard_files(shard_id: int, shards: int, modules: list[str]) -> list[str]:
    """The test modules owned by one shard. Shard ids are 1-based."""
    if not 1 <= shard_id <= shards:
        raise ValueError(f"shard id must be between 1 and {shards}, got {shard_id}")
    plan = plan_shards(modules, load_weights(), shards)
    return plan[shard_id - 1]


def shard_deselections(modules: list[str], shard: list[str]) -> list[str]:
    """The pytest arguments that keep a shard to its own modules.

    pytest deselects by node id prefix and a module path is a prefix of every
    test id inside it, so one flag per module is enough. Handing pytest the
    shard's modules as arguments instead would bypass ``collect_ignore``:
    pytest would then import a module it deliberately skips and fail the
    collection.
    """
    keep = set(shard)
    return [f"--deselect={name}" for name in modules if name not in keep]


def parse_durations(report: str) -> dict[str, float]:
    """Aggregate a pytest ``--durations`` report into per-module seconds.

    pytest prints one row per test *phase*, so a module's cost is the sum of
    the rows that name it.
    """
    per_test: dict[str, float] = defaultdict(float)
    for line in report.splitlines():
        match = _DURATION_ROW.match(line.strip())
        if match is None:
            continue
        per_test[match.group("node_id")] += float(match.group("seconds"))
    per_module: dict[str, float] = defaultdict(float)
    for node_id, seconds in per_test.items():
        per_module[node_id.split("::", 1)[0]] += seconds
    return dict(per_module)


def write_weights(path: Path, durations: dict[str, float]) -> None:
    """Write the measured per-module durations to the weight table."""
    payload = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "total_seconds": round(sum(durations.values()), 1),
        "files": {name: round(seconds, 2) for name, seconds in sorted(durations.items())},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def estimate_shard_seconds(modules: list[str], weights: dict[str, float]) -> float:
    """Estimated runtime of one shard, from the measured module durations."""
    fallback = unknown_module_weight(weights)
    return sum(weights.get(name, fallback) for name in modules)


def summarize(shards: int, plan: list[list[str]], weights: dict[str, float]) -> str:
    """A human-readable summary of a shard plan."""
    lines = [
        f"pytest shard plan: {sum(len(modules) for modules in plan)} modules across {shards} shards"
    ]
    for index, modules in enumerate(plan, start=1):
        seconds = estimate_shard_seconds(modules, weights)
        lines.append(
            f"  shard {index}/{shards}: {len(modules):3d} modules, ~{seconds:.0f}s estimated"
        )
    total = sum(estimate_shard_seconds(modules, weights) for modules in plan)
    lines.append(f"  total:              ~{total:.0f}s estimated")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    plan_parser = subcommands.add_parser("plan", help="show the shard split")
    plan_parser.add_argument("--shards", type=int, default=DEFAULT_SHARDS)
    plan_parser.add_argument("--json", action="store_true", help="print the plan as JSON")

    files_parser = subcommands.add_parser("files", help="print the modules owned by one shard")
    files_parser.add_argument("--shard-id", type=int, default=1)
    files_parser.add_argument("--shards", type=int, default=DEFAULT_SHARDS)

    args = parser.parse_args()
    weights = load_weights()
    plan = plan_shards(suite_files(), weights, args.shards)

    if args.command == "files":
        if not 1 <= args.shard_id <= args.shards:
            parser.error(f"--shard-id must be between 1 and {args.shards}")
        for name in plan[args.shard_id - 1]:
            print(name)
        return 0

    if args.json:
        print(json.dumps(plan, indent=2))
        return 0

    print(summarize(args.shards, plan, weights))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
