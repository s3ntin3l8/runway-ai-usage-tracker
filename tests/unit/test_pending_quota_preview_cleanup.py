from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

from app import main


def test_expire_pending_quota_previews_commits_cleanup(monkeypatch):
    calls: list[str] = []

    class FakeSession:
        def __init__(self, _engine: object):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def commit(self):
            calls.append("commit")

    from app.services.credential_tags import PendingCredentialTagRepo

    monkeypatch.setattr("sqlmodel.Session", FakeSession)
    monkeypatch.setattr(
        PendingCredentialTagRepo,
        "expire_quota_previews",
        lambda session: calls.append("expire"),
    )

    main._expire_pending_quota_previews()

    assert calls == ["expire", "commit"]


def test_preview_cleanup_interval_tracks_ttl_and_caps_at_a_day():
    assert main._pending_quota_preview_cleanup_interval_seconds(60) == 60
    assert main._pending_quota_preview_cleanup_interval_seconds(172800) == 86400


@pytest.mark.asyncio
async def test_preview_cleanup_loop_runs_cleanup_and_propagates_cancellation():
    sleep_intervals: list[float] = []
    cleanup_calls: list[bool] = []

    async def fake_sleep(interval: float) -> None:
        sleep_intervals.append(interval)
        if len(sleep_intervals) == 2:
            raise asyncio.CancelledError

    def cleanup() -> None:
        cleanup_calls.append(True)

    with pytest.raises(asyncio.CancelledError):
        await main._pending_quota_preview_cleanup_loop(sleep=fake_sleep, cleanup=cleanup)

    assert sleep_intervals == [86400, 86400]
    assert cleanup_calls == [True]


@pytest.mark.asyncio
async def test_preview_cleanup_loop_logs_cleanup_errors(caplog):
    sleep_count = 0

    async def fake_sleep(_interval: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            raise asyncio.CancelledError

    def failing_cleanup() -> None:
        raise RuntimeError("database unavailable")

    with pytest.raises(asyncio.CancelledError):
        await main._pending_quota_preview_cleanup_loop(sleep=fake_sleep, cleanup=failing_cleanup)

    assert "Pending quota preview cleanup failed: database unavailable" in caplog.text


@pytest.mark.asyncio
async def test_lifespan_runs_startup_cleanup_and_stops_periodic_task(monkeypatch, caplog):
    from app.core.config import settings

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import app.core.db as core_db
    import app.services.accumulator as accumulator

    monkeypatch.setattr(core_db, "engine", engine)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "_expire_pending_quota_previews", lambda: None)
    monkeypatch.setattr(accumulator, "evict_orphan_error_rows", lambda _session: None)

    async def collect_all() -> list[dict]:
        return []

    async def close_manager() -> None:
        return None

    monkeypatch.setattr(main.manager, "collect_all", collect_all)
    monkeypatch.setattr(main.manager, "close", close_manager)
    monkeypatch.setattr(main.poller, "start", lambda: None)
    monkeypatch.setattr(main.poller, "stop", close_manager)
    monkeypatch.setattr(main.sidecar_version_checker, "start", lambda: None)
    monkeypatch.setattr(main.sidecar_version_checker, "stop", close_manager)
    monkeypatch.setattr(main.token_auto_refresher, "stop", close_manager)
    monkeypatch.setattr(settings, "TOKEN_AUTO_REFRESH_ENABLED", False)

    caplog.set_level("DEBUG", logger="app.main")
    async with main.lifespan(main.app):
        await asyncio.sleep(0)

    assert "Pending quota preview cleanup task cancelled during shutdown" in caplog.text
