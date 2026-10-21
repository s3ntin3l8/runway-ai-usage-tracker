"""Tests for the parallel pytest shard planner (issue #712)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.pytest_shard import (
    DEFAULT_SHARDS,
    ROOT,
    estimate_shard_seconds,
    load_weights,
    parse_durations,
    plan_shards,
    shard_deselections,
    shard_files,
    suite_files,
    summarize,
    unknown_module_weight,
    weights_path,
    write_weights,
)

SUITE_DIRECTORIES = ("unit", "integration", "services", "security", "data_health")


def collect_ignored_modules() -> set[str]:
    """The modules pytest skips through ``collect_ignore`` in a conftest."""
    ignored: set[str] = set()
    for conftest in sorted((ROOT / "tests").rglob("conftest.py")):
        tree = ast.parse(conftest.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "collect_ignore" not in names or not isinstance(node.value, ast.List):
                continue
            for element in node.value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    ignored.add((conftest.parent / element.value).relative_to(ROOT).as_posix())
    return ignored


def test_suite_files_lists_repo_relative_posix_paths() -> None:
    files = suite_files()
    assert files, "the suite must contain test modules"
    assert files == sorted(files), "the module list must be sorted for a stable split"
    for name in files:
        assert name.startswith("tests/"), name
        assert name.endswith(".py"), name
        assert "\\" not in name, name
        assert (ROOT / name).is_file(), name


def test_suite_files_covers_every_suite_directory() -> None:
    directories = {name.split("/")[1] for name in suite_files()}
    directories |= {name.split("/")[2] for name in suite_files() if name.count("/") > 1}
    for directory in SUITE_DIRECTORIES:
        assert directory in directories, f"tests/{directory} is missing from the shard plan"


def test_plan_assigns_every_module_exactly_once() -> None:
    files = suite_files()
    plan = plan_shards(files, load_weights(), DEFAULT_SHARDS)
    assigned = [name for shard in plan for name in shard]
    assert sorted(assigned) == files
    assert len(assigned) == len(set(assigned))


def test_plan_is_deterministic() -> None:
    files = suite_files()
    weights = load_weights()
    assert plan_shards(files, weights, DEFAULT_SHARDS) == plan_shards(
        files, weights, DEFAULT_SHARDS
    )


def test_plan_keeps_whole_modules_together() -> None:
    plan = plan_shards(suite_files(), load_weights(), DEFAULT_SHARDS)
    for shard in plan:
        assert len(shard) == len(set(shard))
        for name in shard:
            assert name.endswith(".py")


def test_plan_balances_by_measured_duration() -> None:
    weights = {"slow.py": 100.0, "medium.py": 10.0, "fast.py": 1.0}
    plan = plan_shards(["slow.py", "medium.py", "fast.py"], weights, 3)
    assert plan[0] == ["slow.py"]
    assert plan[1] == ["medium.py"]
    assert plan[2] == ["fast.py"]


def test_plan_spreads_unknown_modules_across_shards() -> None:
    weights = {f"known{index}.py": float(index + 1) for index in range(4)}
    plan = plan_shards(["known0.py", "known1.py", "known2.py", "known3.py"], weights, 2)
    unknown = plan_shards([*weights, "new.py"], weights, 2)
    # The unmeasured module joins the lighter shard rather than piling onto one.
    assert "new.py" in unknown[1]
    assert len(plan[0]) == len(plan[1]) == 2


def test_unknown_module_weight_is_the_median() -> None:
    assert unknown_module_weight({}) == 1.0
    assert unknown_module_weight({"a.py": 1.0, "b.py": 2.0, "c.py": 3.0}) == 2.0


def test_plan_rejects_a_non_positive_shard_count() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        plan_shards(["a.py"], {}, 0)


def test_shard_files_rejects_an_out_of_range_id() -> None:
    with pytest.raises(ValueError, match="between 1 and"):
        shard_files(0, 4, ["a.py"])
    with pytest.raises(ValueError, match="between 1 and"):
        shard_files(5, 4, ["a.py"])


def test_shard_files_returns_a_subset_of_the_suite() -> None:
    files = shard_files(2, DEFAULT_SHARDS, suite_files())
    assert files
    assert set(files) <= set(suite_files())


def test_shard_deselections_exclude_every_other_module() -> None:
    modules = ["tests/unit/a.py", "tests/unit/b.py", "tests/unit/c.py"]
    deselections = shard_deselections(modules, ["tests/unit/b.py"])
    assert deselections == ["--deselect=tests/unit/a.py", "--deselect=tests/unit/c.py"]


def test_shard_deselections_are_empty_for_a_single_shard() -> None:
    modules = ["tests/unit/a.py", "tests/unit/b.py"]
    assert shard_deselections(modules, modules) == []


def test_parse_durations_sums_the_phases_of_each_module() -> None:
    report = "\n".join(
        [
            "============================= slowest durations ==============================",
            "0.50s call     tests/unit/test_a.py::test_one",
            "0.10s setup    tests/unit/test_a.py::test_one",
            "0.05s teardown tests/unit/test_a.py::test_one",
            "1.25s call     tests/unit/test_b.py::test_two",
            "not a durations row",
        ]
    )
    durations = parse_durations(report)
    assert durations["tests/unit/test_a.py"] == pytest.approx(0.65)
    assert durations["tests/unit/test_b.py"] == pytest.approx(1.25)
    assert "not a durations row" not in durations


def test_parse_durations_ignores_an_empty_report() -> None:
    assert parse_durations("") == {}


def test_write_and_load_weights_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "shard_durations.json"
    write_weights(target, {"tests/unit/test_b.py": 1.5, "tests/unit/test_a.py": 2.0})
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["total_seconds"] == pytest.approx(3.5)
    assert list(payload["files"]) == ["tests/unit/test_a.py", "tests/unit/test_b.py"]
    assert load_weights(target) == {"tests/unit/test_a.py": 2.0, "tests/unit/test_b.py": 1.5}


def test_load_weights_tolerates_a_missing_table(tmp_path: Path) -> None:
    assert load_weights(tmp_path / "absent.json") == {}


def test_committed_weight_table_is_well_formed() -> None:
    target = weights_path()
    if not target.is_file():
        # The table is a balancing hint, not a correctness requirement: without
        # it the planner falls back to a flat split, which still covers the
        # whole suite. Recording one is `make shard-weights`.
        pytest.skip("the measured durations table has not been recorded yet")
    payload = json.loads(target.read_text(encoding="utf-8"))
    files = payload["files"]
    assert files, "the measured durations table must not be empty"
    for name, seconds in files.items():
        assert name.startswith("tests/"), name
        assert name.endswith(".py"), name
        assert isinstance(seconds, float), name
        assert seconds >= 0.0, name


def test_committed_weight_table_skips_the_ignored_modules() -> None:
    """A module pytest skips must never reach a shard's command line.

    Handing pytest a ``collect_ignore``d module turns the shard into a
    collection error, so the measured table must not contain one.
    """
    weights = load_weights()
    if not weights:
        pytest.skip("the measured durations table has not been recorded yet")
    ignored = collect_ignored_modules()
    assert ignored, "the suite is expected to skip at least one module"
    assert not set(weights) & ignored


def test_committed_plan_stays_balanced() -> None:
    """The committed split must keep every shard within a narrow band.

    A shard that runs far longer than its peers is what used to push the
    monolithic Windows job into its timeout, so the plan is checked here
    rather than only discovered in CI.
    """
    weights = load_weights()
    if not weights:
        pytest.skip("the measured durations table has not been recorded yet")
    plan = plan_shards(suite_files(), weights, DEFAULT_SHARDS)
    estimates = [estimate_shard_seconds(shard, weights) for shard in plan]
    mean = sum(estimates) / len(estimates)
    assert max(estimates) <= mean * 1.5, estimates


def test_summarize_reports_every_shard() -> None:
    weights = {"a.py": 1.0, "b.py": 2.0}
    summary = summarize(2, [["a.py"], ["b.py"]], weights)
    assert "shard 1/2" in summary
    assert "shard 2/2" in summary
