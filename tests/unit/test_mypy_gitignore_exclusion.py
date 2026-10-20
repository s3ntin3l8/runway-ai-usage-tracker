"""mypy must skip the trees git ignores, the way ruff already does.

Ruff honours .gitignore by default; mypy did not. A contributor who ran
``make sidecar-app`` locally therefore had mypy walk the PyInstaller bundle in
``dist/``, which holds two copies of ``sidecar_app/assets/generate_icons.py``
-- one under ``Contents/Resources`` and one under ``Contents/Frameworks`` -- and
abort the whole run with "Duplicate module named". CI never builds that
bundle, so only local checkouts ever hit it.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    # pre-commit exports GIT_DIR and the other GIT_* variables so its hooks
    # stage into their own index. In a worktree .git is a file, so an
    # inherited GIT_DIR makes git refuse to run outside a work tree. Run
    # with a clean environment instead.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def git_checkout() -> None:
    """Skip when git is missing or the tree is not a checkout (sdist, image)."""
    if shutil.which("git") is None:
        pytest.skip("git is required to inspect the tracked sources")
    listed = _git("ls-files")
    if listed.returncode != 0 or not listed.stdout.split():
        pytest.skip("not a git checkout - there is nothing to assert")


def _ignore_rules() -> set[str]:
    """The literal rules in .gitignore, without comments or blank lines."""
    raw = (ROOT / ".gitignore").read_text(encoding="utf-8")
    rules = set()
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.add(line)
    return rules


def test_mypy_honours_gitignore(monkeypatch):
    """The installed mypy must be told to honour .gitignore.

    Asserted through mypy's own parser rather than by reading the TOML:
    mypy reports an unrecognised config key as a line of output and still
    exits 0, so a renamed option would leave the TOML intact, ``make lint``
    green, and the ``dist/`` abort back.
    """
    pytest.importorskip("mypy")
    from mypy.main import process_options

    monkeypatch.chdir(ROOT)
    stdout, stderr = io.StringIO(), io.StringIO()
    # require_targets=False keeps the check independent of the source tree:
    # only the parsed configuration matters here.
    _, options = process_options([], stdout=stdout, stderr=stderr, require_targets=False)

    assert options.exclude_gitignore is True
    assert "Unrecognized option" not in stdout.getvalue() + stderr.getvalue()


@pytest.mark.parametrize("root", ["build", "dist"])
def test_pyinstaller_output_roots_stay_gitignored(root: str):
    """Dropping these .gitignore entries would bring the abort back.

    Either spelling of the rule is fine: what matters is that the tree stays
    ignored, not whether the rule carries a trailing slash.
    """
    rules = _ignore_rules()
    assert root in rules or f"{root}/" in rules


def test_no_tracked_python_source_is_gitignored(git_checkout: None):
    """Honouring .gitignore must never hide a tracked source file from mypy."""
    tracked = _git("ls-files", "--", "*.py", "*.pyi").stdout.split()
    assert tracked, "expected tracked Python sources in the checkout"

    # --no-index forces the pattern check: without it git skips tracked files
    # and the assertion below would hold for the wrong reason.
    ignored = _git("check-ignore", "--no-index", "--stdin", stdin="\n".join(tracked)).stdout.split()
    assert ignored == []


@pytest.mark.parametrize("root", ["build", "dist"])
def test_the_generated_bundle_really_is_excluded(git_checkout: None, root: str):
    """Some file under the ignored tree must match the .gitignore rules.

    The path stays neutral on purpose: the .gitignore rule covers the whole
    directory, so this must not depend on the bundle's name.
    """
    result = _git("check-ignore", "--no-index", "-v", "--", f"{root}/bundle/x.py")
    assert result.returncode == 0, f"{root}/ is expected to be gitignored"
