"""Patches of names on ``scripts.sidecar`` must reach the moved offline-queue code."""

from __future__ import annotations

import json

from scripts import sidecar


def test_queue_flush_uses_patched_get_queue_dir_and_post(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "ensure_dirs", lambda: None)
    calls = []

    def fake_post(url, payload, api_key, **kwargs):
        calls.append(url)
        return True, {}, 200

    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", fake_post)
    (tmp_path / "2026-01-01.jsonl").write_text(
        json.dumps({"ts": 1, "payload": {"metrics": [{"a": 1}]}}) + "\n"
    )
    assert sidecar.queue_flush("http://x", "k") == 1
    assert calls == ["http://x/api/v1/fleet/ingest"]
    assert not (tmp_path / "2026-01-01.jsonl").exists()


def test_queue_push_uses_patched_dirs_and_ensure_dirs(monkeypatch, tmp_path):
    ensured = []
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "ensure_dirs", lambda: ensured.append(1))
    assert sidecar.queue_push({"metrics": []}) is True
    assert list(tmp_path.glob("*.jsonl"))
    assert ensured  # queue_push called the patched ensure_dirs


def test_moved_code_reads_os_from_facade(monkeypatch, tmp_path):
    class FakeOs:
        reads: list[str] = []

        @property
        def name(self):
            FakeOs.reads.append("name")
            return "nt"

    monkeypatch.setattr(sidecar, "os", FakeOs())
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path / "missing")
    # nt + missing queue dir returns early; the recorded read proves os came from the facade.
    assert sidecar.queue_flush("http://x", "k") == 0
    assert FakeOs.reads == ["name"]


def test_token_only_units_patch_reaches_strip_credentials(monkeypatch):
    card = {"remaining": "Token", "unit": "oauth"}
    assert sidecar.strip_credentials({"metrics": [card]})["metrics"] == []
    monkeypatch.setattr(sidecar, "_TOKEN_ONLY_UNITS", frozenset())
    assert sidecar.strip_credentials({"metrics": [card]})["metrics"] == [card]
