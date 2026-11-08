"""Patches of names on ``scripts.sidecar`` must reach the moved cookie-extractor code.

The moved code uses ``os`` and ``platform`` (replaced wholesale on the facade),
``__REGISTRY__`` and ``decrypt_chromium_cookie`` (patched by name); it does not
use ``subprocess``.
"""

from __future__ import annotations

from scripts import sidecar
from scripts.sidecar_pkg import cookie_extractor


def test_patched_platform_reaches_decrypt(monkeypatch):
    class FakePlatform:
        @staticmethod
        def system():
            return "Windows"

    # On the real (non-Windows) platform bytes decode directly; the Windows branch
    # has no ``ctypes.windll`` here and returns None, so None proves the patch landed.
    assert sidecar.decrypt_chromium_cookie(b"abc") == "abc"
    monkeypatch.setattr(sidecar, "platform", FakePlatform())
    assert sidecar.decrypt_chromium_cookie(b"abc") is None


def test_patched_os_reaches_moved_code(monkeypatch):
    class FakeOs:
        @staticmethod
        def getenv(name, default=None):
            return "patched"

    monkeypatch.setattr(sidecar, "os", FakeOs())
    assert cookie_extractor.os.getenv("ANYTHING") == "patched"


def test_patched_registry_reaches_env_var_lookup(monkeypatch):
    registry = {
        "providers": {
            "demo": {
                "rules": [{"type": "env", "variable": "DEMO_TOKEN", "mapping": {"value": "tok"}}]
            }
        }
    }
    monkeypatch.setattr(sidecar, "__REGISTRY__", registry)
    assert sidecar._session_cookie_env_var("demo", "tok") == "DEMO_TOKEN"


def test_patched_decrypt_reaches_extractor(monkeypatch):
    seen = []
    monkeypatch.setattr(
        sidecar, "decrypt_chromium_cookie", lambda *a, **k: seen.append(a) or "patched"
    )
    # BrowserCookieExtractor.get_cookie resolves its decrypt call through this lookup.
    assert cookie_extractor._sidecar_attr("decrypt_chromium_cookie")(b"x") == "patched"
    assert seen
