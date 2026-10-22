"""Which credential sources a machine found but could not read (#711)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import CredentialSource, PendingCredentialTag
from app.services.blocked_collection import TOKEN_WITHHELD, collecting_providers
from app.services.blocked_sources import (
    SOURCE_REASONS,
    is_source_blocked,
    source_block_rows,
    source_blocks,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _blocked(provider="ollama", sidecar="alpha", reason="source_locked"):
    return PendingCredentialTag(
        sidecar_id=sidecar,
        provider_id=provider,
        credential_origin=f"cookie:{provider}/Chrome/Default/Network/Cookies",
        reason=reason,
        source_store="Chrome/Default/Network/Cookies",
        source_detail="held open by another process",
        source_remedy="set OLLAMA_SESSION_TOKEN (or paste the cookie in Settings → Providers)",
    )


def _source(provider="ollama", last_success_at=None, enabled=True, source_id="src:a"):
    return CredentialSource(
        provider_id=provider,
        account_id="alice@example.com",
        source_id=source_id,
        source_type="file",
        source_label="x",
        enabled=enabled,
        last_success_at=last_success_at,
    )


def test_sidecar_and_server_agree_on_the_reasons():
    from scripts.sidecar import SOURCE_BLOCK_REASONS

    assert SOURCE_REASONS == SOURCE_BLOCK_REASONS


def test_only_a_source_reason_marks_a_row_as_blocked():
    assert is_source_blocked(_blocked(reason="source_locked")) is True
    assert is_source_blocked(_blocked(reason="source_unsupported")) is True
    assert is_source_blocked(_blocked(reason="source_unreadable")) is True
    assert is_source_blocked(_blocked(reason=TOKEN_WITHHELD)) is False
    assert is_source_blocked(_blocked(reason=None)) is False


def test_a_blocked_source_names_the_store_the_reason_and_the_remedy():
    with _session() as session:
        session.add(_blocked())
        session.commit()

        blocks = source_blocks(session, source_block_rows(session), now=NOW)

    assert len(blocks) == 1
    block = blocks[0]
    assert block.provider_id == "ollama"
    assert block.sidecar_id == "alpha"
    assert block.credential_origin == "cookie:ollama/Chrome/Default/Network/Cookies"
    assert block.source_store == "Chrome/Default/Network/Cookies"
    assert block.reason == "source_locked"
    assert block.detail == "held open by another process"
    assert block.remedy.startswith("set OLLAMA_SESSION_TOKEN")


def test_a_blocked_source_says_whether_the_provider_collects_anyway():
    with _session() as session:
        row = _blocked()
        session.add(row)
        session.commit()

        # Nothing ever succeeded: the provider is dark.
        assert source_blocks(session, [row], now=NOW)[0].collecting is False

        # Another source collected recently: the provider works, the store is
        # still worth knowing about.
        session.add(_source(last_success_at=NOW - timedelta(hours=1)))
        session.commit()
        assert source_blocks(session, [row], now=NOW)[0].collecting is True

        # A long-ago success does not count on its own; the recent one above
        # is what keeps the provider collecting.
        session.add(_source(last_success_at=NOW - timedelta(hours=7), source_id="src:b"))
        session.commit()
        assert source_blocks(session, [row], now=NOW)[0].collecting is True


def test_a_disabled_source_does_not_vouch_for_the_provider():
    with _session() as session:
        row = _blocked()
        session.add(row)
        session.add(_source(last_success_at=NOW, enabled=False))
        session.commit()

        assert source_blocks(session, [row], now=NOW)[0].collecting is False


def test_rows_that_are_not_blocked_are_ignored_and_the_order_is_stable():
    with _session() as session:
        rows = [
            _blocked(provider="ollama", sidecar="alpha"),
            _blocked(provider="kimi_coding", sidecar="beta"),
            _blocked(provider="opencode", sidecar="alpha", reason=TOKEN_WITHHELD),
        ]
        session.add_all(rows)
        session.commit()

        blocks = source_blocks(session, rows, now=NOW)

    assert [(b.provider_id, b.sidecar_id) for b in blocks] == [
        ("kimi_coding", "beta"),
        ("ollama", "alpha"),
    ]


def test_nothing_blocked_is_an_empty_list():
    with _session() as session:
        assert source_blocks(session, [], now=NOW) == []
        assert source_block_rows(session) == []


def test_no_providers_means_nothing_is_collecting():
    """The shared freshness rule has nothing to judge when no provider is asked about."""
    with _session() as session:
        session.add(_source(last_success_at=NOW))
        session.commit()

        assert collecting_providers(session, set(), now=NOW) == set()
