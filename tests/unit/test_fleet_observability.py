"""Fleet observability: identity sources, ingest failures, outdated sidecars."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import SidecarRegistry
from app.services.fleet_registry import fleet_registry


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_identity_sources_round_trip_through_registry():
    session = _session()
    sources = {"anthropic": {"account_id": "alice@example.com", "source": "local"}}
    row = fleet_registry.upsert_sidecar("laptop", "10.0.0.2", session, identity_sources=sources)
    assert fleet_registry.to_dict(row)["identity_sources"] == sources

    # A check-in without the field (older sidecar) keeps the last report.
    row = fleet_registry.upsert_sidecar("laptop", "10.0.0.2", session)
    assert fleet_registry.to_dict(row)["identity_sources"] == sources


def test_offline_sidecar_reports_outdated_but_no_update_offer():
    """#202: an offline sidecar behind the latest release says so, while the
    actionable "Update now" stays suppressed (it can't receive the push)."""
    session = _session()
    row = SidecarRegistry(
        sidecar_id="macbook",
        hostname="macbook",
        sidecar_version="2.8.1",
        self_update_capable=True,
        last_seen=datetime.now(UTC) - timedelta(hours=55),
    )
    session.add(row)
    session.commit()

    with (
        patch(
            "app.services.sidecar_version_checker.sidecar_version_checker.get_latest",
            return_value="2.10.0",
        ),
        patch(
            "app.services.sidecar_version_checker.sidecar_version_checker.get_latest_edge_sha",
            return_value=None,
        ),
    ):
        d = fleet_registry.to_dict(row)

    assert d["stale"] is True
    assert d["outdated"] is True
    assert d["update_available"] is False


def test_extraction_failures_are_counted(monkeypatch):
    """A failing extractor (#320) must count as a collection error, not only
    a log line."""
    from unittest.mock import MagicMock

    import scripts.sidecar as sc

    def _boom(*_a, **_k):
        raise TypeError("'PosixPath' object is not iterable")

    monkeypatch.setattr(sc, "_make_account_extractor", lambda *_a, **_k: _boom)
    failures = sc._extract_events_for_provider(
        "anthropic",
        ["alice@example.com"],
        watermark=MagicMock(last_pushed=MagicMock(return_value=None)),
        bootstrap_days=90,
        out_events=[],
    )
    assert failures == 1
