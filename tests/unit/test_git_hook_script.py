"""The git hook must not depend on a path that can disappear.

The hooks live in the repository's shared ``.git/hooks``, so every worktree
writes to the same two files. ``pre-commit install`` writes a script that
pins the interpreter that ran it, so installing the hooks from a worktree
breaks every commit in every worktree the moment that worktree is removed --
which is exactly what happened here.

This repository installs its own hook script instead, and that script
resolves its interpreter when it runs. These tests pin the two halves of
that guarantee: the script must not bake in a path, and it must hand
pre-commit the right arguments.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = ROOT / "scripts" / "hooks" / "git-hook.sh"
DEV_CHECKS = ROOT / "scripts" / "dev_checks.py"


@pytest.fixture(scope="module")
def hook_source() -> str:
    if not HOOK_SCRIPT.is_file():
        pytest.fail(f"{HOOK_SCRIPT} is missing")
    return HOOK_SCRIPT.read_text(encoding="utf-8")


def test_the_hook_script_exists(hook_source: str) -> None:
    """The script is tracked, so a fix to it can be reviewed and merged."""
    assert hook_source.strip(), "the hook script is empty"


def test_the_hook_script_bakes_in_no_path(hook_source: str) -> None:
    """No absolute path may appear in it.

    This is the invariant that makes the breakage unreachable: whatever the
    script names has to exist at *run* time, never only at install time.
    """
    assert not re.search(r"[A-Za-z]:[\\/]", hook_source), "a drive-letter path"
    assert "/home/" not in hook_source, "a POSIX home path"
    assert "/Users/" not in hook_source, "a macOS home path"
    assert ".wt/" not in hook_source, "a worktree path"
    assert "INSTALL_PYTHON" not in hook_source, "a pinned interpreter"


def test_the_hook_type_comes_from_the_script_name(hook_source: str) -> None:
    """One script serves both hooks, so the name has to be read at run time."""
    assert "basename" in hook_source
    assert '--hook-type="$type"' in hook_source


def test_the_hook_resolves_its_interpreter_at_run_time(hook_source: str) -> None:
    """The candidates are the worktree's venv and the main checkout's."""
    assert "$root/.venv" in hook_source, "the worktree's own venv"
    assert "$main/.venv" in hook_source, "the main checkout's venv"
    assert "import pre_commit" in hook_source, "the candidate is probed"
    assert "--git-common-dir" in hook_source, "the main checkout is derived"


def test_the_hook_runs_pre_commit_the_way_pre_commit_generates_it(
    hook_source: str,
) -> None:
    """pre-commit's own entry point, with the same arguments it generates."""
    assert "hook-impl" in hook_source
    assert "--config=" in hook_source
    assert "--hook-type=" in hook_source
    assert "--hook-dir" in hook_source


def test_the_installer_installs_the_tracked_script() -> None:
    """``install-hooks`` must not run ``pre-commit install``.

    That command writes a script with the interpreter baked in, which is the
    whole defect.
    """
    source = DEV_CHECKS.read_text(encoding="utf-8")
    assert "HOOK_SCRIPT" in source
    assert "install_git_hooks" in source
    assert 'pre_commit", "install' not in source
    assert '"pre_commit", "install"' not in source


def test_the_installed_hook_calls_pre_commit_with_the_right_arguments(
    tmp_path: Path,
) -> None:
    """Run the hook against a stub interpreter and read what it was called with.

    This proves the type derivation, the resolution order and the argument
    shape without running the real pre-commit.
    """
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("sh is not on PATH")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

    # A stub interpreter: it answers the "import pre_commit" probe, and
    # records every other invocation.
    venv = repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    stub = venv / "python"
    stub.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "%s\\n" "$*" >> "$STUB_LOG"\n',
        encoding="utf-8",
        newline="\n",
    )
    stub.chmod(0o755)

    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    installed = hooks / "pre-commit"
    installed.write_text(HOOK_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    installed.chmod(0o755)

    log = tmp_path / "stub.log"
    completed = subprocess.run(
        [sh, str(installed)],
        cwd=repo,
        env={**os.environ, "STUB_LOG": str(log)},
        capture_output=True,
        text=True,
        check=False,
    )
    recorded = log.read_text(encoding="utf-8") if log.exists() else ""
    assert recorded, f"the stub was never called: {completed.stderr}"
    assert "-mpre_commit" in recorded
    assert "hook-impl" in recorded
    assert "--hook-type=pre-commit" in recorded
    assert "--config=" in recorded
    assert "--hook-dir" in recorded
