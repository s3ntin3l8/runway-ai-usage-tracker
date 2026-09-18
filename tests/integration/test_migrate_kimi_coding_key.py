"""Integration test for scripts/migrate_kimi_coding_key.py.

Seeds provider_configs reproducing the misfiled-credential pattern (API key
in the session_cookie slot, a genuine kimi-auth JWT that must stay put, an
already-correct row), runs the migration, and asserts only the API key moves.
"""

import sys
from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ProviderConfig


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


def _run(flag: str) -> None:
    with patch("scripts.migrate_kimi_coding_key.engine"):
        import scripts.migrate_kimi_coding_key as m

        argv = sys.argv
        try:
            sys.argv = ["migrate_kimi_coding_key.py", flag]
            m.main()
        finally:
            sys.argv = argv


# A real kimi-auth JWT is three dot-separated base64url segments.
_FAKE_JWT = "aaa.bbb.ccc"
_API_KEY = "sk-kimi-code-api-key-123"


def test_apply_moves_api_key_and_keeps_jwt(engine, monkeypatch):
    with Session(engine) as s:
        s.add(ProviderConfig(provider_id="kimi_coding", account_id="default"))
        row = s.exec(select(ProviderConfig)).one()
        row.session_cookie = _API_KEY
        s.add(ProviderConfig(provider_id="kimi_coding", account_id="second"))
        row2 = s.exec(select(ProviderConfig).where(ProviderConfig.account_id == "second")).one()
        row2.session_cookie = _FAKE_JWT
        s.add(ProviderConfig(provider_id="kimi_coding", account_id="third"))
        row3 = s.exec(select(ProviderConfig).where(ProviderConfig.account_id == "third")).one()
        row3.api_key = "already-set"
        row3.session_cookie = "left-alone"
        s.commit()

    import scripts.migrate_kimi_coding_key as m

    monkeypatch.setattr(m, "engine", engine)
    argv = sys.argv
    try:
        sys.argv = ["migrate_kimi_coding_key.py", "--apply"]
        assert m.main() == 0
    finally:
        sys.argv = argv

    with Session(engine) as s:
        rows = {r.account_id: r for r in s.exec(select(ProviderConfig)).all()}

        # Non-JWT value moves to the api_key slot; session_cookie cleared.
        assert rows["default"].api_key == _API_KEY
        assert rows["default"].session_cookie is None

        # JWT-shaped value stays in the session_cookie slot (web strategy).
        assert rows["second"].api_key is None
        assert rows["second"].session_cookie == _FAKE_JWT

        # Row with an existing api_key is untouched.
        assert rows["third"].api_key == "already-set"
        assert rows["third"].session_cookie == "left-alone"


def test_dry_run_moves_nothing(engine, monkeypatch):
    with Session(engine) as s:
        s.add(ProviderConfig(provider_id="kimi_coding", account_id="default"))
        row = s.exec(select(ProviderConfig)).one()
        row.session_cookie = _API_KEY
        s.commit()

    import scripts.migrate_kimi_coding_key as m

    monkeypatch.setattr(m, "engine", engine)
    argv = sys.argv
    try:
        sys.argv = ["migrate_kimi_coding_key.py", "--dry-run"]
        assert m.main() == 0
    finally:
        sys.argv = argv

    with Session(engine) as s:
        row = s.exec(select(ProviderConfig)).one()
        assert row.session_cookie == _API_KEY
        assert row.api_key is None
