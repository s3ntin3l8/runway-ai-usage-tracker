"""Unit tests for the sidecar self-update apply layer (scripts/sidecar_pkg/self_update.py).

Only the *pure* parts are tested here: asset-name resolution, checksum verify,
release-asset URL lookup, target detection, and the frozen/single-flight guards.
The OS-mutating ``apply_update`` swaps the running binary and is intentionally
NOT unit-tested — it is verified manually per platform during release QA.
"""

import hashlib
import io
import sys
import tarfile
import zipfile

import pytest

from scripts.sidecar_pkg import self_update
from scripts.sidecar_pkg.self_update import (
    SelfUpdateError,
    SelfUpdateUnsupportedError,
    _extract,
    find_asset_urls,
    resolve_asset_name,
    verify_sha256,
)

# ---------------------------------------------------------------------------
# resolve_asset_name
# ---------------------------------------------------------------------------


class TestResolveAssetName:
    def test_macos_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert resolve_asset_name("tray", "stable", "1.2.0") == "Runway-Sidecar-macOS-1.2.0.zip"

    def test_windows_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolve_asset_name("tray", "stable", "v1.2.0") == "Runway-Sidecar-Windows-1.2.0.zip"

    def test_linux_tray_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_asset_name("tray", "stable", "1.2.0") == "Runway-Sidecar-Linux-1.2.0.tar.gz"

    def test_linux_cli_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert (
            resolve_asset_name("cli", "stable", "1.2.0") == "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz"
        )

    def test_linux_tray_edge(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_asset_name("tray", "edge", None) == "Runway-Sidecar-Linux-edge.tar.gz"

    def test_linux_cli_edge(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_asset_name("cli", "edge", None) == "Runway-Sidecar-Linux-CLI-edge.tar.gz"

    def test_macos_edge_unsupported(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        with pytest.raises(SelfUpdateUnsupportedError):
            resolve_asset_name("tray", "edge", None)

    def test_windows_edge_unsupported(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        with pytest.raises(SelfUpdateUnsupportedError):
            resolve_asset_name("tray", "edge", None)

    def test_stable_missing_version(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SelfUpdateError):
            resolve_asset_name("cli", "stable", "")


# ---------------------------------------------------------------------------
# find_asset_urls
# ---------------------------------------------------------------------------


class TestFindAssetUrls:
    def _release(self):
        return {
            "assets": [
                {"name": "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz", "browser_download_url": "u/bin"},
                {
                    "name": "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz.sha256",
                    "browser_download_url": "u/sha",
                },
            ]
        }

    def test_returns_both_urls(self):
        asset, sha = find_asset_urls(self._release(), "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz")
        assert asset == "u/bin"
        assert sha == "u/sha"

    def test_raises_when_asset_missing(self):
        with pytest.raises(SelfUpdateError):
            find_asset_urls(self._release(), "Runway-Sidecar-macOS-9.9.9.zip")

    def test_raises_when_checksum_missing(self):
        release = {
            "assets": [
                {"name": "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz", "browser_download_url": "u/bin"},
            ]
        }
        with pytest.raises(SelfUpdateError):
            find_asset_urls(release, "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz")


# ---------------------------------------------------------------------------
# verify_sha256
# ---------------------------------------------------------------------------


class TestVerifySha256:
    def test_matching_hash(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"hello runway")
        digest = hashlib.sha256(b"hello runway").hexdigest()
        assert verify_sha256(f, digest) is True
        assert verify_sha256(f, digest.upper()) is True  # case-insensitive

    def test_mismatched_hash(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"hello runway")
        wrong = hashlib.sha256(b"tampered").hexdigest()
        assert verify_sha256(f, wrong) is False

    def test_empty_expected_is_false(self, tmp_path):
        f = tmp_path / "blob"
        f.write_bytes(b"x")
        assert verify_sha256(f, "") is False


# ---------------------------------------------------------------------------
# _detect_target
# ---------------------------------------------------------------------------


class TestDetectTarget:
    def test_cli_binary(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/opt/runway/runway-sidecar-cli")
        assert self_update._detect_target() == "cli"

    def test_tray_binary(self, monkeypatch):
        monkeypatch.setattr(sys, "executable", "/Applications/RunwaySidecar")
        assert self_update._detect_target() == "tray"


# ---------------------------------------------------------------------------
# self_update guards
# ---------------------------------------------------------------------------


class TestSelfUpdateGuards:
    def test_noop_when_not_frozen(self, monkeypatch):
        # sys.frozen is unset under pytest; assert the network is never touched.
        monkeypatch.setattr(self_update, "_is_frozen", lambda: False)
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("network must not be hit when not frozen")

        monkeypatch.setattr(self_update.request, "urlopen", _boom)
        assert self_update.self_update("1.1.0", None) is False
        assert called["n"] == 0

    def test_noop_in_docker(self, monkeypatch):
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_is_docker", lambda: True)
        monkeypatch.setattr(
            self_update.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network in docker")),
        )
        assert self_update.self_update("1.1.0", None) is False

    def test_noop_when_already_current(self, monkeypatch):
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        # check_once returning None => up to date; nothing downloaded.
        monkeypatch.setattr(self_update, "check_once", lambda *a, **k: None)
        assert self_update.self_update("1.1.0", "stable") is False

    def test_single_flight_blocks_second_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
        # Pre-create the lock file so _single_flight cannot acquire it.
        (tmp_path / self_update._LOCK_NAME).write_text("")
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        monkeypatch.setattr(
            self_update.request,
            "urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("lock held; no work")),
        )
        assert self_update.self_update("1.1.0", "stable") is False


# ---------------------------------------------------------------------------
# _extract — path-traversal hardening (CodeQL py/unsafe-unpacking)
# ---------------------------------------------------------------------------


class TestExtractSafety:
    def _make_tar(self, path, members):
        with tarfile.open(path, "w:gz") as tf:
            for name in members:
                data = b"x"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

    def _make_zip(self, path, members):
        with zipfile.ZipFile(path, "w") as zf:
            for name in members:
                zf.writestr(name, "x")

    def test_tar_extracts_safe_members(self, tmp_path):
        archive = tmp_path / "ok.tar.gz"
        self._make_tar(archive, ["runway-sidecar-cli"])
        dest = tmp_path / "out"
        dest.mkdir()
        _extract(archive, dest)
        assert (dest / "runway-sidecar-cli").is_file()

    def test_tar_rejects_traversal(self, tmp_path):
        archive = tmp_path / "evil.tar.gz"
        self._make_tar(archive, ["../evil"])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(tarfile.TarError):  # data filter raises a FilterError
            _extract(archive, dest)
        assert not (tmp_path / "evil").exists()  # nothing escaped dest

    def test_zip_extracts_safe_members(self, tmp_path):
        archive = tmp_path / "ok.zip"
        self._make_zip(archive, ["RunwaySidecar"])
        dest = tmp_path / "out"
        dest.mkdir()
        _extract(archive, dest)
        assert (dest / "RunwaySidecar").is_file()

    def test_zip_rejects_traversal(self, tmp_path):
        archive = tmp_path / "evil.zip"
        self._make_zip(archive, ["../evil"])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(SelfUpdateError):
            _extract(archive, dest)
        assert not (tmp_path / "evil").exists()  # nothing escaped dest
