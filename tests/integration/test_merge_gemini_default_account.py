"""Integration test for scripts/merge_gemini_default_account.py.

The script is a thin CLI wrapper over
app/services/maintenance/account_merge.py, which has its own dedicated unit
tests — these just exercise the script's own `migrate` entry point (account
resolution, dry-run vs apply, CLI-level behavior).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import LatestUsage


@pytest.fixture(autouse=True)
def mock_db_session():
    """Override the conftest autouse Session mock — this test needs a real DB."""
    yield


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _card(session: Session, account_id: str) -> None:
    session.add(
        LatestUsage(
            provider_id="gemini",
            account_id=account_id,
            window_type="daily",
            variant="",
            model_id="pro",
            card_json=json.dumps({"account_id": account_id}),
        )
    )
    session.commit()


def test_migrate_auto_detects_the_target_email(engine):
    with Session(engine) as s:
        _card(s, "default")
        _card(s, "alice@example.com")

    with patch("scripts.merge_gemini_default_account.engine", engine):
        from scripts.merge_gemini_default_account import migrate

        rc = migrate("gemini", "default", None, apply=True)

    assert rc == 0
    with Session(engine) as s:
        rows = list(s.exec(select(LatestUsage)))
        assert len(rows) == 1
        assert rows[0].account_id == "alice@example.com"


def test_migrate_aborts_when_target_ambiguous(engine):
    with Session(engine) as s:
        _card(s, "default")
        _card(s, "alice@example.com")
        _card(s, "bob@example.com")

    with patch("scripts.merge_gemini_default_account.engine", engine):
        from scripts.merge_gemini_default_account import migrate

        rc = migrate("gemini", "default", None, apply=True)

    assert rc == 1
    with Session(engine) as s:
        # Nothing touched.
        accounts = {r.account_id for r in s.exec(select(LatestUsage))}
        assert accounts == {"default", "alice@example.com", "bob@example.com"}


def test_migrate_dry_run_writes_nothing(engine):
    with Session(engine) as s:
        _card(s, "default")
        _card(s, "alice@example.com")

    with patch("scripts.merge_gemini_default_account.engine", engine):
        from scripts.merge_gemini_default_account import migrate

        rc = migrate("gemini", "default", None, apply=False)

    assert rc == 0
    with Session(engine) as s:
        accounts = {r.account_id for r in s.exec(select(LatestUsage))}
        assert accounts == {"default", "alice@example.com"}


def test_migrate_explicit_email_overrides_autodetect(engine):
    with Session(engine) as s:
        _card(s, "default")
        _card(s, "alice@example.com")
        _card(s, "bob@example.com")

    with patch("scripts.merge_gemini_default_account.engine", engine):
        from scripts.merge_gemini_default_account import migrate

        rc = migrate("gemini", "default", "alice@example.com", apply=True)

    assert rc == 0
    with Session(engine) as s:
        accounts = {r.account_id for r in s.exec(select(LatestUsage))}
        assert accounts == {"alice@example.com", "bob@example.com"}
