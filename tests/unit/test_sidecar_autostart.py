"""Unit tests for sidecar_app.autostart — macOS and Windows paths."""

import sys
import types
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_autostart(system: str, extra_modules: dict | None = None) -> types.ModuleType:
    """Import (or re-import) autostart with platform.system() patched to *system*.

    On non-Windows hosts we also inject a fake ``winreg`` module so that the
    conditional import at the top of autostart.py can succeed.
    """

    # Inject a fake winreg before loading so the conditional import succeeds
    fake_winreg = extra_modules.get("winreg") if extra_modules else None
    if fake_winreg is None:
        fake_winreg = MagicMock()
        fake_winreg.HKEY_CURRENT_USER = "HKCU"
        fake_winreg.KEY_READ = 1
        fake_winreg.KEY_SET_VALUE = 2
        fake_winreg.REG_SZ = 1

    # Remove any cached module so we get a fresh import
    sys.modules.pop("sidecar_app.autostart", None)

    with (
        patch("platform.system", return_value=system),
        patch.dict(sys.modules, {"winreg": fake_winreg}),
    ):
        import sidecar_app.autostart as mod  # noqa: PLC0415

        # Re-evaluate _SYSTEM inside the freshly loaded module
        mod._SYSTEM = system  # type: ignore[attr-defined]
        return mod


# ---------------------------------------------------------------------------
# macOS tests
# ---------------------------------------------------------------------------


class TestMacOSAutostart:
    """Tests for the macOS LaunchAgent plist path."""

    def _get_mod(self) -> types.ModuleType:
        return _reload_autostart("Darwin")

    def test_is_installed_when_plist_exists(self) -> None:
        mod = self._get_mod()
        with patch("pathlib.Path.exists", return_value=True):
            assert mod.is_login_item_installed() is True

    def test_is_not_installed_when_plist_absent(self) -> None:
        mod = self._get_mod()
        with patch("pathlib.Path.exists", return_value=False):
            assert mod.is_login_item_installed() is False

    def test_install_writes_plist_without_loading(self) -> None:
        """Install writes the plist but must NOT ``launchctl load`` it.

        Loading mid-session would immediately spawn a duplicate sidecar (the
        plist has RunAtLoad); macOS auto-loads LaunchAgents at the next login.
        """
        mod = self._get_mod()
        with (
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch("pathlib.Path.write_text") as mock_write,
            patch("subprocess.run") as mock_run,
            patch.object(sys, "executable", "/usr/bin/runway-sidecar"),
        ):
            mod.install_login_item()

        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)
        mock_write.assert_called_once()
        written_content: str = mock_write.call_args[0][0]
        assert "/usr/bin/runway-sidecar" in written_content
        assert "<key>Label</key>" in written_content
        assert "<key>RunAtLoad</key>" in written_content

        # No launchctl load — that was the source of the duplicate-instance bug.
        mock_run.assert_not_called()

    def test_remove_calls_unload_and_removes_plist(self) -> None:
        mod = self._get_mod()
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.unlink") as mock_unlink,
            patch("subprocess.run") as mock_run,
        ):
            mod.remove_login_item()

        mock_run.assert_called_once()
        run_cmd = mock_run.call_args[0][0]
        assert run_cmd[0] == "launchctl"
        assert run_cmd[1] == "unload"
        mock_unlink.assert_called_once()

    def test_remove_is_idempotent_when_not_installed(self) -> None:
        """remove_login_item() should be a no-op when the plist is absent."""
        mod = self._get_mod()
        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.unlink") as mock_unlink,
            patch("subprocess.run") as mock_run,
        ):
            mod.remove_login_item()  # must not raise

        mock_run.assert_not_called()
        mock_unlink.assert_not_called()


# ---------------------------------------------------------------------------
# Windows tests
# ---------------------------------------------------------------------------


