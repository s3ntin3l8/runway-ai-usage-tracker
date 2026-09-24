"""Fleet-page download card: GitHub release → typed installer/payload list."""

from __future__ import annotations

import asyncio

import pytest

from app.models.schemas import SidecarDownloadsResponse
from app.services import sidecar_downloads as sd
from scripts.sidecar_pkg import asset_names

BASE = "https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases/download/v2.13.0"


def _release(label: str = "v2.13.0") -> dict:
    names = asset_names.release_assets(label)
    extra = [f"{n}.sha256" for n in names] + [f"{n}.sig" for n in names]
    extra += ["SHA256SUMS.txt", "SHA256SUMS.txt.sig", "source.tar.gz"]
    return {
        "tag_name": label,
        "published_at": "2026-09-24T12:00:00Z",
        "html_url": f"https://github.com/x/releases/tag/{label}",
        "assets": [
            {"name": n, "browser_download_url": f"{BASE}/{n}", "size": 1234}
            for n in [*names, *extra]
        ],
    }


class TestClassifyParity:
    @pytest.mark.parametrize("label", ["v2.13.0", "edge", "v3.0.0-rc.1"])
    def test_server_and_sidecar_agree_on_every_published_name(self, label):
        # The server image doesn't ship scripts/, so the grammar is mirrored;
        # this pins the copy to the sidecar's source of truth.
        for name in asset_names.release_assets(label):
            assert sd.classify(name) == asset_names.classify(name)

    @pytest.mark.parametrize(
        "name",
        ["SHA256SUMS.txt", "Runway-Sidecar-macOS-v1.0.0.dmg.sha256", "notes.md", "x.dmg"],
    )
    def test_non_assets_ignored(self, name):
        assert sd.classify(name) is None
        assert asset_names.classify(name) is None


class TestParseRelease:
    def test_installers_first_then_payloads_per_platform(self):
        resp = sd.parse_release(_release(), "stable")
        assert resp.version == "v2.13.0"
        assert [(a.platform, a.kind) for a in resp.assets] == [
            ("macOS", "installer"),
            ("macOS", "payload"),
            ("Windows", "installer"),
            ("Windows", "payload"),
            ("Linux", "payload"),
            ("Linux-CLI", "payload"),
        ]
        dmg = resp.assets[0]
        assert dmg.name == "Runway-Sidecar-macOS-v2.13.0.dmg"
        assert dmg.sha256_url == f"{BASE}/Runway-Sidecar-macOS-v2.13.0.dmg.sha256"
        assert dmg.size == 1234
        assert resp.checksums_url == f"{BASE}/SHA256SUMS.txt"
        assert resp.error is None

    def test_legacy_release_without_installers(self):
        # Pre-installer releases: only payloads — the card falls back to them.
        rel = _release()
        rel["assets"] = [a for a in rel["assets"] if not a["name"].endswith((".dmg", ".exe"))]
        resp = sd.parse_release(rel, "stable")
        assert {a.kind for a in resp.assets} == {"payload"}


class TestCache:
    def test_caches_and_degrades_to_last_good(self, monkeypatch):
        calls: list[str] = []
        good = sd.parse_release(_release(), "stable")
        bad = SidecarDownloadsResponse(channel="stable", release_url="u", error="boom")
        results = [good, bad]

        async def fake_fetch(self, channel):
            calls.append(channel)
            return results.pop(0)

        monkeypatch.setattr(sd.SidecarDownloads, "_fetch", fake_fetch)
        svc = sd.SidecarDownloads(ttl=0)  # always stale → refetch every call

        first = asyncio.run(svc.get("stable"))
        second = asyncio.run(svc.get("stable"))
        assert first.error is None
        assert second is first  # failed refresh keeps serving the last good answer
        assert calls == ["stable", "stable"]

    def test_fresh_cache_skips_fetch(self, monkeypatch):
        calls: list[str] = []

        async def fake_fetch(self, channel):
            calls.append(channel)
            return sd.parse_release(_release("edge"), channel)

        monkeypatch.setattr(sd.SidecarDownloads, "_fetch", fake_fetch)
        svc = sd.SidecarDownloads(ttl=3600)
        asyncio.run(svc.get("edge"))
        asyncio.run(svc.get("edge"))
        assert calls == ["edge"]

    def test_error_without_cache_is_returned(self, monkeypatch):
        async def fake_fetch(self, channel):
            return SidecarDownloadsResponse(channel=channel, release_url="u", error="HTTP 403")

        monkeypatch.setattr(sd.SidecarDownloads, "_fetch", fake_fetch)
        resp = asyncio.run(sd.SidecarDownloads().get("stable"))
        assert resp.error == "HTTP 403"
        assert resp.assets == []

    def test_unknown_channel_coerced_to_stable(self, monkeypatch):
        seen: list[str] = []

        async def fake_fetch(self, channel):
            seen.append(channel)
            return sd.parse_release(_release(), channel)

        monkeypatch.setattr(sd.SidecarDownloads, "_fetch", fake_fetch)
        asyncio.run(sd.SidecarDownloads().get("nightly"))
        assert seen == ["stable"]
