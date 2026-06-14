"""GET /api/v1/system/settings exposes the server version.

The Settings → About card reads `version` from this endpoint; before the fix it
was never returned, so the UI showed "v?". The version is a constant in
``app.__init__`` that Release Please bumps alongside package.json / pyproject.toml.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__
from app.main import app


def test_settings_returns_server_version():
    resp = TestClient(app).get("/api/v1/system/settings")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__


def test_version_matches_package_json():
    """The Python constant and package.json stay in lockstep — Release Please
    bumps both, so a drift means the extra-files wiring regressed."""
    pkg = json.loads((Path(__file__).resolve().parents[2] / "package.json").read_text())
    assert __version__ == pkg["version"]
