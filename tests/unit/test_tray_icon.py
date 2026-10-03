"""macOS tray glyph is inset so it matches native menu-bar icon sizing."""

import pytest

from sidecar_app import tray


@pytest.mark.parametrize("status", ["ok", "warn", "err", "paused", "starting"])
def test_macos_icon_is_inset(status, monkeypatch):
    monkeypatch.setattr(tray.sys, "platform", "darwin")
    img = tray._build_status_icon(status)
    assert img.size == (128, 128)
    left, top, right, bottom = img.getchannel("A").getbbox()
    margin = 16
    assert left >= margin and top >= margin
    assert 128 - right >= margin and 128 - bottom >= margin
