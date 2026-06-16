"""Unit tests for session minting / verification / rotation (app/core/sessions.py)."""

import os
import tempfile

import pytest
from sqlmodel import SQLModel, create_engine

import app.core.sessions as sessions


@pytest.fixture
def temp_engine(monkeypatch):
    """Point sessions.py at a throwaway DB so it never touches the real one."""
    fd, path = tempfile.mkstemp()
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(sessions, "engine", engine)
    monkeypatch.setattr(sessions, "_signer", None)
    yield engine
    engine.dispose()
    os.close(fd)
    if os.path.exists(path):
        os.remove(path)


def test_issue_and_verify(temp_engine):
    assert sessions.verify_session(sessions.issue_session(remember=False)) is True


def test_verify_rejects_empty_and_garbage(temp_engine):
    assert sessions.verify_session(None) is False
    assert sessions.verify_session("") is False
    assert sessions.verify_session("not-a-fernet-token") is False


def test_rotate_invalidates_existing_but_not_new(temp_engine):
    token = sessions.issue_session(remember=True)
    assert sessions.verify_session(token) is True
    sessions.rotate_secret()
    assert sessions.verify_session(token) is False
    # Fresh tokens minted after rotation still verify.
    assert sessions.verify_session(sessions.issue_session(remember=False)) is True


def test_expired_token_rejected(temp_engine, monkeypatch):
    monkeypatch.setattr(sessions.settings, "SESSION_LIFETIME_HOURS", 0)
    token = sessions.issue_session(remember=False)
    assert sessions.verify_session(token) is False


def test_secret_persists_across_signer_reload(temp_engine):
    token = sessions.issue_session(remember=False)
    # Drop the in-memory signer; reloading it from the DB must verify the token.
    sessions._signer = None
    assert sessions.verify_session(token) is True


def test_remember_uses_longer_lifetime(monkeypatch):
    monkeypatch.setattr(sessions.settings, "SESSION_LIFETIME_HOURS", 12)
    monkeypatch.setattr(sessions.settings, "SESSION_REMEMBER_DAYS", 30)
    assert sessions.session_max_age(remember=False) == 12 * 3600
    assert sessions.session_max_age(remember=True) == 30 * 24 * 3600
