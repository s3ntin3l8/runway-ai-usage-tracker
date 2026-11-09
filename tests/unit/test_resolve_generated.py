"""``resolve-generated`` against real, throwaway git repositories."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import dev_checks as dc

TABLE = "tests/shard_durations.linux.json"
BASELINE = ".secrets.baseline"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=check
    )


def table(files: dict[str, float]) -> str:
    return (
        json.dumps(
            {"generated_by": "x", "generated_at": "T", "total_seconds": 1.0, "files": files},
            indent=2,
        )
        + "\n"
    )


def baseline(line: int, extra: str = "") -> str:
    return "\n".join(["{", f'  "line_number": {line},', f'  "note": "{extra}"', "}"]) + "\n"


def commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    commit(
        root,
        "base",
        {
            TABLE: table({"tests/test_a.py": 1.0, "tests/test_b.py": 2.0, "tests/test_c.py": 3.0}),
            BASELINE: baseline(1),
            "tests/test_a.py": "",
            "tests/test_b.py": "",
            "tests/test_c.py": "",
            "notes.txt": "base\n",
        },
    )
    git(root, "checkout", "-b", "feature")
    git(root, "rm", "-q", "tests/test_b.py")
    commit(
        root,
        "branch",
        {
            TABLE: table({"tests/test_a.py": 1.0, "tests/test_c.py": 9.0}),
            BASELINE: baseline(2, "branch"),
        },
    )
    git(root, "checkout", "main")
    commit(
        root,
        "main",
        {
            TABLE: table({"tests/test_a.py": 5.0, "tests/test_b.py": 2.0, "tests/test_c.py": 3.0}),
            BASELINE: baseline(2, "main"),
        },
    )
    git(root, "checkout", "feature")
    return root


def run(repo: Path, calls: list[str]) -> int:
    def regen(root: Path) -> None:
        calls.append("regen")

    def secrets(root: Path) -> int:
        calls.append("secrets")
        return 0

    return dc.resolve_generated(repo, baseline_regenerator=regen, secrets_checker=secrets)


def unmerged(repo: Path) -> list[str]:
    return git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()


def test_rebase_takes_main_and_prunes_deleted_modules(repo, capsys):
    assert git(repo, "rebase", "main", check=False).returncode != 0
    head = git(repo, "rev-parse", "HEAD").stdout
    calls: list[str] = []
    assert run(repo, calls) == 0
    files = json.loads((repo / TABLE).read_text())["files"]
    assert files == {"tests/test_a.py": 5.0, "tests/test_c.py": 3.0}
    assert (repo / BASELINE).read_text() == baseline(2, "main")
    assert calls == ["regen", "secrets"]
    assert unmerged(repo) == []
    assert git(repo, "rev-parse", "HEAD").stdout == head
    assert "git rebase --continue" in capsys.readouterr().out


def test_merge_takes_theirs_which_is_main(repo):
    assert git(repo, "merge", "main", check=False).returncode != 0
    assert run(repo, []) == 0
    assert (repo / BASELINE).read_text() == baseline(2, "main")
    files = json.loads((repo / TABLE).read_text())["files"]
    assert files == {"tests/test_a.py": 5.0, "tests/test_c.py": 3.0}
    assert unmerged(repo) == []
    assert (repo / ".git" / "MERGE_HEAD").exists()


def test_other_conflicts_are_left_alone(repo, capsys):
    git(repo, "checkout", "main")
    commit(repo, "main notes", {"notes.txt": "main\n"})
    git(repo, "checkout", "feature")
    commit(repo, "feature notes", {"notes.txt": "feature\n"})
    # One commit, so the rebase stops once with all three files conflicted.
    git(repo, "reset", "--soft", "HEAD~2")
    git(repo, "commit", "-m", "squashed")
    assert git(repo, "rebase", "main", check=False).returncode != 0
    assert run(repo, []) == 1
    assert unmerged(repo) == ["notes.txt"]
    assert "notes.txt" in capsys.readouterr().out
    assert "<<<<<<<" in (repo / "notes.txt").read_text()


def test_outside_rebase_or_merge_changes_nothing(repo, capsys):
    before = git(repo, "status", "--porcelain").stdout
    head = git(repo, "rev-parse", "HEAD").stdout
    calls: list[str] = []
    assert run(repo, calls) != 0
    assert calls == []
    assert git(repo, "status", "--porcelain").stdout == before
    assert git(repo, "rev-parse", "HEAD").stdout == head
    assert "no rebase or merge" in capsys.readouterr().out
