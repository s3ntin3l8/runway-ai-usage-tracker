"""Delivery counters must survive idle heartbeats and failed uploads."""

from unittest.mock import Mock

import pytest

from scripts import sidecar
from sidecar_app.daemon import TrayDaemon


@pytest.fixture
def daemon(monkeypatch):
    tray = TrayDaemon({"api_url": "http://localhost:9999", "api_key": "test"})
    monkeypatch.setattr(sidecar, "queue_flush", Mock())
    monkeypatch.setattr(sidecar, "queue_push", Mock(return_value=True))
    monkeypatch.setattr("scripts.sidecar_pkg.event_watermark.EventWatermark", Mock())
    return tray


def cycle(monkeypatch, daemon, metrics=0, events=0, response=(True, {}, 200)):
    collection = sidecar.CollectionResult(
        [{"service_name": "test"} for _ in range(metrics)],
        [{"event_id": str(n)} for n in range(events)],
        0,
        [],
    )
    monkeypatch.setattr(sidecar, "run_collection", lambda *a, **kw: collection)
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **kw: response)
    return daemon._runner.run_once()


def test_heartbeat_preserves_last_successful_upload(monkeypatch, daemon):
    assert cycle(monkeypatch, daemon, metrics=4, events=21)
    uploaded_at = daemon._runner.last_upload_at
    assert cycle(monkeypatch, daemon)
    assert daemon._runner.last_upload_at == uploaded_at
    assert daemon._runner.last_sync_at >= uploaded_at
    assert "4 metrics" in daemon.stats_summary
    assert "21 events" in daemon.stats_summary


def test_event_only_upload_is_visible(monkeypatch, daemon):
    assert cycle(monkeypatch, daemon, events=3)
    assert "0 metrics" in daemon.stats_summary
    assert "3 events" in daemon.stats_summary
    assert daemon._runner.last_upload_at is not None


def test_startup_and_idle_heartbeat_do_not_claim_data_upload(monkeypatch, daemon):
    assert "Last upload: never" in daemon.stats_summary
    assert "Last sync: never" in daemon.stats_summary
    assert cycle(monkeypatch, daemon)
    assert "Last upload: never" in daemon.stats_summary
    assert "Last sync: never" not in daemon.stats_summary


@pytest.mark.parametrize(
    "response", [(False, "unavailable", 503), (True, {"events_error": True}, 200)]
)
def test_failed_delivery_keeps_successful_counts(monkeypatch, daemon, response):
    assert cycle(monkeypatch, daemon, metrics=1, events=2)
    uploaded_at = daemon._runner.last_upload_at
    synced_at = daemon._runner.last_sync_at
    cycle(monkeypatch, daemon, metrics=8, events=9, response=response)
    assert daemon._runner.last_upload_at == uploaded_at
    assert daemon._runner.last_metrics_count == 1
    assert daemon._runner.last_events_count == 2
    if not response[0]:
        assert daemon._runner.last_sync_at == synced_at
    assert "Error:" in daemon.stats_summary


def test_partial_batch_failure_does_not_claim_full_upload(monkeypatch, daemon):
    assert cycle(monkeypatch, daemon, metrics=1, events=2)
    uploaded_at = daemon._runner.last_upload_at
    collection = sidecar.CollectionResult([], [{"event_id": str(n)} for n in range(1001)], 0, [])
    monkeypatch.setattr(sidecar, "run_collection", lambda *a, **kw: collection)
    responses = iter([(True, {}, 200), (False, "unavailable", 503)])
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **kw: next(responses))
    assert daemon._runner.run_once() is False
    assert daemon._runner.last_upload_at == uploaded_at
    assert daemon._runner.last_events_count == 2


@pytest.mark.parametrize("heartbeat_response", [(True, {}, 200), (False, "unavailable", 503)])
def test_replayed_upload_advances_freshness_and_counts(monkeypatch, tmp_path, heartbeat_response):
    """Successful backlog delivery remains visible even if the next heartbeat fails."""
    monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path / "queue")
    assert sidecar.queue_push(
        {"metrics": [{"service_name": "quota"}], "events": [{"event_id": "queued"}]}
    )
    tray = TrayDaemon({"api_url": "http://localhost:9999", "api_key": "test"})
    monkeypatch.setattr(
        sidecar, "run_collection", lambda *a, **kw: sidecar.CollectionResult([], [], 0, [])
    )
    replies = iter([(True, {}, 200), heartbeat_response])
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **kw: next(replies))
    tray._runner.run_once()
    assert tray._runner.last_upload_at is not None
    assert tray._runner.last_sync_at >= tray._runner.last_upload_at
    assert tray._runner.last_metrics_count == 1
    assert tray._runner.last_events_count == 1
    assert "Last upload: never" not in tray.stats_summary


@pytest.mark.parametrize(
    "replay_response", [(False, "unavailable", 503), (True, {"events_error": True}, 200)]
)
def test_failed_queue_replay_cannot_claim_upload(monkeypatch, tmp_path, replay_response):
    monkeypatch.setattr(sidecar, "get_sidecar_dir", lambda: tmp_path)
    monkeypatch.setattr(sidecar, "get_queue_dir", lambda: tmp_path / "queue")
    assert sidecar.queue_push({"events": [{"event_id": "queued"}]})
    tray = TrayDaemon({"api_url": "http://localhost:9999", "api_key": "test"})
    monkeypatch.setattr(
        sidecar, "run_collection", lambda *a, **kw: sidecar.CollectionResult([], [], 0, [])
    )
    replies = iter([replay_response, (True, {}, 200)])
    monkeypatch.setattr(sidecar, "http_post_signed_with_retry", lambda *a, **kw: next(replies))
    tray._runner.run_once()
    assert tray._runner.last_upload_at is None
    assert list((tmp_path / "queue").glob("*.jsonl"))


def test_stats_snapshot_does_not_change_when_runner_changes(daemon):
    stats = daemon._runner.stats_snapshot()
    daemon._runner.last_error = "later failure"
    assert stats["last_error"] is None
    assert daemon._runner.stats_snapshot()["last_error"] == "later failure"
