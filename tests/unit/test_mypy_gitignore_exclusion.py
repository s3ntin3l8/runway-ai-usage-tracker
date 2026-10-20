"""mypy must skip the trees git ignores, the way ruff already does.

Ruff honours .gitignore by default; mypy did not. A contributor who ran
``make sidecar-app`` locally therefore had mypy walk the PyInstaller bundle in
``dist/``, which holds two copies of ``assets/generate_icons.py`` -- one per
frameworks tree -- and abort the whole run with "Duplicate module named". CI
never builds that bundle, so only local checkouts ever hit it.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _mypy_config() -> dict:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["mypy"]


def _gitignore_lines() -> set[str]:
    raw = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return {line.strip() for line in raw.splitlines()}


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_mypy_honours_gitignore():
    """The mypy config must opt into .gitignore, like ruff does by default."""
    assert _mypy_config().get("exclude_gitignore") is True


@pytest.mark.parametrize("root", ["build/", "dist/"])
def test_pyinstaller_output_roots_stay_gitignored(root: str):
    """Dropping these .gitignore entries would bring the abort back."""
    assert root in _gitignore_lines()


def test_no_tracked_python_source_is_gitignored():
    """Honouring .gitignore must never hide a tracked source file from mypy."""
    tracked = _git("ls-files", "--", "*.py").stdout.split()
    assert tracked, "expected tracked Python sources in the checkout"

    # --no-index forces the pattern check: without it git skips tracked files
    # and the assertion below would hold for the wrong reason.
    ignored = _git("check-ignore", "--no-index", "--stdin", stdin="\n".join(tracked)).stdout.split()
    assert ignored == []


def test_the_generated_bundle_really_is_excluded():
    """A file under the ignored bundle must match the .gitignore rules."""
    result = _git("check-ignore", "--no-index", "-v", "--", "dist/Runway Sidecar.app/x.py")
    assert result.returncode == 0, "dist/ is expected to be gitignored"
