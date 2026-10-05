"""Unit tests for the sidecar self-update apply layer (scripts/sidecar_pkg/self_update.py).

Mostly the *pure* parts are tested here: asset-name resolution, checksum
verify, release-asset URL lookup, target detection, and the frozen/single-flight
guards. The OS-mutating ``apply_update`` swaps the running binary and is
otherwise verified manually per platform during release QA — except for the
macOS .app exec-bit swap below, which gets a targeted test because a
regression there bricks the launch entirely (see the "can't be opened"
incident this guards against) rather than merely failing an update.
"""

import hashlib
import io
import os
import shutil
import stat
import sys
import tarfile
import time
import zipfile

import pytest

from scripts.sidecar_pkg import self_update
from scripts.sidecar_pkg.self_update import (
    SelfUpdateError,
    SelfUpdateUnsupportedError,
    _extract,
    apply_update,
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
        assert resolve_asset_name("tray", "stable", "1.2.0") == "Runway-Sidecar-macOS-v1.2.0.zip"

    def test_windows_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolve_asset_name("tray", "stable", "v1.2.0") == "Runway-Sidecar-Windows-v1.2.0.zip"

    def test_linux_tray_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_asset_name("tray", "stable", "1.2.0") == "Runway-Sidecar-Linux-v1.2.0.tar.gz"

    def test_linux_cli_stable(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert (
            resolve_asset_name("cli", "stable", "1.2.0") == "Runway-Sidecar-Linux-CLI-v1.2.0.tar.gz"
        )

    def test_linux_tray_edge(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_asset_name("tray", "edge", None) == "Runway-Sidecar-Linux-edge.tar.gz"

    def test_linux_cli_edge(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_asset_name("cli", "edge", None) == "Runway-Sidecar-Linux-CLI-edge.tar.gz"

    def test_macos_edge(self, monkeypatch):
        # Edge now publishes a macOS asset (full platform parity).
        monkeypatch.setattr(sys, "platform", "darwin")
        assert resolve_asset_name("tray", "edge", None) == "Runway-Sidecar-macOS-edge.zip"

    def test_windows_edge(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert resolve_asset_name("tray", "edge", None) == "Runway-Sidecar-Windows-edge.zip"

    def test_beta_uses_its_versioned_release_asset_name(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert resolve_asset_name("tray", "beta", "v3.0.0-beta.1") == (
            "Runway-Sidecar-macOS-v3.0.0-beta.1.zip"
        )

    def test_unknown_platform_edge_unsupported(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "freebsd")
        with pytest.raises(SelfUpdateUnsupportedError):
            resolve_asset_name("tray", "edge", None)

    def test_stable_missing_version(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        with pytest.raises(SelfUpdateError):
            resolve_asset_name("cli", "stable", "")

    def test_stable_name_matches_published_tag_form(self, monkeypatch):
        # Regression: CI publishes names built from the raw tag (``v2.12.0``);
        # the updater used to strip the ``v`` and 404 on every stable update.
        monkeypatch.setattr(sys, "platform", "darwin")
        assert resolve_asset_name("tray", "stable", "v2.12.0") == resolve_asset_name(
            "tray", "stable", "2.12.0"
        )
        assert resolve_asset_name("tray", "stable", "v2.12.0") == "Runway-Sidecar-macOS-v2.12.0.zip"

    @pytest.mark.parametrize("plat", ["darwin", "win32", "linux"])
    @pytest.mark.parametrize("channel", ["stable", "edge"])
    def test_never_resolves_an_installer(self, monkeypatch, plat, channel):
        # The .dmg / -setup.exe are for humans; self-update swaps the payload.
        monkeypatch.setattr(sys, "platform", plat)
        name = resolve_asset_name("tray", channel, "v1.2.0")
        assert name.endswith((".zip", ".tar.gz"))

    def test_candidates_include_legacy_spelling(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert self_update._asset_name_candidates("tray", "stable", "v1.2.0") == [
            "Runway-Sidecar-Windows-v1.2.0.zip",
            "Runway-Sidecar-Windows-1.2.0.zip",
        ]

    def test_edge_candidates_have_no_legacy(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        assert self_update._asset_name_candidates("tray", "edge", None) == [
            "Runway-Sidecar-Windows-edge.zip"
        ]


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

    def test_beta_release_lookup_selects_newest_numbered_release(self, monkeypatch):
        releases = [
            {"tag_name": "v3.0.0-beta.2", "prerelease": True, "assets": ["new"]},
            {"tag_name": "v3.0.0-beta.1", "prerelease": True, "assets": ["old"]},
        ]
        monkeypatch.setattr(self_update, "_get_json", lambda _url: releases)
        assert self_update._get_release_json("beta") is releases[0]

    def test_returns_both_urls(self):
        asset, sha = find_asset_urls(self._release(), "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz")
        assert asset == "u/bin"
        assert sha == "u/sha"

    def test_raises_when_asset_missing(self):
        with pytest.raises(SelfUpdateError):
            find_asset_urls(self._release(), "Runway-Sidecar-macOS-9.9.9.zip")

    def test_candidate_list_falls_back_to_legacy_name(self):
        asset, sha = find_asset_urls(
            self._release(),
            ["Runway-Sidecar-Linux-CLI-v1.2.0.tar.gz", "Runway-Sidecar-Linux-CLI-1.2.0.tar.gz"],
        )
        assert (asset, sha) == ("u/bin", "u/sha")

    def test_candidate_list_prefers_primary_name(self):
        release = {
            "assets": [
                {"name": "Runway-Sidecar-macOS-v1.2.0.zip", "browser_download_url": "new"},
                {
                    "name": "Runway-Sidecar-macOS-v1.2.0.zip.sha256",
                    "browser_download_url": "new.sha",
                },
                {"name": "Runway-Sidecar-macOS-v1.2.0.dmg", "browser_download_url": "dmg"},
                {"name": "Runway-Sidecar-macOS-1.2.0.zip", "browser_download_url": "old"},
                {
                    "name": "Runway-Sidecar-macOS-1.2.0.zip.sha256",
                    "browser_download_url": "old.sha",
                },
            ]
        }
        assert find_asset_urls(
            release, ["Runway-Sidecar-macOS-v1.2.0.zip", "Runway-Sidecar-macOS-1.2.0.zip"]
        ) == ("new", "new.sha")

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


class TestSelfUpdateSupported:
    def test_false_when_not_frozen(self, monkeypatch):
        # From-source checkout (the common dev case): not frozen → not capable.
        monkeypatch.setattr(self_update, "_is_frozen", lambda: False)
        assert self_update.self_update_supported() is False

    def test_false_in_docker(self, monkeypatch):
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_is_docker", lambda: True)
        assert self_update.self_update_supported() is False

    def test_true_for_frozen_non_docker(self, monkeypatch):
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        assert self_update.self_update_supported() is True


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


class TestSingleFlightLockHygiene:
    """The lock must not leak across re-exec, and a leaked lock must self-heal."""

    def test_release_lock_removes_file_and_is_idempotent(self, monkeypatch, tmp_path):
        # A successful update re-execs via os.execve/os._exit, bypassing the
        # context-manager finally — _release_lock is the explicit cleanup.
        monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
        lock = tmp_path / self_update._LOCK_NAME
        lock.write_text("")
        self_update._release_lock()
        assert not lock.exists()
        self_update._release_lock()  # idempotent — no raise when already gone

    def test_blocks_on_a_fresh_lock(self, monkeypatch, tmp_path):
        monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
        lock = tmp_path / self_update._LOCK_NAME
        lock.write_text("")  # mtime ≈ now → a live update, not stale
        with self_update._single_flight() as acquired:
            assert acquired is False
        assert lock.exists()  # the other run's lock is left intact

    def test_reclaims_a_stale_lock(self, monkeypatch, tmp_path):
        # A lock orphaned by a pre-fix build (re-exec'd without releasing) is
        # older than the threshold and must be reclaimed so updates resume.
        monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
        lock = tmp_path / self_update._LOCK_NAME
        lock.write_text("")
        old = time.time() - (self_update._LOCK_STALE_SECONDS + 60)
        os.utime(lock, (old, old))
        with self_update._single_flight() as acquired:
            assert acquired is True
        assert not lock.exists()  # released cleanly after the block


# ---------------------------------------------------------------------------
# _with_retries — bounded backoff on transient GitHub failures
# ---------------------------------------------------------------------------


def _http_error(code: int):
    from urllib import error

    return error.HTTPError("https://api.github.com/x", code, "boom", {}, None)


class TestWithRetries:
    def test_succeeds_first_try(self, monkeypatch):
        slept = []
        monkeypatch.setattr(self_update.time, "sleep", lambda s: slept.append(s))
        assert self_update._with_retries(lambda: "ok", what="x") == "ok"
        assert slept == []

    def test_retries_then_succeeds_on_504(self, monkeypatch):
        slept = []
        monkeypatch.setattr(self_update.time, "sleep", lambda s: slept.append(s))
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _http_error(504)
            return "recovered"

        assert self_update._with_retries(_fn, what="release fetch") == "recovered"
        assert calls["n"] == 3
        assert slept == [2, 4]  # exponential backoff: 2s, then 4s

    def test_exhausts_and_reraises(self, monkeypatch):
        slept = []
        monkeypatch.setattr(self_update.time, "sleep", lambda s: slept.append(s))
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            raise _http_error(503)

        with pytest.raises(self_update.error.HTTPError):
            self_update._with_retries(_fn, what="x")
        assert calls["n"] == self_update._MAX_ATTEMPTS
        assert slept == [2, 4]  # one sleep between each of the 3 attempts but not after the last

    def test_no_retry_on_404(self, monkeypatch):
        slept = []
        monkeypatch.setattr(self_update.time, "sleep", lambda s: slept.append(s))
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            raise _http_error(404)

        with pytest.raises(self_update.error.HTTPError):
            self_update._with_retries(_fn, what="x")
        assert calls["n"] == 1  # genuine "asset missing" fails fast
        assert slept == []

    def test_retries_on_urlerror(self, monkeypatch):
        monkeypatch.setattr(self_update.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            if calls["n"] < 2:
                raise self_update.error.URLError("connection reset")
            return "ok"

        assert self_update._with_retries(_fn, what="x") == "ok"
        assert calls["n"] == 2


class TestSelfUpdateRetryWiring:
    def test_transient_504_on_release_fetch_exhausts_to_false(self, monkeypatch, tmp_path):
        # Past the guards, with an update available, a persistent 504 on the
        # release fetch should retry _MAX_ATTEMPTS times then degrade to False.
        monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path)
        monkeypatch.setattr(self_update, "_is_frozen", lambda: True)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        monkeypatch.setattr(self_update, "check_once", lambda *a, **k: "edge build abc123")
        monkeypatch.setattr(self_update.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            raise _http_error(504)

        monkeypatch.setattr(self_update.request, "urlopen", _boom)
        assert self_update.self_update("1.0.0+edge.aaa", "edge") is False
        assert calls["n"] == self_update._MAX_ATTEMPTS


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

    def test_zip_restores_exec_bit(self, tmp_path):
        """Regression test for the "can't be opened" incident: zipfile.extractall
        writes file contents but drops Unix permission bits, so a member stored
        with mode 0o755 (as `zip -r` records for the real RunwaySidecar binary)
        must come out executable after _extract, not read-only."""
        archive = tmp_path / "ok.zip"
        zinfo = zipfile.ZipInfo("Contents/MacOS/RunwaySidecar")
        zinfo.external_attr = (0o755 & 0o777) << 16
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(zinfo, "x")
        dest = tmp_path / "out"
        dest.mkdir()
        _extract(archive, dest)
        extracted = dest / "Contents" / "MacOS" / "RunwaySidecar"
        assert extracted.is_file()
        assert os.access(extracted, os.X_OK)

    def test_zip_without_unix_attrs_is_a_noop(self, tmp_path):
        """Archives written without Unix external_attr (e.g. Windows-built
        zips, or writestr's default) must not blow up — external_attr is 0
        there, and the `if mode:` guard should just skip the chmod."""
        archive = tmp_path / "ok.zip"
        self._make_zip(archive, ["runway-sidecar.exe"])
        dest = tmp_path / "out"
        dest.mkdir()
        _extract(archive, dest)
        assert (dest / "runway-sidecar.exe").is_file()


# ---------------------------------------------------------------------------
# apply_update — macOS .app exec-bit safety net
# ---------------------------------------------------------------------------


class TestApplyUpdateMacOSExecBit:
    def _make_app(self, root, name, *, executable):
        """Build a minimal `<name>.app/Contents/MacOS/<name>` bundle, plus a
        nested helper executable under Contents/MacOS/Helpers/ — some bundles
        ship those, and the safety net must reach them too, not just the
        top-level entry point."""
        app = root / f"{name}.app"
        macos_dir = app / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        mode = 0o755 if executable else 0o644
        binary = macos_dir / name
        binary.write_bytes(b"x")
        binary.chmod(mode)
        helpers_dir = macos_dir / "Helpers"
        helpers_dir.mkdir()
        helper = helpers_dir / f"{name} Helper"
        helper.write_bytes(b"x")
        helper.chmod(mode)
        return app

    def test_swap_restores_exec_bit_when_archive_lost_it(self, tmp_path, monkeypatch):
        """Simulates the exact bricking scenario: the staged (extracted)
        bundle's entry-point already lost its exec bit (as it would have
        before the _extract fix, or via any other archiver quirk), and
        apply_update's own chmod must still make it launchable post-swap."""
        monkeypatch.setattr(sys, "platform", "darwin")

        install_dir = tmp_path / "install"
        install_dir.mkdir()
        installed_app = self._make_app(install_dir, "Runway Sidecar", executable=True)
        monkeypatch.setattr(self_update, "_install_path", lambda: installed_app)

        staged_dir = tmp_path / "staged"
        staged_dir.mkdir()
        self._make_app(staged_dir, "Runway Sidecar", executable=False)

        relaunched = []
        monkeypatch.setattr(
            self_update, "_relaunch_posix", lambda target, install: relaunched.append(install)
        )

        result = apply_update("tray", staged_dir, restart=False)

        assert result is True
        assert not relaunched  # restart=False
        macos_dir = installed_app / "Contents" / "MacOS"
        entry_point = macos_dir / "Runway Sidecar"
        helper = macos_dir / "Helpers" / "Runway Sidecar Helper"
        for binary in (entry_point, helper):
            assert binary.is_file()
            assert os.access(binary, os.X_OK)
            # Owner-only rwx, matching the file branch's stated policy —
            # not a blanket +rx that would widen group/world permissions.
            assert stat.S_IMODE(binary.stat().st_mode) == 0o700


# ---------------------------------------------------------------------------
# running_from_disk_image (macOS DMG / App Translocation guard)
# ---------------------------------------------------------------------------


class TestRunningFromDiskImage:
    @pytest.mark.parametrize(
        "exe",
        [
            "/Volumes/Runway Sidecar/Runway Sidecar.app/Contents/MacOS/RunwaySidecar",
            "/private/var/folders/x/AppTranslocation/ABCD/d/Runway Sidecar.app/Contents/MacOS/RunwaySidecar",
        ],
    )
    def test_dmg_and_translocated_paths(self, exe):
        assert self_update.running_from_disk_image(exe, "darwin") is True

    def test_installed_app_is_not_disk_image(self):
        exe = "/Applications/Runway Sidecar.app/Contents/MacOS/RunwaySidecar"
        assert self_update.running_from_disk_image(exe, "darwin") is False

    def test_non_macos_never_disk_image(self):
        assert self_update.running_from_disk_image("/Volumes/x/RunwaySidecar", "linux") is False

    def test_blocks_self_update_support(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        monkeypatch.setattr(self_update, "running_from_disk_image", lambda: True)
        assert self_update.self_update_supported() is False

    def test_self_update_noops_from_disk_image(self, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(self_update, "_is_docker", lambda: False)
        monkeypatch.setattr(self_update, "running_from_disk_image", lambda: True)
        monkeypatch.setattr(
            self_update, "check_once", lambda *a: pytest.fail("must not reach the network")
        )
        assert self_update.self_update("1.0.0", "stable") is False


# ---------------------------------------------------------------------------
# Single-slot rollback (.previous) + Windows bookkeeping
# ---------------------------------------------------------------------------


class TestRollback:
    @pytest.fixture
    def cli_install(self, tmp_path, monkeypatch):
        """A frozen one-file Linux CLI install at tmp/bin/runway-sidecar-cli."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(self_update, "self_update_supported", lambda: True)
        monkeypatch.setattr(self_update, "_sidecar_dir", lambda: tmp_path / "cfg")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        install = bindir / "runway-sidecar-cli"
        install.write_bytes(b"v1")
        monkeypatch.setattr(self_update, "_install_path", lambda: install)
        monkeypatch.setattr(self_update, "_detect_target", lambda: "cli")
        return install

    def _stage(self, tmp_path, payload):
        staged = tmp_path / "staged"
        staged.mkdir(exist_ok=True)
        new = staged / "runway-sidecar-cli"
        new.write_bytes(payload)
        new.chmod(0o755)
        return staged

    def test_update_keeps_previous_and_version(self, tmp_path, cli_install):
        legacy_old = cli_install.with_name("runway-sidecar-cli.old")
        legacy_old.write_bytes(b"ancient")

        ok = apply_update(
            "cli", self._stage(tmp_path, b"v2"), restart=False, current_version="1.0.0"
        )

        assert ok is True
        assert cli_install.read_bytes() == b"v2"
        assert cli_install.with_name("runway-sidecar-cli.previous").read_bytes() == b"v1"
        assert self_update.rollback_available() == "1.0.0"
        assert not legacy_old.exists()  # superseded breadcrumb cleaned up

    def test_rollback_swaps_back_and_is_undoable(self, tmp_path, cli_install):
        apply_update("cli", self._stage(tmp_path, b"v2"), restart=False, current_version="1.0.0")

        assert self_update.rollback("2.0.0", restart=False) is True

        assert cli_install.read_bytes() == b"v1"
        assert cli_install.with_name("runway-sidecar-cli.previous").read_bytes() == b"v2"
        assert self_update.rollback_available() == "2.0.0"
        assert not cli_install.with_name("runway-sidecar-cli.rollback").exists()

    def test_rollback_without_backup_is_a_noop(self, cli_install):
        assert self_update.rollback_available() is None
        assert self_update.rollback("1.0.0", restart=False) is False
        assert cli_install.read_bytes() == b"v1"

    def test_rollback_unavailable_when_not_self_updatable(self, cli_install, monkeypatch):
        cli_install.with_name("runway-sidecar-cli.previous").write_bytes(b"v0")
        monkeypatch.setattr(self_update, "self_update_supported", lambda: False)
        assert self_update.rollback_available() is None
        assert self_update.rollback("1.0.0", restart=False) is False


class TestWindowsSwapScript:
    def _script(self, **kw):
        import pathlib

        base = pathlib.PureWindowsPath(r"C:\Users\u\AppData\Local\Programs\Runway Sidecar")
        return self_update._windows_swap_script(
            4242,
            base / "RunwaySidecar.exe",
            base / "RunwaySidecar.new.exe",
            base / "RunwaySidecar.exe.previous",
            **kw,
        )

    def test_keeps_previous_and_refreshes_display_version(self):
        script = self._script(restart=True, display_version="2.13.0")
        assert 'move /Y "' in script and 'RunwaySidecar.exe.previous" >NUL 2>&1' in script
        assert "reg query" in script
        assert '/v DisplayVersion /t REG_SZ /d "2.13.0" /f' in script
        # Only touches the installer's key, and only if it exists (portable
        # installs have none): query guards the add.
        assert "Uninstall\\Runway Sidecar" in script
        assert script.index("reg query") < script.index("reg add")
        assert 'start "" "' in script

    def test_no_registry_write_without_version(self):
        script = self._script(restart=False, display_version=None)
        assert "reg " not in script
        assert "rem no relaunch" in script

    def test_failed_replace_restores_backup(self):
        script = self._script(restart=True, display_version="2.13.0")
        recovery = script[script.index(":restore_previous") : script.index(":restore_failed")]
        assert "del " not in recovery
        assert "if not exist" in recovery
        assert (
            'move /Y "C:\\Users\\u\\AppData\\Local\\Programs\\Runway Sidecar\\RunwaySidecar.exe.previous"'
            in recovery
        )
        assert "if errorlevel 1 goto restore_failed" in recovery
        restore_failure = script[script.index(":restore_failed") : script.index(":swap_failed")]
        assert "Failed to restore previous sidecar from backup" in restore_failure
        failure_branch = script[script.index(":swap_failed") :]
        assert "if exist" in failure_branch and 'start "" "' in failure_branch
        assert "exit /b 1" in failure_branch

    def test_logs_swap_result_and_checks_each_move(self):
        script = self._script(restart=True, display_version="2.13.0")
        assert 'set "LOG=%~dp0runway-self-update.log"' in script
        assert "if %wait_attempts% GEQ 120 goto move_failed" in script
        assert "if errorlevel 1 goto restore_previous" in script
        assert "if errorlevel 1 goto restore_failed" in script
        assert "Failed to restore previous sidecar from backup" in script
        assert "Installed updated sidecar" in script
        assert "locked after %wait_attempts% move attempts" in script
        assert 'tasklist /FI "IMAGENAME eq RunwaySidecar.exe" >>"%LOG%" 2>&1' in script
        assert script.index(":move_failed") < script.index(":swap_failed")
        assert 'del "%~f0"\r\nexit /b 1' in script
        assert "Relaunch requested" not in script
        assert script.index('set "LOG=%~dp0runway-self-update.log"') < script.index("echo [")
        assert 'Self-update helper started.>"%LOG%"' in script
        assert script.index('Self-update helper started.>"%LOG%"') < script.index(
            "Waiting for sidecar PID"
        )

    def test_relaunch_uses_fresh_onefile_runtime_after_parent_releases_exe(self):
        script = self._script(restart=True, display_version=None)
        assert script.index('set "PYINSTALLER_RESET_ENVIRONMENT=1"') < script.index('start "" "')
        assert script.index(":move_original") < script.index(":install_new")
        assert "goto move_original" in script
        assert "timeout /t" not in script

    def test_windows_helper_spawn_failure_is_reported(self, tmp_path, monkeypatch, caplog):
        helper = tmp_path / "runway-self-update.bat"
        incoming = tmp_path / "RunwaySidecar.new.exe"
        incoming.write_bytes(b"staged update")

        def fail_spawn(*_args, **_kwargs):
            raise OSError("cmd.exe unavailable")

        monkeypatch.setattr(self_update.subprocess, "Popen", fail_spawn)
        assert (
            self_update._apply_windows(
                tmp_path / "RunwaySidecar.exe",
                incoming,
                restart=False,
            )
            is False
        )
        assert not helper.exists()
        assert not incoming.exists()
        assert "Could not start Windows self-update helper" in caplog.text
        assert "cmd.exe unavailable" in caplog.text

    def test_windows_helper_launch_quotes_path_and_hides_console(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "Runway Sidecar"
        install_dir.mkdir()
        install = install_dir / "RunwaySidecar.exe"
        incoming = install_dir / "RunwaySidecar.new.exe"
        calls = []
        monkeypatch.setattr(self_update.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))

        assert self_update._apply_windows(install, incoming, restart=False)
        (args, kwargs) = calls[0]
        assert args[0] == f'cmd.exe /d /s /c ""{install_dir / "runway-self-update.bat"}""'
        assert kwargs["executable"] == "cmd.exe"
        assert kwargs["creationflags"] & 0x08000000


class TestPreExecHooks:
    """execve keeps the PID and skips atexit: cleanup must run just before it."""

    @pytest.fixture(autouse=True)
    def _clean_hooks(self, monkeypatch):
        monkeypatch.setattr(self_update, "_PRE_EXEC_HOOKS", [])

    def test_hooks_run_after_the_lock_release_and_before_exec(self, monkeypatch, tmp_path):
        calls: list[str] = []
        self_update.register_pre_exec_hook(lambda: calls.append("hook"))
        monkeypatch.setattr(self_update, "_release_lock", lambda: calls.append("lock"))
        monkeypatch.setattr(self_update.os, "execve", lambda path, argv, env: calls.append("exec"))
        self_update._relaunch_posix("cli", tmp_path / "x")
        assert calls == ["lock", "hook", "exec"]

    def test_registration_is_idempotent(self):
        def hook():
            pass

        self_update.register_pre_exec_hook(hook)
        self_update.register_pre_exec_hook(hook)
        assert [h for h, _ in self_update._PRE_EXEC_HOOKS] == [hook]

    def test_a_failing_hook_does_not_block_the_exec_or_later_hooks(
        self, monkeypatch, tmp_path, caplog
    ):
        calls: list[str] = []

        def boom():
            raise RuntimeError("nope")

        self_update.register_pre_exec_hook(boom)
        self_update.register_pre_exec_hook(lambda: calls.append("second"))
        monkeypatch.setattr(self_update, "_release_lock", lambda: None)
        monkeypatch.setattr(self_update.os, "execve", lambda path, argv, env: calls.append("exec"))
        self_update._relaunch_posix("cli", tmp_path / "x")
        assert calls == ["second", "exec"]
        assert "Pre-exec hook" in caplog.text

    def test_the_tray_relaunch_does_not_run_cli_hooks(self, monkeypatch, tmp_path):
        ran: list[str] = []
        self_update.register_pre_exec_hook(lambda: ran.append("hook"))
        monkeypatch.setattr(self_update, "_release_lock", lambda: None)
        monkeypatch.setattr(self_update.subprocess, "Popen", lambda *a, **k: None)
        monkeypatch.setattr(self_update.os, "_exit", lambda code: ran.append("exit"))
        self_update._relaunch_posix("tray", tmp_path / "tray")
        assert ran == ["exit"]

    def test_the_sidecar_pid_file_is_gone_by_the_time_of_exec(self, monkeypatch, tmp_path):
        """End to end: the real remove_pid_file hook leaves nothing for the new image to trip on."""
        from scripts import sidecar

        pid_file = tmp_path / "sidecar.pid"
        monkeypatch.setattr(sidecar, "get_pid_file_path", lambda: pid_file)
        assert sidecar.write_pid_file() is True
        assert pid_file.exists()
        self_update.register_pre_exec_hook(sidecar.remove_pid_file)

        seen: dict[str, bool] = {}
        monkeypatch.setattr(self_update, "_release_lock", lambda: None)
        monkeypatch.setattr(
            self_update.os,
            "execve",
            lambda path, argv, env: seen.update(pid_file=pid_file.exists()),
        )
        self_update._relaunch_posix("cli", tmp_path / "x")

        assert seen == {"pid_file": False}
        # …and the re-exec'd image (same PID) can claim it again.
        assert sidecar.write_pid_file() is True
        sidecar.remove_pid_file()


class TestFreshRuntimeOnRelaunch:
    """A relaunched onefile build must unpack its own runtime, not reuse the old _MEI dir."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setattr(self_update, "_PRE_EXEC_HOOKS", [])
        monkeypatch.setattr(self_update, "_release_lock", lambda: None)
        monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "/opt/x/tmp/_MEIold")
        monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/opt/x/runway-sidecar-cli")
        monkeypatch.setenv("KEEP_ME", "1")

    @staticmethod
    def _assert_fresh(env):
        assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert not [k for k in env if k.startswith("_PYI_")]
        assert env["KEEP_ME"] == "1"

    def test_cli_exec_gets_a_scrubbed_env(self, monkeypatch, tmp_path):
        seen: dict = {}
        monkeypatch.setattr(self_update.os, "execve", lambda path, argv, env: seen.update(env=env))
        self_update._relaunch_posix("cli", tmp_path / "x")
        self._assert_fresh(seen["env"])

    def test_linux_tray_spawn_gets_a_scrubbed_env(self, monkeypatch, tmp_path):
        seen: dict = {}
        monkeypatch.setattr(self_update.sys, "platform", "linux")
        monkeypatch.setattr(self_update.subprocess, "Popen", lambda *a, **k: seen.update(k))
        monkeypatch.setattr(self_update.os, "_exit", lambda code: None)
        self_update._relaunch_posix("tray", tmp_path / "tray")
        self._assert_fresh(seen["env"])

    def test_macos_open_spawn_gets_a_scrubbed_env(self, monkeypatch, tmp_path):
        seen: dict = {}
        monkeypatch.setattr(self_update.sys, "platform", "darwin")
        monkeypatch.setattr(
            self_update.subprocess, "Popen", lambda *a, **k: seen.update(k, argv=a[0])
        )
        monkeypatch.setattr(self_update.os, "_exit", lambda code: None)
        self_update._relaunch_posix("tray", tmp_path / "tray")
        assert seen["argv"][:2] == ["open", "-n"]
        self._assert_fresh(seen["env"])

    def test_cleanup_runs_before_exec(self, monkeypatch, tmp_path):
        download = tmp_path / "runway-update-x"
        download.mkdir()
        gone_at_exec: list[bool] = []
        monkeypatch.setattr(
            self_update.os, "execve", lambda p, a, e: gone_at_exec.append(not download.exists())
        )
        self_update._relaunch_posix("cli", tmp_path / "x", cleanup=lambda: shutil.rmtree(download))
        assert gone_at_exec == [True]

    def test_a_failing_cleanup_does_not_block_the_exec(self, monkeypatch, tmp_path, caplog):
        calls: list[str] = []
        monkeypatch.setattr(self_update.os, "execve", lambda p, a, e: calls.append("exec"))

        def boom():
            raise OSError("rmtree failed")

        self_update._relaunch_posix("cli", tmp_path / "x", cleanup=boom)
        assert calls == ["exec"]
        assert "Pre-relaunch cleanup" in caplog.text


class TestExecFailureRestoresHooks:
    """If execve fails the old image keeps running: it must get back what the hooks released."""

    @pytest.fixture(autouse=True)
    def _clean_hooks(self, monkeypatch):
        monkeypatch.setattr(self_update, "_PRE_EXEC_HOOKS", [])
        monkeypatch.setattr(self_update, "_release_lock", lambda: None)

    @staticmethod
    def _failing_execv(msg):
        def execve(path, argv, env):
            raise OSError(msg)

        return execve

    def test_a_failed_execve_undoes_the_hooks_and_still_raises(self, monkeypatch, tmp_path):
        calls: list[str] = []
        self_update.register_pre_exec_hook(
            lambda: calls.append("released"), on_failure=lambda: calls.append("restored")
        )

        def failing_execv(path, argv, env):
            calls.append("exec")
            raise OSError("Exec format error")

        monkeypatch.setattr(self_update.os, "execve", failing_execv)
        with pytest.raises(OSError, match="Exec format"):
            self_update._relaunch_posix("cli", tmp_path / "x")
        assert calls == ["released", "exec", "restored"]

    def test_an_undo_is_optional_and_a_failing_undo_does_not_mask_the_error(
        self, monkeypatch, tmp_path
    ):
        def bad_undo():
            raise RuntimeError("undo broke")

        self_update.register_pre_exec_hook(lambda: None)  # no undo
        self_update.register_pre_exec_hook(lambda: None, on_failure=bad_undo)
        monkeypatch.setattr(self_update.os, "execve", self._failing_execv("denied"))
        with pytest.raises(OSError, match="denied"):
            self_update._relaunch_posix("cli", tmp_path / "x")

    def test_a_successful_execve_never_runs_the_undo(self, monkeypatch, tmp_path):
        calls: list[str] = []
        self_update.register_pre_exec_hook(lambda: None, on_failure=lambda: calls.append("undo"))
        monkeypatch.setattr(self_update.os, "execve", lambda p, a, e: None)
        self_update._relaunch_posix("cli", tmp_path / "x")
        assert calls == []

    def test_after_a_failed_execv_the_real_pid_file_is_back(self, monkeypatch, tmp_path):
        from scripts import sidecar

        pid_file = tmp_path / "sidecar.pid"
        monkeypatch.setattr(sidecar, "get_pid_file_path", lambda: pid_file)
        assert sidecar.write_pid_file() is True
        self_update.register_pre_exec_hook(
            sidecar.remove_pid_file, on_failure=sidecar.write_pid_file
        )
        monkeypatch.setattr(self_update.os, "execve", self._failing_execv("ENOEXEC"))
        with pytest.raises(OSError):
            self_update._relaunch_posix("cli", tmp_path / "x")
        assert pid_file.exists()
        sidecar.remove_pid_file()
