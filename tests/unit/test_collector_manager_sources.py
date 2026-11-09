"""Unit tests for CollectorManager source health, server sources recording, and provenance."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from app.models.db import CredentialSource
from tests.unit._collector_manager_helpers import manager_fixture  # noqa: F401 — fixture


class TestCollectorManagerInitialization:
    def test_record_source_health_follows_source_promoted_to_another_account(
        self, manager, monkeypatch
    ):
        """An identity-pending verifier is keyed ``default`` when collection starts; a
        successful run promotes its source to the resolved account mid-attempt. The
        health update must reach the source's new row, not vanish against ``default``."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        # The suite-wide autouse ``mock_db_session`` fixture replaces ``sqlmodel.Session``
        # (which ``_record_source_health`` imports at call time) with a mock; this test
        # needs a real in-memory DB, so put the real class back for its duration.
        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        with Session(engine) as session:
            session.add(
                CredentialSource(
                    provider_id="antigravity",
                    account_id="alice@example.com",  # promoted from "default"
                    source_id="sidecar:host-a:oauth",
                    source_type="file",
                    source_label="oauth.json",
                    sidecar_id="host-a",
                    health="unavailable",
                )
            )
            session.commit()

        manager._record_source_health("antigravity", "default", {"sidecar:host-a:oauth": "healthy"})
        # The failover cache is keyed by where the row lives now, not the pre-run id, or a
        # promoted source would keep its stale state until the next sync.
        assert manager._credential_source_state[("antigravity", "alice@example.com")] == {
            "sidecar:host-a:oauth": ("healthy", None)
        }
        assert ("antigravity", "default") not in manager._credential_source_state

        with Session(engine) as session:
            row = session.exec(select(CredentialSource)).one()
            assert row.health == "healthy"

    def test_record_source_health_stamps_provenance(self, manager, monkeypatch):
        """Each attempted source gets attempt/success/error provenance — the data behind
        "which source is the data coming from"."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        with Session(engine) as session:
            for source_id in ("sidecar:a", "sidecar:b"):
                session.add(
                    CredentialSource(
                        provider_id="gemini",
                        account_id="alice@example.com",
                        source_id=source_id,
                        source_type="file",
                        source_label=source_id,
                    )
                )
            session.commit()

        manager._record_source_health(
            "gemini",
            "alice@example.com",
            {"sidecar:a": "auth_failed", "sidecar:b": "healthy"},
        )

        with Session(engine) as session:
            rows = {r.source_id: r for r in session.exec(select(CredentialSource)).all()}
        assert rows["sidecar:a"].last_attempt_at is not None
        assert rows["sidecar:a"].last_success_at is None
        assert rows["sidecar:a"].last_error == "Authentication failed"
        assert rows["sidecar:b"].last_success_at is not None
        assert rows["sidecar:b"].last_error is None
        # Failover reads its own cache, so the outcome must be visible to the very next
        # cycle instead of after the next sync: a rejected source rests, a working one doesn't.
        state = manager._credential_source_state[("gemini", "alice@example.com")]
        assert state["sidecar:a"][0] == "auth_failed" and state["sidecar:a"][1] is not None
        assert state["sidecar:b"] == ("healthy", None)

    def test_record_server_sources_registers_the_env_credential_that_fed_collection(
        self, manager, monkeypatch
    ):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")  # pragma: allowlist secret
        # Hermetic: ignore whatever gh / config files exist on the machine running the tests.
        monkeypatch.setattr("app.services.credentials.provider.expand_rule_paths", lambda _p: [])

        manager._record_server_sources("github", "s3ntin3l8", "healthy")

        with Session(engine) as session:
            (row,) = session.exec(select(CredentialSource)).all()
        assert (row.provider_id, row.account_id) == ("github", "s3ntin3l8")
        assert row.source_type == "env" and row.source_label == "GITHUB_TOKEN"
        assert row.source_id == "server:github:env:GITHUB_TOKEN"
        assert row.last_success_at is not None

    def test_record_server_sources_registers_runways_device_login_file(self, manager, monkeypatch):
        """A managed file (the GitHub device-login token) is evidence for its account too."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        from app.services.credential_provider import CredentialProvider

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setattr(
            CredentialProvider,
            "server_credential_origins",
            staticmethod(
                lambda _pid: [
                    {
                        "source_type": "file",
                        "label": "github_oauth.json",
                        "keys": ["api_key"],
                        "managed": True,
                    }
                ]
            ),
        )

        manager._record_server_sources("github", "s3ntin3l8", "healthy")

        with Session(engine) as session:
            (row,) = session.exec(select(CredentialSource)).all()
        assert (row.provider_id, row.account_id) == ("github", "s3ntin3l8")
        assert row.source_type == "file" and row.source_label == "github_oauth.json"

    def test_record_server_sources_prunes_a_removed_env_credential(self, manager, monkeypatch):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setattr("app.services.credentials.provider.expand_rule_paths", lambda _p: [])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")  # pragma: allowlist secret
        manager._record_server_sources("github", "s3ntin3l8", "healthy")

        # The env var is removed: the next collection must forget its row (even though no
        # server credential is left to register).
        monkeypatch.delenv("GITHUB_TOKEN")
        manager._record_server_sources("github", "default", "unavailable")

        with Session(engine) as session:
            assert session.exec(select(CredentialSource)).all() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "expect_outcome"),
        [
            ("complete", True),
            ("partial", True),  # fresh provider data, just not a complete snapshot
            ("failed", True),  # the credential was used and failed: record that
            ("cached", False),  # served from cache: nothing was verified
            ("skipped", False),  # collector not run
        ],
    )
    async def test_server_source_outcome_is_recorded_only_when_the_credential_was_used(
        self, manager, state, expect_outcome
    ):
        """A cached/skipped result must not stamp last_success_at on the env credential —
        the inventory would then show it as freshly verified when no collection ran."""
        smart = SimpleNamespace(
            collector=SimpleNamespace(PROVIDER_ID="github", account_id="s3ntin3l8"),
            last_collection_state=state,
        )
        manager.smart_collectors = {"github:default": smart}
        usable = [{"data_source": "api", "remaining": 5}]
        with (
            patch.object(manager, "_collect_with_source_failover", AsyncMock(return_value=usable)),
            patch.object(manager, "_record_source_health"),
            patch.object(manager, "_record_server_sources") as record,
        ):
            await manager._collect_with_semaphore("github:default", MagicMock())

        record.assert_called_once()
        assert record.call_args.args[:2] == ("github", "s3ntin3l8")
        assert record.call_args.args[2] == ("healthy" if expect_outcome else None)

    def test_record_server_sources_registers_without_stamping_when_nothing_was_verified(
        self, manager, monkeypatch
    ):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel, create_engine
        from sqlmodel.orm.session import Session

        monkeypatch.setattr("sqlmodel.Session", Session)
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr("app.core.db.engine", engine)
        monkeypatch.setattr("app.services.credentials.provider.expand_rule_paths", lambda _p: [])
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")  # pragma: allowlist secret

        manager._record_server_sources("github", "s3ntin3l8", None)

        with Session(engine) as session:
            (row,) = session.exec(select(CredentialSource)).all()
        assert row.source_id == "server:github:env:GITHUB_TOKEN"
        assert (row.last_attempt_at, row.last_success_at, row.last_error) == (None, None, None)

    def test_record_server_sources_failure_is_a_warning_not_an_exception(
        self, manager, monkeypatch, caplog
    ):
        """Never break a collection over provenance — but a persistent failure must be
        visible above debug level."""
        import logging

        def boom(_provider_id):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(
            "app.services.credential_provider.CredentialProvider.get_credentials",
            staticmethod(boom),
        )
        with caplog.at_level(logging.WARNING, logger="app.services.collector_manager"):
            manager._record_server_sources("github", "default", "healthy")  # must not raise

        assert any(
            r.levelno == logging.WARNING and "server credential sources" in r.getMessage()
            for r in caplog.records
        )

    def test_result_health_collapses_collector_output(self, manager):
        usable = [{"data_source": "api", "remaining": 5}]
        assert manager._result_health(usable) == "healthy"
        assert manager._result_health([{"error_type": "auth_failed"}]) == "auth_failed"
        assert manager._result_health([{"error_type": "invalid_api_key"}]) == "auth_failed"
        assert manager._result_health([{"error_type": "api_error"}]) == "unavailable"
        assert manager._result_health([]) == "unavailable"
