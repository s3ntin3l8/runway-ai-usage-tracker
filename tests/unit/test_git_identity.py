"""Tests for scripts/sidecar_pkg/git_identity.py — the repository name a working
directory belongs to.

The git binary is stubbed: these tests never touch a real repository, a network
or the user's own checkout.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from scripts.sidecar_pkg import git_identity


@pytest.fixture(autouse=True)
def _clear_cache():
    git_identity.clear_cache()
    yield
    git_identity.clear_cache()


class TestRepoNameFromUrl:
    def test_https_url(self):
        assert (
            git_identity.repo_name_from_url("https://github.com/owner/runway-ai-usage-tracker.git")
            == "runway-ai-usage-tracker"
        )

    def test_https_url_without_the_git_suffix(self):
        assert (
            git_identity.repo_name_from_url("https://github.com/owner/runway-ai-usage-tracker")
            == "runway-ai-usage-tracker"
        )

    def test_scp_like_url(self):
        assert (
            git_identity.repo_name_from_url("git@github.com:owner/runway-ai-usage-tracker.git")
            == "runway-ai-usage-tracker"
        )

    def test_ssh_url(self):
        assert (
            git_identity.repo_name_from_url("ssh://git@host/owner/runway-ai-usage-tracker.git")
            == "runway-ai-usage-tracker"
        )

    def test_url_with_credentials_keeps_only_the_name(self):
        name = git_identity.repo_name_from_url(
            "https://user:token@github.com/owner/repo.git"  # pragma: allowlist secret
        )
        assert name == "repo"
        assert "token" not in (name or "")
        assert "user" not in (name or "")

    def test_local_path_falls_back_to_its_folder(self):
        assert git_identity.repo_name_from_url("/home/u/runway-ai-usage-tracker") == (
            "runway-ai-usage-tracker"
        )

    def test_windows_path_falls_back_to_its_folder(self):
        assert git_identity.repo_name_from_url(r"D:\Projects\runway-ai-usage-tracker") == (
            "runway-ai-usage-tracker"
        )

    def test_trailing_slash(self):
        assert git_identity.repo_name_from_url("https://github.com/owner/repo/") == "repo"

    def test_empty_and_none(self):
        assert git_identity.repo_name_from_url(None) is None
        assert git_identity.repo_name_from_url("") is None
        assert git_identity.repo_name_from_url("   ") is None

    def test_a_name_that_is_only_dots_is_rejected(self):
        assert git_identity.repo_name_from_url("https://github.com/owner/..") is None

    def test_control_characters_are_stripped(self):
        assert git_identity.repo_name_from_url("https://github.com/owner/re\npo") == "repo"

    def test_an_overlong_name_is_capped(self):
        name = git_identity.repo_name_from_url(f"https://github.com/owner/{'x' * 500}")
        assert name is not None
        assert len(name) == 128


class TestResolveRepoName:
    def _stub(self, monkeypatch, *, top="/home/u/repo", remote="https://github.com/o/repo.git"):
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            calls.append(list(cmd))
            if "rev-parse" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=top + "\n", stderr="")
            if "config" in cmd:
                if remote is None:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout=remote + "\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        monkeypatch.setattr(git_identity.shutil, "which", lambda name: "/usr/bin/git")
        monkeypatch.setattr(git_identity.subprocess, "run", fake_run)
        monkeypatch.setattr(git_identity.os.path, "isdir", lambda path: True)
        return calls

    def test_the_remote_names_the_repository(self, monkeypatch):
        self._stub(monkeypatch)
        assert git_identity.resolve_repo_name("/home/u/repo") == "repo"

    def test_a_repository_without_a_remote_uses_the_root_name(self, monkeypatch):
        self._stub(monkeypatch, remote=None)
        assert git_identity.resolve_repo_name("/home/u/repo") == "repo"

    def test_a_directory_outside_a_repository_resolves_to_nothing(self, monkeypatch):
        def fake_run(cmd, **kwargs):  # noqa: ANN001
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="not a repository")

        monkeypatch.setattr(git_identity.shutil, "which", lambda name: "/usr/bin/git")
        monkeypatch.setattr(git_identity.subprocess, "run", fake_run)
        assert git_identity.resolve_repo_name("/tmp") is None

    def test_a_missing_git_resolves_to_nothing(self, monkeypatch):
        monkeypatch.setattr(git_identity.shutil, "which", lambda name: None)
        assert git_identity.resolve_repo_name("/home/u/repo") is None

    def test_a_missing_directory_resolves_to_nothing(self, monkeypatch):
        self._stub(monkeypatch)
        monkeypatch.setattr(git_identity.os.path, "isdir", lambda path: False)
        assert git_identity.resolve_repo_name("/home/u/repo") is None

    def test_the_result_is_cached(self, monkeypatch):
        calls = self._stub(monkeypatch)
        git_identity.resolve_repo_name("/home/u/repo")
        git_identity.resolve_repo_name("/home/u/repo")
        assert len([c for c in calls if "rev-parse" in c]) == 1

    def test_none_and_empty_resolve_to_nothing(self):
        assert git_identity.resolve_repo_name(None) is None
        assert git_identity.resolve_repo_name("") is None


class TestAnnotateProjectRefs:
    def test_events_get_the_repository_name(self, monkeypatch):
        monkeypatch.setattr(git_identity, "resolve_repo_name", lambda cwd: "repo")
        events: list[dict[str, Any]] = [
            {"cwd": "/home/u/repo"},
            {"cwd": "/home/u/other"},
        ]

        resolved = git_identity.annotate_project_refs(events)

        assert resolved == 2
        assert all(event["project_ref"] == "repo" for event in events)

    def test_an_event_without_a_working_directory_is_left_alone(self, monkeypatch):
        monkeypatch.setattr(git_identity, "resolve_repo_name", lambda cwd: "repo")
        events: list[dict[str, Any]] = [{"cwd": None}, {}]

        assert git_identity.annotate_project_refs(events) == 0
        assert all("project_ref" not in event for event in events)

    def test_an_event_outside_a_repository_keeps_its_own_label(self, monkeypatch):
        monkeypatch.setattr(git_identity, "resolve_repo_name", lambda cwd: None)
        events: list[dict[str, Any]] = [{"cwd": "/tmp"}]

        assert git_identity.annotate_project_refs(events) == 0
        assert "project_ref" not in events[0]