class TestWindowsAutostart:
    """Tests for the Windows registry Run key path."""

    def _get_mod_and_winreg(self) -> tuple[types.ModuleType, MagicMock]:
        """Return freshly loaded module + the fake winreg mock attached to it."""
        fake_winreg = MagicMock()
        fake_winreg.HKEY_CURRENT_USER = "HKCU"
        fake_winreg.KEY_READ = 1
        fake_winreg.KEY_SET_VALUE = 2
        fake_winreg.REG_SZ = 1

        mod = _reload_autostart("Windows", extra_modules={"winreg": fake_winreg})
        # Bind the same fake winreg into the module namespace so calls resolve
        mod.winreg = fake_winreg  # type: ignore[attr-defined]
        return mod, fake_winreg

    def test_is_installed_when_reg_key_exists(self) -> None:
        mod, fake_winreg = self._get_mod_and_winreg()
        # QueryValueEx succeeds (returns normally) → installed
        fake_winreg.OpenKey.return_value.__enter__ = lambda s: s
        fake_winreg.OpenKey.return_value.__exit__ = MagicMock(return_value=False)
        fake_winreg.QueryValueEx.return_value = ("path", 1)

        with patch.object(mod, "winreg", fake_winreg):
            result = mod.is_login_item_installed()

        assert result is True

    def test_is_not_installed_when_reg_key_absent(self) -> None:
        mod, fake_winreg = self._get_mod_and_winreg()
        fake_winreg.OpenKey.side_effect = FileNotFoundError

        with patch.object(mod, "winreg", fake_winreg):
            result = mod.is_login_item_installed()

        assert result is False

    def test_install_sets_reg_value(self) -> None:
        mod, fake_winreg = self._get_mod_and_winreg()
        fake_winreg.OpenKey.return_value.__enter__ = lambda s: s
        fake_winreg.OpenKey.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch.object(mod, "winreg", fake_winreg),
            patch.object(sys, "executable", r"C:\runway\sidecar.exe"),
        ):
            mod.install_login_item()

        fake_winreg.SetValueEx.assert_called_once()
        args = fake_winreg.SetValueEx.call_args[0]
        # args: (key, name, reserved, type, value)
        assert args[1] == "Runway Sidecar"
        assert args[4] == r"C:\runway\sidecar.exe"

    def test_remove_deletes_reg_value(self) -> None:
        mod, fake_winreg = self._get_mod_and_winreg()
        fake_winreg.OpenKey.return_value.__enter__ = lambda s: s
        fake_winreg.OpenKey.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(mod, "winreg", fake_winreg):
            mod.remove_login_item()

        fake_winreg.DeleteValue.assert_called_once()
        args = fake_winreg.DeleteValue.call_args[0]
        assert args[1] == "Runway Sidecar"

    def test_remove_idempotent_when_key_absent(self) -> None:
        """remove_login_item() must not raise when the registry key is absent."""
        mod, fake_winreg = self._get_mod_and_winreg()
        fake_winreg.OpenKey.side_effect = FileNotFoundError

        with patch.object(mod, "winreg", fake_winreg):
            mod.remove_login_item()  # must not raise


# ---------------------------------------------------------------------------
# Linux tests
# ---------------------------------------------------------------------------


class TestLinuxAutostart:
    """Tests for the Linux XDG autostart .desktop file path."""

    def _get_mod(self) -> types.ModuleType:
        return _reload_autostart("Linux")

    def test_is_installed_when_desktop_file_exists(self) -> None:
        mod = self._get_mod()
        with patch("pathlib.Path.exists", return_value=True):
            assert mod.is_login_item_installed() is True

    def test_is_not_installed_when_desktop_file_absent(self) -> None:
        mod = self._get_mod()
        with patch("pathlib.Path.exists", return_value=False):
            assert mod.is_login_item_installed() is False

    def test_install_creates_desktop_file(self) -> None:
        mod = self._get_mod()
        with (
            patch("pathlib.Path.mkdir") as mock_mkdir,
            patch("pathlib.Path.write_text") as mock_write,
            patch.object(sys, "executable", "/usr/bin/runway-sidecar"),
        ):
            mod.install_login_item()

        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)
        mock_write.assert_called_once()
        written_content: str = mock_write.call_args[0][0]
        assert "/usr/bin/runway-sidecar" in written_content
        assert "[Desktop Entry]" in written_content
        assert "Type=Application" in written_content
        assert "X-GNOME-Autostart-enabled=true" in written_content

    def test_remove_deletes_desktop_file(self) -> None:
        mod = self._get_mod()
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.unlink") as mock_unlink,
        ):
            mod.remove_login_item()

        mock_unlink.assert_called_once()

    def test_remove_is_idempotent_when_not_installed(self) -> None:
        mod = self._get_mod()
        with (
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.unlink") as mock_unlink,
        ):
            mod.remove_login_item()  # must not raise

        mock_unlink.assert_not_called()


# ---------------------------------------------------------------------------
# Unsupported platform (FreeBSD, etc.)
# ---------------------------------------------------------------------------


class TestUnsupportedPlatform:
    def test_is_installed_returns_false(self) -> None:
        mod = _reload_autostart("FreeBSD")
        assert mod.is_login_item_installed() is False

    def test_install_is_noop(self) -> None:
        mod = _reload_autostart("FreeBSD")
        mod.install_login_item()  # must not raise

    def test_remove_is_noop(self) -> None:
        mod = _reload_autostart("FreeBSD")
        mod.remove_login_item()  # must not raise
