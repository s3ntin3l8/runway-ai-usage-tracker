"""The tray bootstrap must honour claude_config_dirs / codex_home like the CLI daemon."""

from unittest.mock import MagicMock

import pytest

# pystray/Pillow live in the optional `desktop` extra, which CI may not install.
pytest.importorskip("pystray")
pytest.importorskip("PIL")

from sidecar_app import __main__ as tray_main  # noqa: E402
from sidecar_app.daemon import _sidecar  # noqa: E402


def test_apply_login_dirs_calls_configure_login_dirs(monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(_sidecar, "configure_login_dirs", spy)
    cfg = {"claude_config_dirs": ["/x"], "codex_home": "/y"}
    tray_main._apply_login_dirs(cfg)
    spy.assert_called_once_with(cfg)


def test_apply_login_dirs_failure_only_logs(monkeypatch):
    monkeypatch.setattr(
        _sidecar, "configure_login_dirs", MagicMock(side_effect=RuntimeError("boom"))
    )
    tray_main._apply_login_dirs({})  # must not raise


def test_apply_login_dirs_populates_extras(monkeypatch):
    monkeypatch.setattr(_sidecar, "_LOGIN_DIR_EXTRAS", {})
    tray_main._apply_login_dirs({"claude_config_dirs": ["/a/b"]})
    assert any("/a/b" in str(v) for v in _sidecar._LOGIN_DIR_EXTRAS.values())
