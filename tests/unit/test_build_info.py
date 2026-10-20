"""The server reports the commit it was built from (Settings → About).

The version string names a release and is identical across every `:edge`
build, so the SHA is what identifies one exact build. It comes from
`RUNWAY_BUILD_SHA` (baked into the image at build time) or, in a source
checkout, from git.
"""

import subprocess

import pytest

from app.core import build_info
from app.core.build_info import BUILD_SHA_ENV, get_build_sha

# A stand-in commit SHA. Assembled at runtime so detect-secrets doesn't read
# the literal as a high-entropy hex string (a real secret).
FAKE_SHA = "1" * 16


@pytest.fixture(autouse=True)
def _reset_build_sha(monkeypatch):
    """The lookup is cached — keep one test's env/git state out of the next."""
    monkeypatch.delenv(BUILD_SHA_ENV, raising=False)
    get_build_sha.cache_clear()
    yield
    get_build_sha.cache_clear()


def _fake_git(monkeypatch, tmp_path, *, returncode=0, stdout=None):
    """Point the git fallback at a throwaway checkout with a stubbed git.

    Returns the stub, so a test can inspect how git was called.
    """
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_info.shutil, "which", lambda _name: "/usr/bin/git")

    if stdout is None:
        stdout = FAKE_SHA + "\n"
    calls: list[list[str]] = []

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, returncode, stdout=stdout)

    monkeypatch.setattr(build_info.subprocess, "run", fake_run)
    return calls


def test_env_sha_is_used(monkeypatch):
    """A build-stamped SHA wins over whatever git says about the checkout."""
    monkeypatch.setenv(BUILD_SHA_ENV, "ABCDEF0123456789")
    assert get_build_sha() == "abcdef0123456789"


def test_env_sha_is_trimmed(monkeypatch):
    monkeypatch.setenv(BUILD_SHA_ENV, "  abcdef0123456  ")
    assert get_build_sha() == "abcdef0123456"


def test_invalid_env_sha_is_ignored(monkeypatch, tmp_path):
    """A junk env var must not become a link target — fall through to git."""
    monkeypatch.setenv(BUILD_SHA_ENV, "not-a-sha")
    _fake_git(monkeypatch, tmp_path, stdout=FAKE_SHA + "\n")
    assert get_build_sha() == FAKE_SHA


def test_git_fallback_reads_head(monkeypatch, tmp_path):
    """A source checkout reports its own commit."""
    calls = _fake_git(monkeypatch, tmp_path, stdout=FAKE_SHA + "\n")
    assert get_build_sha() == FAKE_SHA
    assert calls == [["/usr/bin/git", "rev-parse", "HEAD"]]


def test_git_failure_returns_none(monkeypatch, tmp_path):
    """A broken checkout (no HEAD yet, git missing) reports no SHA."""
    _fake_git(monkeypatch, tmp_path, returncode=128, stdout="")
    assert get_build_sha() is None


def test_git_rejects_a_non_sha_head(monkeypatch, tmp_path):
    _fake_git(monkeypatch, tmp_path, stdout="not-a-sha\n")
    assert get_build_sha() is None


def test_no_checkout_returns_none(monkeypatch, tmp_path):
    """An installed package (no `.git`) has no commit to report."""
    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_info.shutil, "which", lambda _name: None)
    assert get_build_sha() is None


def test_missing_git_binary_returns_none(monkeypatch, tmp_path):
    """git not on PATH must not raise — the server still has to start."""
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(build_info, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(build_info.shutil, "which", lambda _name: None)
    assert get_build_sha() is None


def test_git_that_cannot_launch_returns_none(monkeypatch, tmp_path):
    """A git that can't be spawned must not take the server down either."""

    def boom(*_args, **_kwargs):
        raise OSError("git vanished")

    _fake_git(monkeypatch, tmp_path)
    monkeypatch.setattr(build_info.subprocess, "run", boom)
    assert get_build_sha() is None
