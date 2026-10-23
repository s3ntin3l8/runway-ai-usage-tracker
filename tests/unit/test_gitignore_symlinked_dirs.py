"""Gitignored directory names must also be ignored when they are symlinks.

git matches a trailing-slash rule against directories only: a symlink to a
directory is a file to git, so ``.venv/`` does not cover the symlinked venv
that CONTRIBUTING.md tells contributors to create in a worktree. Such a
worktree then shows the symlink as untracked, and a ``git add -A`` would
commit it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The directories a contributor may legitimately replace with a symlink: the
# virtualenvs (CONTRIBUTING.md points at a symlinked .venv in a worktree), the
# dependency tree, and the PyInstaller output roots - the last two are large
# enough to be worth keeping outside the checkout.
SYMLINKABLE = [".venv", ".venv-sc", "venv", "ENV", "env", "node_modules", "build", "dist"]


@pytest.fixture
def checkout(tmp_path: Path, isolated_git) -> Path:
    """A throwaway git repository carrying this repository's .gitignore."""
    if shutil.which("git") is None:
        pytest.skip("git is required to test the ignore rules")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(
        (ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8"
    )
    # No GIT_DIR here: git initialises a *bare* repository when GIT_DIR is set
    # without GIT_WORK_TREE, and check-ignore then refuses to run.
    init = isolated_git("init", "--quiet", cwd=repo)
    assert init.returncode == 0, init.stderr
    # The new repository has to be a work tree inside the temp directory, so a
    # git that lays it out differently fails here instead of in every
    # assertion below.
    assert (repo / ".git").is_dir(), "the new repository landed outside the temp directory"
    work_tree = isolated_git("rev-parse", "--is-inside-work-tree", cwd=repo)
    assert work_tree.stdout.strip() == "true", work_tree.stderr
    return repo


@pytest.mark.parametrize("name", SYMLINKABLE)
def test_a_symlinked_directory_is_ignored(
    checkout: Path, isolated_git, make_symlink, name: str
) -> None:
    """The name must be ignored as a symlink, not only as a real directory."""
    target = checkout.parent / "target"
    target.mkdir(exist_ok=True)
    make_symlink(checkout / name, target, target_is_directory=True)

    result = isolated_git("check-ignore", "--no-index", "-v", "--", name, cwd=checkout)
    assert result.returncode == 0, f"{name} is not ignored as a symlink:\n{result.stdout}"


@pytest.mark.parametrize("name", SYMLINKABLE)
def test_a_real_directory_is_still_ignored(checkout: Path, isolated_git, name: str) -> None:
    """Dropping the trailing slash must not stop the directory from matching."""
    (checkout / name).mkdir()

    result = isolated_git("check-ignore", "--no-index", "-v", "--", name, cwd=checkout)
    assert result.returncode == 0, f"{name} is not ignored as a directory:\n{result.stdout}"
