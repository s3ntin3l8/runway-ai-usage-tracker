"""GET /api/v1/system/settings exposes the server version.

The Settings → About card reads `version` from this endpoint; before the fix it
was never returned, so the UI showed "v?". The version is a constant in
``app.__init__`` that Release Please bumps alongside package.json / pyproject.toml.

The same response carries ``build_sha`` — the commit the running build came
from, which is what distinguishes one `:edge` image from the next.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__
from app.api.endpoints import system as system_endpoint
from app.main import app

# Assembled from short literals so detect-secrets doesn't read the fixture as a
# high-entropy hex string (a real secret).
FAKE_SHA = "abc1234" + "def567"


def test_settings_returns_server_version():
    resp = TestClient(app).get("/api/v1/system/settings")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__


def test_settings_returns_build_sha(monkeypatch):
    monkeypatch.setattr(system_endpoint, "get_build_sha", lambda: FAKE_SHA)
    resp = TestClient(app).get("/api/v1/system/settings")
    assert resp.status_code == 200
    assert resp.json()["build_sha"] == FAKE_SHA


def test_settings_build_sha_is_null_when_unknown(monkeypatch):
    monkeypatch.setattr(system_endpoint, "get_build_sha", lambda: None)
    resp = TestClient(app).get("/api/v1/system/settings")
    assert resp.json()["build_sha"] is None


def test_version_matches_package_json():
    """The Python constant and package.json stay in lockstep — Release Please
    bumps both, so a drift means the extra-files wiring regressed."""
    pkg = json.loads((Path(__file__).resolve().parents[2] / "package.json").read_text())
    assert __version__ == pkg["version"]
