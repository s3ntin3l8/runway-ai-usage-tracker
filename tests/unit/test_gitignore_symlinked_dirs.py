"""Gitignored directory names must also be ignored when they are symlinks.

git matches a trailing-slash rule against directories only: a symlink to a
directory is a file to git, so ``.venv/`` does not cover the symlinked venv
that CONTRIBUTING.md tells contributors to create in a worktree. Such a
worktree then shows the symlink as untracked, and a ``git add -A`` would
commit it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# The directories a contributor may legitimately replace with a symlink: the
# virtualenvs (CONTRIBUTING.md points at a symlinked .venv in a worktree), the
# dependency tree, and the PyInstaller output roots - the last two are large
# enough to be worth keeping outside the checkout.
SYMLINKABLE = [".venv", ".venv-sc", "venv", "ENV", "env", "node_modules", "build", "dist"]


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """A git environment in which only the repository's own .gitignore decides.

    Without the isolation, a developer's global ``core.excludesFile`` could
    make a case match that the .gitignore fix is supposed to cover, and the
    test would pass on a checkout that never got the fix.
    """
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(empty)
    return env


def _git(*args: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # pre-commit exports GIT_DIR and the other GIT_* variables so its hooks
    # stage into their own index. In a worktree .git is a file, so an
    # inherited GIT_DIR makes git refuse to run outside a work tree - and an
    # inherited GIT_DIR would also make `git init` reinitialise the checkout
    # that runs the test. The fixture above strips them.
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def checkout(tmp_path: Path, git_env: dict[str, str]) -> Path:
    """A throwaway git repository carrying this repository's .gitignore."""
    if shutil.which("git") is None:
        pytest.skip("git is required to test the ignore rules")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"))
    # No GIT_DIR here: git initialises a *bare* repository when GIT_DIR is set
    # without GIT_WORK_TREE, and check-ignore then refuses to run.
    init = _git("init", "--quiet", cwd=repo, env=git_env)
    assert init.returncode == 0, init.stderr
    # The new repository has to be a work tree inside the temp directory, so a
    # git that lays it out differently fails here instead of in every
    # assertion below.
    assert (repo / ".git").is_dir(), "the new repository landed outside the temp directory"
    work_tree = _git("rev-parse", "--is-inside-work-tree", cwd=repo, env=git_env)
    assert work_tree.stdout.strip() == "true", work_tree.stderr
    return repo


@pytest.mark.parametrize("name", SYMLINKABLE)
def test_a_symlinked_directory_is_ignored(
    checkout: Path, git_env: dict[str, str], make_symlink, name: str
) -> None:
    """The name must be ignored as a symlink, not only as a real directory."""
    target = checkout.parent / "target"
    target.mkdir(exist_ok=True)
    make_symlink(checkout / name, target, target_is_directory=True)

    result = _git("check-ignore", "--no-index", "-v", "--", name, cwd=checkout, env=git_env)
    assert result.returncode == 0, f"{name} is not ignored as a symlink:\n{result.stdout}"


@pytest.mark.parametrize("name", SYMLINKABLE)
def test_a_real_directory_is_still_ignored(
    checkout: Path, git_env: dict[str, str], name: str
) -> None:
    """Dropping the trailing slash must not stop the directory from matching."""
    (checkout / name).mkdir()

    result = _git("check-ignore", "--no-index", "-v", "--", name, cwd=checkout, env=git_env)
    assert result.returncode == 0, f"{name} is not ignored as a directory:\n{result.stdout}"
