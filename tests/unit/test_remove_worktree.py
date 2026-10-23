"""Removing a worktree must not delete the directories it links to.

A worktree in this repository links the main checkout's ``.venv`` into itself,
so it can share the installed dependencies. ``git worktree remove`` follows
those links on Windows and deletes the target -- the main checkout's
virtualenv. The helper drops the links first, so the removal only ever
deletes the worktree's own files.

Every git call goes through the repository's own ``isolated_git`` fixture: the
pre-commit hooks export ``GIT_DIR``, and inherited, that makes ``git init``
reinitialise the checkout the test is running in.
"""

import os
import subprocess
from pathlib import Path

import pytest

from scripts import remove_worktree as rw


def _git(isolated_git, *args: str, cwd: Path) -> str:
    """Run git without the environment the hooks export."""
    result = isolated_git(*args, cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _make_link(target: Path, link: Path) -> None:
    """Create a directory link, whatever this platform allows."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        pytest.skip("this platform allows no directory link")
    # A junction needs no privilege, so it is the link Windows actually uses
    # for a shared .venv. Symlinks need Developer Mode, junctions do not.
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f'New-Item -ItemType Junction -Path "{link}" -Target "{target}" | Out-Null',
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(f"cannot create a junction here: {result.stderr.decode(errors='replace')}")


def _repo(isolated_git, root: Path) -> Path:
    """A repository with one commit."""
    root.mkdir()
    _git(isolated_git, "init", "-q", cwd=root)
    (root / "README.md").write_text("repo")
    _git(isolated_git, "add", ".", cwd=root)
    _git(
        isolated_git,
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "init",
        cwd=root,
    )
    return root


def test_a_link_is_removed_and_its_target_survives(tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "link"
    _make_link(target, link)

    assert rw.is_link(link)
    rw.remove_link(link)

    assert not link.exists()
    assert (target / "keep.txt").read_text() == "keep"


def test_find_links_reports_links_without_following_them(tmp_path):
    target = tmp_path / "shared"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "link"
    _make_link(target, link)
    plain = tmp_path / "plain"
    plain.mkdir()

    links = rw.find_links(tmp_path)

    assert links == [link]
    assert plain.exists()


def test_unlink_links_removes_every_link(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for target in (first, second):
        target.mkdir()
        (target / "keep.txt").write_text("keep")
    one = tmp_path / "one"
    two = tmp_path / "two"
    _make_link(first, one)
    _make_link(second, two)

    removed = rw.unlink_links(tmp_path)

    assert sorted(removed) == sorted([one, two])
    assert (first / "keep.txt").exists()
    assert (second / "keep.txt").exists()


def test_removing_a_worktree_keeps_the_linked_directory(isolated_git, tmp_path):
    repo = _repo(isolated_git, tmp_path / "repo")
    tree = tmp_path / "tree"
    _git(isolated_git, "worktree", "add", "-q", str(tree), cwd=repo)

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "keep.txt").write_text("keep")
    _make_link(shared, tree / ".venv")
    assert (tree / ".venv" / "keep.txt").exists()

    assert rw.remove_worktree(tree, cwd=repo) == 0

    assert not tree.exists()
    assert (shared / "keep.txt").read_text() == "keep"


def test_a_dirty_worktree_is_reported_rather_than_forced(isolated_git, tmp_path):
    repo = _repo(isolated_git, tmp_path / "repo")
    tree = tmp_path / "tree"
    _git(isolated_git, "worktree", "add", "-q", str(tree), cwd=repo)
    (tree / "uncommitted.txt").write_text("work in progress")

    assert rw.remove_worktree(tree, cwd=repo) != 0
    # The worktree is still there -- git refused it -- and nothing was forced.
    assert tree.exists()
    assert (tree / "uncommitted.txt").exists()


def test_the_helper_is_reachable_without_a_venv(tmp_path):
    """The removal has to work when the environment is exactly what is broken."""
    from scripts import dev_checks as dc

    assert "remove-worktree" in dc.TASKS_WITHOUT_VENV


def test_a_checkout_that_lost_its_working_tree_gets_it_back(isolated_git, tmp_path):
    repo = _repo(isolated_git, tmp_path / "repo")

    # A stray `core.bare` in the shared config takes the whole checkout out of
    # use: every `git status` then answers "must be run in a work tree".
    _git(isolated_git, "config", "core.bare", "true", cwd=repo)
    assert _git(isolated_git, "config", "--get", "core.bare", cwd=repo).strip() == "true"

    assert rw.restore_working_tree(repo) is True

    assert _git(isolated_git, "config", "--get", "core.bare", cwd=repo).strip() == "false"
    assert "## " in _git(isolated_git, "status", "-sb", cwd=repo)


def test_a_genuinely_bare_repository_is_left_bare(isolated_git, tmp_path):
    bare = tmp_path / "bare.git"
    _git(isolated_git, "init", "--bare", "-q", str(bare), cwd=tmp_path)

    assert rw.restore_working_tree(bare) is False
    assert _git(isolated_git, "config", "--get", "core.bare", cwd=bare).strip() == "true"
