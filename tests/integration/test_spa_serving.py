"""Integration tests for the SPA-serving layer in app/main.py.

The v2 SPA rewrite added index/catch-all routing, the favicon fallback, the
immutable-assets middleware, and the security-header middleware. None of it was
exercised by the suite (in the Python test env `webapp/dist` doesn't exist, so
these routes' real branches never ran), which is the patch-coverage gap Codecov
flags. These tests monkeypatch the resolved `frontend_path` to a temp dist so
every branch is hit deterministically, regardless of whether a real build is
present.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app


@pytest.fixture()
def dist(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A fake webapp/dist with index.html, a favicon and a hashed asset."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Runway</title>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)")
    # The SPA routes read this module global at request time, so a monkeypatch
    # takes effect without re-importing the app.
    monkeypatch.setattr(main, "frontend_path", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    # No `with` → the app lifespan (poller / startup collection) is not run.
    return TestClient(app)


# --- index / SPA fallback --------------------------------------------------


def test_root_serves_spa_index(client: TestClient, dist: pathlib.Path):
    r = client.get("/")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()
    # SPA shell must never be cached.
    assert "no-cache" in r.headers["cache-control"]


def test_root_missing_build_returns_404(
    client: TestClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(main, "frontend_path", str(tmp_path / "does-not-exist"))
    r = client.get("/")
    assert r.status_code == 404
    assert "Frontend build not found" in r.text


def test_deep_link_falls_back_to_index(client: TestClient, dist: pathlib.Path):
    # A client-side route (no dot in the last segment) → the SPA shell.
    r = client.get("/provider/anthropic")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text.lower()


def test_existing_dotted_file_is_served(client: TestClient, dist: pathlib.Path):
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert "<svg" in r.text


def test_missing_dotted_file_is_404_not_html(client: TestClient, dist: pathlib.Path):
    r = client.get("/nope.js")
    assert r.status_code == 404
    assert "<!doctype html>" not in r.text.lower()


def test_path_traversal_is_blocked(client: TestClient, dist: pathlib.Path):
    # Percent-encoded ../ escapes are decoded to a real ".." path whose last
    # segment has a dot → the realpath guard must 404 rather than serve it.
    r = client.get("/%2e%2e/%2e%2e/%2e%2e/etc/passwd.js")
    assert r.status_code == 404


# --- favicon ---------------------------------------------------------------


def test_favicon_served_when_present(client: TestClient, dist: pathlib.Path):
    r = client.get("/favicon.ico")
    assert r.status_code == 200


def test_favicon_204_when_absent(
    client: TestClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(main, "frontend_path", str(empty))
    r = client.get("/favicon.ico")
    assert r.status_code == 204


# --- middleware ------------------------------------------------------------


def test_assets_get_immutable_cache_header(client: TestClient):
    # The immutable-assets middleware stamps the header for any /assets/ path,
    # even a 404 — so this holds whether or not a real build is mounted.
    r = client.get("/assets/whatever-zzz.js")
    assert "immutable" in r.headers.get("cache-control", "")
    assert "max-age=31536000" in r.headers.get("cache-control", "")


def test_security_headers_present_on_every_response(client: TestClient, dist: pathlib.Path):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
