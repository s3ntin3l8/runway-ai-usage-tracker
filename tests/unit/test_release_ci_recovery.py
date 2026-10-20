"""Release-only network failures must recover narrowly and remain fail closed."""

import subprocess
from pathlib import Path

import pytest

from scripts import prepare_nsis, sign_release_assets


@pytest.mark.parametrize("failures", [1, 3])
def test_rekor_conflict_retries_with_a_fresh_bundle(tmp_path, monkeypatch, failures):
    asset = tmp_path / "Runway-Sidecar-macOS-edge.zip"
    asset.write_bytes(b"payload")
    bundle = Path(f"{asset}.sigstore.json")
    attempts = []

    def run(command, **kwargs):
        assert command == ["cosign", "sign-blob", "--yes", "--bundle", str(bundle), str(asset)]
        assert not bundle.exists()
        attempts.append(command)
        bundle.write_text("partial" if len(attempts) <= failures else "signed")
        code = int(len(attempts) <= failures)
        return subprocess.CompletedProcess(command, code, "", "[409] createLogEntryConflict")

    monkeypatch.setattr(sign_release_assets.subprocess, "run", run)
    monkeypatch.setattr(sign_release_assets.time, "sleep", lambda _: None)
    if failures == 3:
        with pytest.raises(RuntimeError, match="signing failed"):
            sign_release_assets.sign_asset(asset)
        assert not bundle.exists()
        assert len(attempts) == 3
    else:
        sign_release_assets.sign_asset(asset)
        assert bundle.read_text() == "signed"
        assert len(attempts) == 2


def test_signing_does_not_retry_unrelated_failure(tmp_path, monkeypatch):
    asset = tmp_path / "SHA256SUMS.txt"
    asset.write_text("checksums")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "OIDC permission denied")

    monkeypatch.setattr(sign_release_assets.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="signing failed"):
        sign_release_assets.sign_asset(asset)
    assert len(calls) == 1


def test_signing_timeout_removes_partial_bundle(tmp_path, monkeypatch):
    asset = tmp_path / "SHA256SUMS.txt"
    asset.touch()
    bundle = Path(f"{asset}.sigstore.json")

    def run(command, **kwargs):
        bundle.write_text("partial")
        raise subprocess.TimeoutExpired(command, 180)

    monkeypatch.setattr(sign_release_assets.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        sign_release_assets.sign_asset(asset)
    assert not bundle.exists()


def test_success_without_bundle_is_not_accepted(tmp_path, monkeypatch):
    asset = tmp_path / "SHA256SUMS.txt"
    asset.touch()
    monkeypatch.setattr(
        sign_release_assets.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="signing failed"):
        sign_release_assets.sign_asset(asset)


@pytest.mark.parametrize("succeeds", [True, False])
def test_nsis_install_recovers_from_feed_failure_or_stops(tmp_path, monkeypatch, succeeds):
    compiler = tmp_path / "NSIS" / "makensis.exe"
    attempts = []
    monkeypatch.setattr(prepare_nsis.shutil, "which", lambda _: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(prepare_nsis.time, "sleep", lambda _: None)

    def run(command, **kwargs):
        assert command == ["choco", "install", "nsis", "-y", "--no-progress"]
        attempts.append(command)
        if succeeds and len(attempts) == 2:
            compiler.parent.mkdir()
            compiler.write_bytes(b"compiler")
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(prepare_nsis.subprocess, "run", run)
    if succeeds:
        assert prepare_nsis.prepare() == compiler
        assert len(attempts) == 2
    else:
        with pytest.raises(RuntimeError, match="NSIS installation failed"):
            prepare_nsis.prepare()
        assert len(attempts) == 3


def test_installed_nsis_outside_path_does_not_contact_feed(tmp_path, monkeypatch):
    compiler = tmp_path / "NSIS" / "makensis.exe"
    compiler.parent.mkdir()
    compiler.touch()
    monkeypatch.setattr(prepare_nsis.shutil, "which", lambda _: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(
        prepare_nsis.subprocess, "run", lambda *a, **kw: pytest.fail("unnecessary feed access")
    )
    assert prepare_nsis.prepare() == compiler


def test_nsis_installed_despite_feed_error_is_usable(tmp_path, monkeypatch):
    compiler = tmp_path / "NSIS" / "makensis.exe"
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(prepare_nsis.shutil, "which", lambda _: None)

    def run(command, **kwargs):
        assert not compiler.exists(), "must not install again once compiler exists"
        compiler.parent.mkdir()
        compiler.touch()
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(prepare_nsis.subprocess, "run", run)
    monkeypatch.setattr(prepare_nsis.time, "sleep", lambda _: None)
    assert prepare_nsis.prepare() == compiler


def test_nsis_missing_chocolatey_explains_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(prepare_nsis.shutil, "which", lambda _: None)

    def run(command, **kwargs):
        raise FileNotFoundError("choco")

    monkeypatch.setattr(prepare_nsis.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Chocolatey is unavailable"):
        prepare_nsis.prepare()


@pytest.mark.parametrize("actions", [True, False])
def test_nsis_entrypoint_exports_compiler_only_in_actions(tmp_path, monkeypatch, actions):
    compiler = tmp_path / "NSIS" / "makensis.exe"
    compiler.parent.mkdir()
    compiler.touch()
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path))
    monkeypatch.setattr(prepare_nsis.shutil, "which", lambda _: None)
    output = tmp_path / "github-path"
    if actions:
        monkeypatch.setenv("GITHUB_PATH", str(output))
    else:
        monkeypatch.delenv("GITHUB_PATH", raising=False)
    prepare_nsis.main()
    if actions:
        assert output.read_text() == f"{compiler.parent}\n"
    else:
        assert not output.exists()
