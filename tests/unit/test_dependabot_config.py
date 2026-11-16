"""``.github/dependabot.yml`` must point at real manifests and keep majors out of groups.

Nothing else validates the file (actionlint does not cover it), so a wrong
``directory`` or ecosystem name only shows up in Insights -> Dependabot after
merge. This checks each ``updates:`` entry against the repository, and the
documented rule that a weekly group carries minor and patch bumps only: a major
is a migration, not a batch. Dependabot reads a group with no ``update-types``
as *all* update types, so the key must be present, not merely free of ``major``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".github" / "dependabot.yml"

# ecosystem -> glob patterns (relative to the entry's directory) of a manifest it reads
MANIFESTS: dict[str, list[str]] = {
    "github-actions": [".github/workflows/*.yml", ".github/workflows/*.yaml", "action.yml"],
    "pip": ["requirements*.txt", "pyproject.toml", "setup.py"],
    "npm": ["package.json"],
    "docker": ["Dockerfile", "Dockerfile.*", "*.Dockerfile"],
    "pre-commit": [".pre-commit-config.yaml"],
}
# The ecosystems docs/ci-cd.md says Dependabot covers. Pinned so an empty or
# shrunken ``updates:`` list fails instead of passing vacuously.
EXPECTED_ECOSYSTEMS = set(MANIFESTS)


def _load(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("updates"), list), (
        f"{path.name} has no `updates:` list"
    )
    return list(data["updates"])


def _label(entry: dict[str, Any]) -> str:
    return f"{entry.get('package-ecosystem')}@{entry.get('directory')}"


def _problems(entry: dict[str, Any], root: Path) -> list[str]:
    label = _label(entry)
    ecosystem = entry.get("package-ecosystem")
    problems: list[str] = []
    if ecosystem not in MANIFESTS:
        problems.append(
            f"{label}: ecosystem {ecosystem!r} is not in MANIFESTS (add its manifest globs)"
        )
    else:
        directory = root / str(entry.get("directory", "")).lstrip("/")
        if not directory.is_dir():
            problems.append(f"{label}: directory does not exist")
        elif not any(any(directory.glob(p)) for p in MANIFESTS[ecosystem]):
            problems.append(
                f"{label}: no {ecosystem} manifest ({', '.join(MANIFESTS[ecosystem])}) in directory"
            )
    for name, group in (entry.get("groups") or {}).items():
        types = set(group.get("update-types") or [])
        if not types or types - {"minor", "patch"}:
            problems.append(
                f"{label}: group {name!r} must set update-types to minor/patch only "
                f"(absent means all types, majors included); got {sorted(types)}"
            )
    return problems


def test_every_entry_points_at_a_real_manifest_and_keeps_majors_out() -> None:
    entries = _load(CONFIG)
    assert {e.get("package-ecosystem") for e in entries} == EXPECTED_ECOSYSTEMS
    problems = [p for e in entries for p in _problems(e, ROOT)]
    assert problems == []


def test_detects_a_bad_directory_and_groups_that_admit_majors(tmp_path: Path) -> None:
    missing = {"package-ecosystem": "npm", "directory": "/nope"}
    assert "directory does not exist" in _problems(missing, tmp_path)[0]
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    base = {"package-ecosystem": "npm", "directory": "/web"}
    ok = {**base, "groups": {"g": {"update-types": ["minor", "patch"]}}}
    assert _problems(ok, tmp_path) == []
    for groups in (
        {"g": {"update-types": ["minor", "major"]}},
        {"g": {"patterns": ["*"]}},  # absent update-types means all types
    ):
        assert "must set update-types" in _problems({**base, "groups": groups}, tmp_path)[0]


def test_reports_a_bad_directory_and_a_bad_group_together(tmp_path: Path) -> None:
    entry = {"package-ecosystem": "npm", "directory": "/nope", "groups": {"g": {"patterns": ["*"]}}}
    assert len(_problems(entry, tmp_path)) == 2


def test_an_unlisted_ecosystem_is_reported_as_such(tmp_path: Path) -> None:
    assert (
        "not in MANIFESTS"
        in _problems({"package-ecosystem": "cargo", "directory": "/"}, tmp_path)[0]
    )


def test_an_empty_or_missing_updates_list_is_a_readable_failure(tmp_path: Path) -> None:
    for text in ("version: 2\nupdates: []\n", "version: 2\n"):
        path = tmp_path / "dependabot.yml"
        path.write_text(text)
        try:
            entries = _load(path)
        except AssertionError as exc:
            assert "updates" in str(exc)
        else:
            assert {e["package-ecosystem"] for e in entries} != EXPECTED_ECOSYSTEMS
