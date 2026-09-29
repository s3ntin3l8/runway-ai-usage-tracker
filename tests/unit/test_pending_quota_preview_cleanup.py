from __future__ import annotations

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
