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


def _git_available() -> bool:
    return git_identity.shutil.which("git") is not None


def _make_repo(path, remote="https://example.invalid/owner/fake-repo.git"):
    path.mkdir(parents=True)
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    import os

    full_env = {**os.environ, **env}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=full_env)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote], check=True, env=full_env
    )


class TestPersistentCache:
    """The directory -> repository cache survives restarts and deleted directories."""

    def _fake_resolve(self, monkeypatch, answers: dict[str, str | None]):
        calls: list[str] = []

        def fake(cwd: str) -> str | None:
            calls.append(cwd)
            return answers.get(cwd)

        monkeypatch.setattr(git_identity, "_resolve", fake)
        return calls

    def _restart(self, path):
        git_identity.clear_cache()  # what a new process starts from
        git_identity.configure(path)

    def test_a_resolution_survives_a_restart(self, monkeypatch, tmp_path):
        state = tmp_path / "git-identity.json"
        git_identity.configure(state)
        self._fake_resolve(monkeypatch, {"/fake/a": "repo-a"})
        events: list[dict[str, Any]] = [{"cwd": "/fake/a"}]
        git_identity.annotate_project_refs(events)
        assert state.exists()

        self._restart(state)
        self._fake_resolve(monkeypatch, {})  # the directory is gone now
        later: list[dict[str, Any]] = [{"cwd": "/fake/a"}]
        git_identity.annotate_project_refs(later)

        assert later[0]["project_ref"] == "repo-a"

    def test_a_live_answer_replaces_the_persisted_one(self, monkeypatch, tmp_path):
        state = tmp_path / "git-identity.json"
        git_identity.configure(state)
        self._fake_resolve(monkeypatch, {"/fake/a": "old-name"})
        git_identity.resolve_repo_name("/fake/a")
        git_identity.save()

        self._restart(state)
        self._fake_resolve(monkeypatch, {"/fake/a": "new-name"})
        assert git_identity.resolve_repo_name("/fake/a") == "new-name"

    def test_none_results_are_not_persisted(self, monkeypatch, tmp_path):
        state = tmp_path / "git-identity.json"
        git_identity.configure(state)
        self._fake_resolve(monkeypatch, {"/fake/a": "repo-a", "/fake/none": None})
        git_identity.annotate_project_refs([{"cwd": "/fake/a"}, {"cwd": "/fake/none"}])

        import json

        entries = json.loads(state.read_text())["entries"]
        assert entries == {"/fake/a": "repo-a"}

    def test_a_none_stays_cached_in_memory_only(self, monkeypatch, tmp_path):
        git_identity.configure(tmp_path / "git-identity.json")
        calls = self._fake_resolve(monkeypatch, {})
        git_identity.resolve_repo_name("/fake/none")
        git_identity.resolve_repo_name("/fake/none")
        assert calls == ["/fake/none"]

    def test_a_missing_file_is_an_empty_cache(self, tmp_path):
        git_identity.configure(tmp_path / "absent" / "git-identity.json")
        assert git_identity._persisted == {}

    @pytest.mark.parametrize(
        "content",
        ["not json", "[]", '{"entries": []}', '{"entries": {"/x": 5, "": "y", "/ok": "fine"}}'],
    )
    def test_a_corrupt_file_is_tolerated(self, tmp_path, content):
        state = tmp_path / "git-identity.json"
        state.write_text(content)
        git_identity.configure(state)
        assert set(git_identity._persisted) <= {"/ok"}

    def test_the_file_stays_bounded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(git_identity, "_PERSIST_LIMIT", 3)
        state = tmp_path / "git-identity.json"
        git_identity.configure(state)
        self._fake_resolve(monkeypatch, {f"/fake/{i}": f"repo-{i}" for i in range(10)})
        git_identity.annotate_project_refs([{"cwd": f"/fake/{i}"} for i in range(10)])

        import json

        entries = json.loads(state.read_text())["entries"]
        assert list(entries) == ["/fake/7", "/fake/8", "/fake/9"]

    def test_an_oversized_file_is_trimmed_on_load(self, monkeypatch, tmp_path):
        monkeypatch.setattr(git_identity, "_PERSIST_LIMIT", 2)
        import json

        state = tmp_path / "git-identity.json"
        state.write_text(json.dumps({"entries": {f"/fake/{i}": "r" for i in range(5)}}))
        git_identity.configure(state)
        assert list(git_identity._persisted) == ["/fake/3", "/fake/4"]

    def test_an_unwritable_state_path_does_not_raise(self, monkeypatch, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory")
        git_identity.configure(blocker / "git-identity.json")
        self._fake_resolve(monkeypatch, {"/fake/a": "repo-a"})
        events: list[dict[str, Any]] = [{"cwd": "/fake/a"}]
        assert git_identity.annotate_project_refs(events) == 1

    def test_no_temporary_file_is_left_behind(self, monkeypatch, tmp_path):
        git_identity.configure(tmp_path / "git-identity.json")
        self._fake_resolve(monkeypatch, {"/fake/a": "repo-a"})
        git_identity.annotate_project_refs([{"cwd": "/fake/a"}])
        assert [p.name for p in tmp_path.iterdir()] == ["git-identity.json"]

    def test_an_unchanged_cache_is_not_rewritten(self, monkeypatch, tmp_path):
        state = tmp_path / "git-identity.json"
        git_identity.configure(state)
        self._fake_resolve(monkeypatch, {"/fake/a": "repo-a"})
        git_identity.annotate_project_refs([{"cwd": "/fake/a"}])
        state.unlink()
        git_identity.annotate_project_refs([{"cwd": "/fake/a"}])
        assert not state.exists()

    @pytest.mark.skipif(not _git_available(), reason="git is not installed")
    def test_a_real_checkout_keeps_its_name_after_it_is_removed(self, tmp_path):
        import shutil

        repo = tmp_path / "some-folder"
        _make_repo(repo)
        state = tmp_path / "state" / "git-identity.json"
        git_identity.configure(state)
        events: list[dict[str, Any]] = [{"cwd": str(repo)}]
        git_identity.annotate_project_refs(events)
        assert events[0]["project_ref"] == "fake-repo"

        shutil.rmtree(repo)
        self._restart(state)
        later: list[dict[str, Any]] = [{"cwd": str(repo)}]
        git_identity.annotate_project_refs(later)
        assert later[0]["project_ref"] == "fake-repo"

    @pytest.mark.skipif(not _git_available(), reason="git is not installed")
    def test_a_directory_never_resolved_stays_unresolved_once_removed(self, tmp_path):
        git_identity.configure(tmp_path / "git-identity.json")
        events: list[dict[str, Any]] = [{"cwd": str(tmp_path / "never-existed")}]
        assert git_identity.annotate_project_refs(events) == 0


@pytest.mark.skipif(not _git_available(), reason="git is not installed")
def test_a_collection_cycle_annotates_events_and_persists_the_cache(tmp_path, monkeypatch):
    """The cycle wires the state file: an event from a git directory leaves the sidecar
    with its repository name, and the directory is remembered under the sidecar dir."""
    from scripts import sidecar

    repo = tmp_path / "checkout"
    _make_repo(repo)
    monkeypatch.setenv("RUNWAY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(sidecar.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sidecar.GenericCollector, "collect_provider", lambda *a, **kw: ([], []))
    monkeypatch.setattr(sidecar, "_post_credential_manifest", lambda **kw: None)
    monkeypatch.setattr(sidecar, "custom_login_dirs", lambda *a: [])

    def fake_extract(**kw):  # noqa: ANN003
        kw["out_events"].append(
            {"event_id": "e1", "provider_id": "anthropic", "cwd": str(repo), "account_source": "x"}
        )
        return 0

    monkeypatch.setattr(sidecar, "_extract_events_for_provider", fake_extract)

    result = sidecar.run_collection({"providers": ["anthropic"]}, ["anthropic"])

    assert [e.get("project_ref") for e in result.events] == ["fake-repo"]
    assert (tmp_path / "config" / "sidecar" / "git-identity.json").exists()
