"""macOS tray glyph is inset so it matches native menu-bar icon sizing."""

import pytest

# pystray/Pillow live in the optional `desktop` extra, which CI doesn't install.
pytest.importorskip("pystray")
pytest.importorskip("PIL")

from sidecar_app import tray  # noqa: E402


@pytest.mark.parametrize("status", ["ok", "warn", "err", "paused", "starting"])
def test_macos_icon_is_inset(status, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    img = tray._build_status_icon(status)
    assert img.size == (128, 128)
    left, top, right, bottom = img.getchannel("A").getbbox()
    margin = 16
    assert left >= margin and top >= margin
    assert 128 - right >= margin and 128 - bottom >= margin
