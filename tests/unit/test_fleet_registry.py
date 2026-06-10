"""Unit tests for FleetRegistryService (Phase 4B)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.models.db import SidecarRegistry
from app.services.fleet_registry import FleetRegistryService


@pytest.fixture
def service():
    return FleetRegistryService()


@pytest.fixture
def mock_session():
    return MagicMock()


class TestUpsertSidecar:
    def test_creates_new_sidecar_on_first_call(self, service, mock_session):
        mock_session.get.return_value = None  # Not found

        result = service.upsert_sidecar("host-1", "10.0.0.1", mock_session)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        added = mock_session.add.call_args[0][0]
        assert added.sidecar_id == "host-1"
        assert added.hostname == "host-1"
        assert added.last_ip == "10.0.0.1"

    def test_updates_existing_sidecar(self, service, mock_session):
        existing = SidecarRegistry(
            sidecar_id="host-1",
            ingest_count=5,
            last_ip="10.0.0.1",
            last_seen=datetime(2026, 1, 1, tzinfo=UTC),
            first_seen=datetime(2026, 1, 1, tzinfo=UTC),
        )
        mock_session.get.return_value = existing

        service.upsert_sidecar("host-1", "10.0.0.2", mock_session)

        assert existing.ingest_count == 6
        assert existing.last_ip == "10.0.0.2"
        assert existing.last_seen > datetime(2026, 1, 1, tzinfo=UTC)
        mock_session.add.assert_not_called()
        mock_session.commit.assert_called_once()


class TestUpdateSidecar:
    def test_updates_custom_name_and_tags(self, service, mock_session):
        existing = SidecarRegistry(
            sidecar_id="host-1",
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
        )
        mock_session.get.return_value = existing

        result = service.update_sidecar("host-1", "My Laptop", ["Work", "Primary"], mock_session)

        assert existing.custom_name == "My Laptop"
        assert existing.tags == ["Work", "Primary"]
        mock_session.commit.assert_called_once()

    def test_returns_none_for_unknown_sidecar(self, service, mock_session):
        mock_session.get.return_value = None

        result = service.update_sidecar("unknown", "Name", [], mock_session)

        assert result is None
        mock_session.commit.assert_not_called()

    def test_partial_update_only_name(self, service, mock_session):
        existing = SidecarRegistry(
            sidecar_id="host-1",
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
        )
        existing.tags = ["Existing"]
        mock_session.get.return_value = existing

        service.update_sidecar("host-1", "New Name", None, mock_session)

        assert existing.custom_name == "New Name"
        assert existing.tags == ["Existing"]  # Unchanged


class TestDeleteSidecar:
    def test_deletes_existing_sidecar(self, service, mock_session):
        existing = SidecarRegistry(
            sidecar_id="host-1",
            first_seen=datetime.now(UTC),
            last_seen=datetime.now(UTC),
        )
        mock_session.get.return_value = existing

        result = service.delete_sidecar("host-1", mock_session)

        assert result is True
        mock_session.delete.assert_called_once_with(existing)
        mock_session.commit.assert_called_once()

    def test_returns_false_for_unknown_sidecar(self, service, mock_session):
        mock_session.get.return_value = None

        result = service.delete_sidecar("unknown", mock_session)

        assert result is False
        mock_session.delete.assert_not_called()


class TestToDict:
    def test_serializes_all_fields(self, service):
        now = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        row = SidecarRegistry(
            sidecar_id="host-1",
            hostname="host-1.local",
            custom_name="My Laptop",
            last_seen=now,
            first_seen=now,
            last_ip="192.168.1.1",
            error_count=2,
            ingest_count=42,
        )
        row.tags = ["Work", "Primary"]

        d = service.to_dict(row)

        assert d["sidecar_id"] == "host-1"
        assert d["hostname"] == "host-1.local"
        assert d["custom_name"] == "My Laptop"
        assert d["tags"] == ["Work", "Primary"]
        assert d["ingest_count"] == 42
        assert d["error_count"] == 2
        assert d["last_ip"] == "192.168.1.1"
        assert "last_seen" in d
        assert "first_seen" in d
        assert d["channel"] == "stable"  # no version → stable


class TestToDictUpdateAvailable:
    """to_dict surfaces update_available + latest_version from the version checker."""

    def _row(self, version: str | None) -> SidecarRegistry:
        now = datetime(2026, 4, 13, 12, 0, 0, tzinfo=UTC)
        return SidecarRegistry(
            sidecar_id="host-1",
            hostname="host-1",
            sidecar_version=version,
            first_seen=now,
            last_seen=now,
        )

    def test_flags_update_when_sidecar_is_behind(self, service, monkeypatch):
        from app.services import sidecar_version_checker as svc_mod

        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest", "1.5.0")
        d = service.to_dict(self._row("1.4.0"))
        assert d["update_available"] is True
        assert d["latest_version"] == "1.5.0"

    def test_no_update_when_sidecar_matches_latest(self, service, monkeypatch):
        from app.services import sidecar_version_checker as svc_mod

        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest", "1.5.0")
        d = service.to_dict(self._row("1.5.0"))
        assert d["update_available"] is False
        assert d["latest_version"] == "1.5.0"

    def test_no_update_when_latest_unknown(self, service, monkeypatch):
        from app.services import sidecar_version_checker as svc_mod

        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest", None)
        d = service.to_dict(self._row("1.0.0"))
        assert d["update_available"] is False
        assert d["latest_version"] is None

    def test_no_update_when_sidecar_version_missing(self, service, monkeypatch):
        from app.services import sidecar_version_checker as svc_mod

        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest", "1.5.0")
        d = service.to_dict(self._row(None))
        assert d["update_available"] is False
        assert d["latest_version"] == "1.5.0"
        assert d["sidecar_version"] is None

    def test_edge_sidecar_flagged_when_tag_moved(self, service, monkeypatch):
        from app.services import sidecar_version_checker as svc_mod

        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest", "1.5.0")
        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest_edge_sha", "bbb2222ffffffff")
        d = service.to_dict(self._row("1.5.0+edge.aaa1111"))
        assert d["channel"] == "edge"
        assert d["update_available"] is True

    def test_edge_sidecar_not_flagged_when_same_build(self, service, monkeypatch):
        from app.services import sidecar_version_checker as svc_mod

        monkeypatch.setattr(svc_mod.sidecar_version_checker, "_latest_edge_sha", "aaa1111ffffffff")
        d = service.to_dict(self._row("1.5.0+edge.aaa1111"))
        assert d["channel"] == "edge"
        assert d["update_available"] is False
