"""Contract tests tying the sidecar release pipeline to its consumers.

The build workflow, the self-updater, the NSIS installer, the tray's login-item
code and the DMG artwork each hard-depend on names/values owned by another
file. These tests pin those seams so a rename on one side fails CI instead of
silently breaking self-update or leaving orphaned registry values on users'
machines. (Ported in spirit from branchdam-agent's
release_workflow_contract_test.go.)
"""

import pathlib
import re
import struct
import sys

import pytest

from scripts.sidecar_pkg import asset_names
from sidecar_app import autostart

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
BUILD_WF = (WORKFLOWS / "sidecar-build.yml").read_text(encoding="utf-8")
NSI = (ROOT / "installer" / "windows" / "runway-sidecar.nsi").read_text(encoding="utf-8")
INSTALLER_ASSETS = ROOT / "installer" / "assets"


def _nsis_define(name: str) -> str:
    m = re.search(rf'^!define {name} "([^"]*)"', NSI, re.MULTILINE)
    assert m, f"!define {name} missing from runway-sidecar.nsi"
    return m.group(1)


# ---------------------------------------------------------------------------
# Asset names: workflow ↔ updater
# ---------------------------------------------------------------------------


class TestAssetNames:
    def test_workflow_asks_asset_names_for_every_file_name(self):
        assert "python -m scripts.sidecar_pkg.asset_names" in BUILD_WF

    def test_workflow_never_spells_out_an_asset_name(self):
        # Globs (Runway-Sidecar-*) are fine; concrete names must come from
        # asset_names so they cannot drift from what the updater looks up.
        code = "\n".join(ln for ln in BUILD_WF.splitlines() if not ln.lstrip().startswith("#"))
        hardcoded = re.findall(r"Runway-Sidecar-(?:macOS|Windows|Linux)[^\s\"']*", code)
        assert hardcoded == []

    def test_workflow_matrix_covers_every_platform(self):
        platforms = set(re.findall(r"^\s+platform: (\S+)$", BUILD_WF, re.MULTILINE))
        assert platforms == set(asset_names.PLATFORMS)

    def test_checksum_manifest_is_unversioned(self):
        assert asset_names.CHECKSUMS_FILE == "SHA256SUMS.txt"
        assert "> SHA256SUMS.txt" in BUILD_WF

    @pytest.mark.parametrize("label", ["v2.13.0", "edge", "v3.0.0-rc.1"])
    def test_every_published_name_classifies_back(self, label):
        for name in asset_names.release_assets(label):
            plat, got_label, kind = asset_names.classify(name)
            assert got_label == label
            assert kind == ("installer" if name.endswith((".dmg", "-setup.exe")) else "payload")
            assert plat in asset_names.PLATFORMS

    def test_installers_only_for_desktop_platforms(self):
        assert asset_names.installer_name(asset_names.LINUX, "edge") is None
        assert asset_names.installer_name(asset_names.LINUX_CLI, "edge") is None
        assert asset_names.installer_name(asset_names.MACOS, "v1.0.0").endswith(".dmg")
        assert asset_names.installer_name(asset_names.WINDOWS, "v1.0.0").endswith("-setup.exe")

    @pytest.mark.parametrize(
        "caller", ["release-please.yml", "sidecar-edge.yml", "sidecar-release.yml"]
    )
    def test_callers_use_the_shared_build(self, caller):
        text = (WORKFLOWS / caller).read_text(encoding="utf-8")
        assert "uses: ./.github/workflows/sidecar-build.yml" in text
        assert "pyinstaller" not in text.lower()  # no private copy of the matrix
        # The attest job signs via OIDC; the caller must grant it.
        assert "id-token: write" in text

    def test_attest_verifies_against_the_build_workflow_identity(self):
        assert "sidecar-build\\.yml@" in BUILD_WF
        assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in BUILD_WF


# ---------------------------------------------------------------------------
# Windows installer ↔ tray / updater
# ---------------------------------------------------------------------------


