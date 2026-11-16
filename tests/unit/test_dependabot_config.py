"""``.github/dependabot.yml`` must point at real manifests and keep majors out of groups.

Nothing else validates the file (actionlint does not cover it), so a wrong
``directory`` or ecosystem name only shows up in Insights -> Dependabot after
merge. This checks each ``updates:`` entry against the repository, and the
documented rule that a weekly group carries minor and patch bumps only: a major
is a migration, not a batch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
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


def _entries() -> list[dict[str, Any]]:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return list(data["updates"])


def _label(entry: dict[str, Any]) -> str:
    return f"{entry.get('package-ecosystem')}@{entry.get('directory')}"


def _problems(entry: dict[str, Any], root: Path) -> list[str]:
    label = _label(entry)
    ecosystem = entry.get("package-ecosystem")
    if ecosystem not in MANIFESTS:
        return [f"{label}: unknown package-ecosystem {ecosystem!r}"]
    directory = root / str(entry.get("directory", "")).lstrip("/")
    if not directory.is_dir():
        return [f"{label}: directory does not exist"]
    if not any(any(directory.glob(p)) for p in MANIFESTS[ecosystem]):
        return [
            f"{label}: no {ecosystem} manifest ({', '.join(MANIFESTS[ecosystem])}) in directory"
        ]
    problems = []
    for name, group in (entry.get("groups") or {}).items():
        bad = set(group.get("update-types") or []) - {"minor", "patch"}
        if bad:
            problems.append(
                f"{label}: group {name!r} groups {sorted(bad)} updates (only minor/patch)"
            )
    return problems


@pytest.mark.parametrize("entry", _entries(), ids=_label)
def test_entry_points_at_a_real_manifest(entry: dict[str, Any]) -> None:
    assert _problems(entry, ROOT) == []


def test_every_update_entry_is_checked() -> None:
    assert {e["package-ecosystem"] for e in _entries()} <= set(MANIFESTS)


def test_detects_a_bad_directory_and_a_major_in_a_group(tmp_path: Path) -> None:
    missing = {"package-ecosystem": "npm", "directory": "/nope"}
    assert "directory does not exist" in _problems(missing, tmp_path)[0]
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    major = {
        "package-ecosystem": "npm",
        "directory": "/web",
        "groups": {"g": {"update-types": ["minor", "major"]}},
    }
    assert "'major'" in _problems(major, tmp_path)[0]
    assert _problems({**major, "groups": {"g": {"patterns": ["*"]}}}, tmp_path) == []
