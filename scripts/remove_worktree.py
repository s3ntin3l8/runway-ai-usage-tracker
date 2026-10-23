"""Remove a git worktree without deleting what it links to.

A worktree in this repository usually links the main checkout's ``.venv`` (and
``webapp/node_modules``) into itself, so it can share the installed
dependencies instead of paying for a second install. On Windows those links are
directory junctions, and ``git worktree remove`` follows them: it deletes the
*target's* contents, which is the main checkout's virtualenv. The removal
succeeds, and the main checkout is left without a working environment -- and
every other worktree that shared it through a junction loses it too.

This helper removes the links first, and only the links, and then hands the
worktree to git. It never forces the removal: git still refuses a worktree
with uncommitted work, and the helper reports that instead of overriding it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _is_junction(path: Path) -> bool:
    """True for a Windows directory junction (``os.path.isjunction`` is 3.12+)."""
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def is_link(path: Path) -> bool:
    """True when *path* is a symlink or a Windows directory junction."""
    return path.is_symlink() or _is_junction(path)


def find_links(root: Path) -> list[Path]:
    """Every symlink and junction under *root*, without following any of them."""
    links: list[Path] = []
    if not root.is_dir():
        return links
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            candidate = Path(dirpath) / name
            if not is_link(candidate):
                continue
            links.append(candidate)
            # Never walk into a link: the point is to drop it, not to read it.
            if name in dirnames:
                dirnames.remove(name)
    return sorted(links)


def remove_link(path: Path) -> None:
    """Delete a link without touching what it points at."""
    if os.name == "nt" and (_is_junction(path) or path.is_dir()):
        # A directory link on Windows is a reparse point: rmdir drops the link
        # and leaves the target alone. os.unlink refuses a directory link.
        os.rmdir(path)
    else:
        os.unlink(path)


def unlink_links(root: Path) -> list[Path]:
    """Remove every link under *root*; return the ones that were removed."""
    removed: list[Path] = []
    for link in find_links(root):
        remove_link(link)
        removed.append(link)
    return removed


def _git_env() -> dict[str, str]:
    """Git without the ``GIT_*`` variables the hooks export.

    The pre-commit hooks export ``GIT_DIR`` so that their own staging does not
    touch the checkout. Inherited, that makes every git command here operate on
    the wrong repository -- and ``git worktree remove`` would then delete the
    wrong tree. The repository's own test fixture drops the same variables for
    exactly this reason.
    """
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git_output(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=_git_env(),
    )
    return result.stdout.strip()


def restore_working_tree(cwd: Path | None = None) -> bool:
    """Put the repository's working tree back if something made it bare.

    A repository without a working tree answers every ``git status`` with
    "this operation must be run in a work tree", so a stray ``core.bare`` in
    the shared config takes the whole checkout out of use -- and the hooks
    with it. A checkout that has a ``.git`` of its own was never meant to be
    bare, so that is what this restores. A genuinely bare repository (no
    ``.git`` of its own) is left alone.
    """
    bare = _git_output(["config", "--get", "core.bare"], cwd)
    if bare != "true":
        return False
    common = _git_output(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
    if not common:
        return False
    checkout = Path(common).parent
    if not (checkout / ".git").exists():
        # A bare repository has no checkout of its own; leave it bare.
        return False
    subprocess.run(
        ["git", "config", "core.bare", "false"],
        check=True,
        cwd=str(cwd) if cwd else None,
        env=_git_env(),
    )
    print(
        f"warning: {checkout} had lost its working tree (core.bare was true); restored it",
        file=sys.stderr,
    )
    return True


def remove_worktree(path: Path, *, cwd: Path | None = None) -> int:
    """Remove the worktree at *path*, dropping the links it holds first."""
    root = path if path.is_absolute() else Path.cwd() / path
    removed = unlink_links(root)
    for link in removed:
        print(f"unlinked {link}")
    if not removed:
        print(f"{root} holds no links")
    try:
        subprocess.run(
            ["git", "worktree", "remove", str(root)],
            check=True,
            cwd=str(cwd) if cwd else None,
            env=_git_env(),
        )
    except subprocess.CalledProcessError as exc:
        print(
            f"git refused to remove {root} (exit {exc.returncode}).\n"
            "Its links are already gone, so removing it by force is now safe:\n"
            f"  git worktree remove --force {root}",
            file=sys.stderr,
        )
        return exc.returncode or 1
    subprocess.run(
        ["git", "worktree", "prune"],
        check=False,
        cwd=str(cwd) if cwd else None,
        env=_git_env(),
    )
    restore_working_tree(cwd)
    print(f"removed {root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python scripts/remove_worktree.py <worktree-path>", file=sys.stderr)
        return 2
    return remove_worktree(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
