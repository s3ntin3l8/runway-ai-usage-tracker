"""The merge record: where a retired account id went (#903)."""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import ProviderAccountMerge
from app.services.account_merges import record_account_merge, resolve_merged_account


@pytest.fixture(name="session")
def session_fixture():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        os.remove(db_path)


def test_an_unknown_account_resolves_to_itself(session):
    assert resolve_merged_account(session, "openrouter", "alice@example.com") == "alice@example.com"


def test_a_merged_account_resolves_to_its_destination(session):
    record_account_merge(session, "openrouter", "alice@example.com", "bob@example.com")

    assert resolve_merged_account(session, "openrouter", "alice@example.com") == "bob@example.com"
    # Another provider's merge does not leak across.
    assert resolve_merged_account(session, "gemini", "alice@example.com") == "alice@example.com"


def test_a_chain_of_merges_resolves_to_the_last_destination(session):
    record_account_merge(session, "openrouter", "alice@example.com", "bob@example.com")
    record_account_merge(session, "openrouter", "bob@example.com", "carol@example.com")

    assert resolve_merged_account(session, "openrouter", "alice@example.com") == "carol@example.com"


def test_a_cycle_stops_instead_of_looping(session):
    """A malformed pair of records must not hang the ingest.

    alice → bob → alice: the first hop is followed, and the cycle is broken
    there — the resolution stops at bob rather than chasing alice again.
    """
    session.add(
        ProviderAccountMerge(
            provider_id="openrouter",
            source_account_id="alice@example.com",
            destination_account_id="bob@example.com",
        )
    )
    session.add(
        ProviderAccountMerge(
            provider_id="openrouter",
            source_account_id="bob@example.com",
            destination_account_id="alice@example.com",
        )
    )
    session.commit()

    assert resolve_merged_account(session, "openrouter", "alice@example.com") == "bob@example.com"
    # And it terminates: the same answer on a second call.
    assert resolve_merged_account(session, "openrouter", "alice@example.com") == "bob@example.com"


def test_recording_a_merge_again_replaces_the_record(session):
    """One row per (provider, source): the latest merge wins."""
    record_account_merge(session, "openrouter", "alice@example.com", "bob@example.com")
    record_account_merge(session, "openrouter", "alice@example.com", "carol@example.com")
    session.commit()

    rows = session.exec(select(ProviderAccountMerge)).all()
    assert len(rows) == 1
    assert rows[0].destination_account_id == "carol@example.com"
    assert resolve_merged_account(session, "openrouter", "alice@example.com") == "carol@example.com"


def test_the_account_ids_are_canonicalised(session):
    """The record stores canonical ids, so a typed id still resolves."""
    record_account_merge(session, "openrouter", "Alice@Example.com", "Bob@Example.com")
    session.commit()

    rows = session.exec(select(ProviderAccountMerge)).all()
    assert rows[0].source_account_id == "alice@example.com"
    assert rows[0].destination_account_id == "bob@example.com"
    assert resolve_merged_account(session, "openrouter", "ALICE@EXAMPLE.COM") == "bob@example.com"
