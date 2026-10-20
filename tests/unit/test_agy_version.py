"""Unit tests for the sidecar's report of the installed Antigravity CLI version.

The Code Assist API decides *which quota pools* it serves on the User-Agent's
version — a stale version is answered with 200 and a subset of the pools, so a
quota bucket disappears from the dashboard without any error (#687). The CLI
lives on the sidecar's host and the server never sees it, so the sidecar reads
the version and reports it next to the token.
"""

from scripts.sidecar_pkg import agy_version


def _fake_cli(tmp_path, output: str):
    """A stand-in for the agy binary that prints *output*."""
    import os
    from pathlib import Path

    if os.name == "nt":
        script = Path(tmp_path) / "agy.bat"
        script.write_text(f"@echo off\r\necho {output}\r\n", encoding="utf-8")
    else:
        script = Path(tmp_path) / "agy"
        script.write_text(f"#!/bin/sh\necho {output}\n", encoding="utf-8")
        script.chmod(0o755)
    return script


class TestParseVersion:
    def test_accepts_a_bare_version(self):
        assert agy_version.parse_version("1.4.3") == "1.4.3"

    def test_accepts_a_v_prefix_and_surrounding_whitespace(self):
        assert agy_version.parse_version(" v1.4.3\n") == "1.4.3"

    def test_accepts_a_two_part_version(self):
        assert agy_version.parse_version("1.4") == "1.4"

    def test_rejects_anything_that_is_not_a_version(self):
        # The value ends up in an HTTP header: it must never carry text.
        for bad in (
            "",
            None,
            "unknown",
            "1.4.3-beta",
            "1.4.3\r\nX-Injected: 1",
            "1..3",
            "v",
            "1.4.3 1.4.4",
            # Unicode decimals: httpx encodes headers as ASCII.
            "1.٤.٩",
            "١.٢.٣",
            # More than three parts — the shared validator rejects it.
            "1.4.3.2",
        ):
            assert agy_version.parse_version(bad) is None


class TestInstalledVersion:
    def test_reads_the_version_from_the_cli(self, tmp_path, monkeypatch):
        """The real path: run the CLI and take what it prints."""
        binary = _fake_cli(tmp_path, "1.5.0")
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: str(binary))
        monkeypatch.setattr(agy_version._cache, "value", None)

        assert agy_version.installed_version() == "1.5.0"

    def test_a_cli_that_prints_something_else_reports_no_version(self, tmp_path, monkeypatch):
        binary = _fake_cli(tmp_path, "unknown build")
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: str(binary))
        monkeypatch.setattr(agy_version._cache, "value", None)

        assert agy_version.installed_version() is None

    def test_reports_the_installed_version(self, monkeypatch):
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version, "_read_version", lambda _binary: "1.4.3")
        monkeypatch.setattr(agy_version._cache, "value", None)

        assert agy_version.installed_version() == "1.4.3"

    def test_caches_the_answer(self, monkeypatch):
        """Reading the version costs a subprocess call — do it once, not per cycle."""
        calls: list[str] = []

        def fake_read(binary: str) -> str:
            calls.append(binary)
            return "1.4.3"

        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version, "_read_version", fake_read)
        monkeypatch.setattr(agy_version._cache, "value", None)

        assert agy_version.installed_version() == "1.4.3"
        assert agy_version.installed_version() == "1.4.3"
        assert len(calls) == 1

    def test_re_reads_after_the_cache_expired(self, monkeypatch):
        """An agy update must be picked up without restarting the sidecar."""
        calls: list[str] = []

        def fake_read(binary: str) -> str:
            calls.append(binary)
            return "1.4.3"

        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version, "_read_version", fake_read)
        monkeypatch.setattr(agy_version._cache, "value", None)

        agy_version.installed_version(now=1000.0)
        agy_version.installed_version(now=1000.0 + agy_version._CACHE_TTL_SECONDS + 1)
        assert len(calls) == 2

    def test_unknown_when_agy_is_not_installed(self, monkeypatch):
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: None)
        monkeypatch.setattr(agy_version._cache, "value", None)

        assert agy_version.installed_version() is None

    def test_unknown_when_the_cli_does_not_answer(self, monkeypatch):
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version, "_read_version", lambda _binary: None)
        monkeypatch.setattr(agy_version._cache, "value", None)

        assert agy_version.installed_version() is None

    def test_unknown_when_the_cli_cannot_be_run(self, monkeypatch):
        """A binary that cannot be executed is an unknown version, not a crash."""
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version._cache, "value", None)

        def boom(*_args, **_kwargs):
            raise OSError("cannot execute")

        monkeypatch.setattr(agy_version.subprocess, "run", boom)
        assert agy_version.installed_version() is None

    def test_unknown_when_the_cli_exits_non_zero(self, monkeypatch):
        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version._cache, "value", None)

        class _Result:
            returncode = 1
            stdout = ""
            stderr = "boom"

        monkeypatch.setattr(agy_version.subprocess, "run", lambda *_a, **_kw: _Result())
        assert agy_version.installed_version() is None

    def test_concurrent_reads_spawn_the_cli_once(self, monkeypatch):
        """The sidecar collects from a thread while the tray may refresh: a cold
        cache must not start the CLI twice."""
        import threading

        calls: list[str] = []

        def slow_read(binary: str) -> str:
            calls.append(binary)
            threading.Event().wait(0.05)
            return "1.4.3"

        monkeypatch.setattr(agy_version, "resolve_agy", lambda: "/usr/bin/agy")
        monkeypatch.setattr(agy_version, "_read_version", slow_read)
        monkeypatch.setattr(agy_version._cache, "value", None)

        results: list[str | None] = []
        threads = [
            threading.Thread(target=lambda: results.append(agy_version.installed_version()))
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert results == ["1.4.3"] * 4
        assert len(calls) == 1


class TestResolveAgy:
    def test_prefers_the_path(self, monkeypatch):
        monkeypatch.setattr(agy_version.shutil, "which", lambda _name: "/usr/bin/agy")
        assert agy_version.resolve_agy() == "/usr/bin/agy"

    def test_finds_the_windows_install_without_it_on_the_path(self, monkeypatch, tmp_path):
        """The Windows installer puts the CLI outside PATH — look there too."""
        monkeypatch.setattr(agy_version.shutil, "which", lambda _name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        # A host with agy installed under ~ would otherwise win this lookup.
        monkeypatch.setattr(agy_version.Path, "home", staticmethod(lambda: tmp_path))
        binary = tmp_path / "agy" / "bin" / "agy.exe"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"")

        assert agy_version.resolve_agy() == str(binary)

    def test_none_when_nothing_is_installed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(agy_version.shutil, "which", lambda _name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(agy_version.Path, "home", staticmethod(lambda: tmp_path))

        assert agy_version.resolve_agy() is None

    def test_a_candidate_that_cannot_be_read_is_skipped(self, monkeypatch, tmp_path):
        """One unreadable candidate must not hide the others."""
        monkeypatch.setattr(agy_version.shutil, "which", lambda _name: None)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(agy_version.Path, "home", staticmethod(lambda: tmp_path))
        real_is_file = agy_version.Path.is_file

        def flaky_is_file(self):
            if self.name == "agy":
                raise OSError("permission denied")
            return real_is_file(self)

        monkeypatch.setattr(agy_version.Path, "is_file", flaky_is_file)
        assert agy_version.resolve_agy() is None
