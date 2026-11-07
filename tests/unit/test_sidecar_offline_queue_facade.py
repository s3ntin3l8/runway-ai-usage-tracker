"""Patches of names on ``scripts.sidecar`` must reach the moved offline-queue code."""

from __future__ import annotations

import json
import types

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
    fake = types.SimpleNamespace(name="nt")
    monkeypatch.setattr(sidecar, "os", fake)
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path / "missing")
    # nt + missing queue dir returns early; proves os.name came from the patched name.
    assert sidecar.queue_flush("http://x", "k") == 0