class TestWindowsInstaller:
    def test_login_item_matches_tray_toggle(self):
        # The finish-page checkbox and the tray's "Launch at Login" must write
        # the same HKCU Run value, or they disagree and uninstall orphans one.
        assert _nsis_define("RUN_KEY") == autostart._WIN_REG_PATH
        assert _nsis_define("RUN_VALUE") == autostart._WIN_REG_KEY

    def test_exe_name_matches_windows_spec(self):
        spec = (ROOT / "sidecar_app" / "spec" / "windows.spec").read_text(encoding="utf-8")
        assert 'name="RunwaySidecar"' in spec
        assert _nsis_define("EXE_NAME") == "RunwaySidecar.exe"

    def test_uninstaller_cleans_self_update_leftovers(self):
        # Names produced by scripts/sidecar_pkg/self_update.py:_apply_windows.
        for leftover in ("RunwaySidecar.new.exe", "runway-self-update.bat"):
            assert f'Delete "$INSTDIR\\{leftover}"' in NSI

    def test_deep_link_scheme_registered_and_removed(self):
        from scripts.sidecar_pkg import pairing

        assert _nsis_define("URL_SCHEME") == pairing.SCHEME
        assert '"URL Protocol"' in NSI
        assert '"$INSTDIR\\${EXE_NAME}" "%1"' in NSI
        assert 'DeleteRegKey HKCU "${URL_KEY}"' in NSI

    def test_per_user_install(self):
        assert "RequestExecutionLevel user" in NSI
        assert "$LOCALAPPDATA\\Programs" in NSI
        assert "HKLM" not in NSI

    @pytest.mark.parametrize(
        ("name", "size"),
        [("installer-sidebar.bmp", (164, 314)), ("installer-header.bmp", (150, 57))],
    )
    def test_wizard_bitmaps_are_nsis_shaped(self, name, size):
        data = (INSTALLER_ASSETS / name).read_bytes()
        assert data[:2] == b"BM"
        width, height = struct.unpack("<ii", data[18:26])
        bpp = struct.unpack("<H", data[28:30])[0]
        assert (width, abs(height)) == size
        assert bpp == 24
        assert f"..\\assets\\{name}" in NSI

    def test_icon_is_referenced_and_present(self):
        assert '"..\\assets\\app.ico"' in NSI
        assert (INSTALLER_ASSETS / "app.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"


# ---------------------------------------------------------------------------
# macOS bundle + DMG
# ---------------------------------------------------------------------------


class TestMacOS:
    def test_spec_uses_committed_icns(self):
        spec = (ROOT / "sidecar_app" / "spec" / "macos.spec").read_text(encoding="utf-8")
        assert '"installer", "assets", "app.icns"' in spec
        assert (INSTALLER_ASSETS / "app.icns").read_bytes()[:4] == b"icns"

    def test_bundle_declares_the_pairing_url_scheme(self):
        from scripts.sidecar_pkg import pairing

        spec = (ROOT / "sidecar_app" / "spec" / "macos.spec").read_text(encoding="utf-8")
        assert f'"CFBundleURLSchemes": ["{pairing.SCHEME}"]' in spec
        # argv emulation would swallow the launch-time GURL Apple Event.
        assert "argv_emulation=False" in spec

    def test_server_mints_links_the_sidecar_parses(self):
        from app.services import pairing as server_pairing
        from scripts.sidecar_pkg import pairing

        code = server_pairing.generate_code()
        link = server_pairing.deep_link("https://runway.example.com/base", code)
        target = pairing.parse_pair_url(link)
        assert target.server == "https://runway.example.com/base"
        assert server_pairing.normalize(target.code) == server_pairing.normalize(code)

    def test_dmg_icon_slots_match_background_art(self):
        svg = (ROOT / "assets" / "installer" / "dmg-background.svg").read_text(encoding="utf-8")
        assert "(180,320)" in svg and "(580,320)" in svg
        assert '--icon "Runway Sidecar.app" 180 320' in BUILD_WF
        assert "--app-drop-link 580 320" in BUILD_WF
        assert "--window-size 760 480" in BUILD_WF

    def test_dmg_background_renders_exist_at_1x_and_2x(self):
        for name, size in (
            ("dmg-background.png", (760, 480)),
            ("dmg-background@2x.png", (1520, 960)),
        ):
            data = (INSTALLER_ASSETS / name).read_bytes()
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", data[16:24]) == size


# ---------------------------------------------------------------------------
# Windows version resource (sidecar_app/spec/win_version.py)
# ---------------------------------------------------------------------------


class TestWinVersion:
    @pytest.fixture(autouse=True)
    def _import(self, monkeypatch):
        monkeypatch.syspath_prepend(str(ROOT / "sidecar_app" / "spec"))
        import win_version

        self.wv = win_version
        yield
        sys.modules.pop("win_version", None)

    @pytest.mark.parametrize(
        ("version", "quad"),
        [
            ("2.12.0", "2.12.0.0"),
            ("v2.12.0", "2.12.0.0"),
            ("2.12.0+edge.abc1234", "2.12.0.0"),
            ("3.0.0-rc.1", "3.0.0.0"),
            ("1", "1.0.0.0"),
            ("70000.1.2", "65535.1.2.0"),
        ],
    )
    def test_quad(self, version, quad):
        assert self.wv.quad(version) == quad

    def test_version_info_keeps_full_product_version(self):
        text = self.wv.render_version_info("2.12.0+edge.abc1234")
        assert "filevers=(2, 12, 0, 0)" in text
        assert "StringStruct('ProductVersion', '2.12.0+edge.abc1234')" in text
