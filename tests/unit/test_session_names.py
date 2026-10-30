"""Session names: cleaning, upsert semantics, precedence, and the no-logging rule."""

import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import SessionName
from app.models.schemas import SESSION_NAMES_MAX_PER_REQUEST, IngestRequest, SessionNameIn
from app.services import session_names as svc
from app.services.session_names import resolve_names, upsert_session_names


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _n(name, kind="generated", sid="sess-1", provider="anthropic", app="claude_code"):
    return SessionNameIn(provider_id=provider, session_id=sid, source_app=app, name=name, kind=kind)


def test_cleaning_strips_controls_collapses_whitespace_and_truncates():
    assert _n("  Fake\x00 name\t\n  here\x1b  ").name == "Fake name here"
    assert len(_n("x" * 500).name) == 200
    assert _n("   \x00\x01 ").name == ""


def test_cleaning_keeps_joiners_and_drops_bidi_overrides():
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert _n(f"Fake {family}").name == f"Fake {family}"
    assert _n("a\u202eb\u2066c").name == "abc"


def test_source_app_is_refreshed_with_the_row(db):
    upsert_session_names(db, [_n("Same", app="old_app")])
    assert upsert_session_names(db, [_n("Same", app="new_app")]) == 1
    assert db.exec(select(SessionName)).one().source_app == "new_app"


def test_kind_is_validated():
    with pytest.raises(ValueError):
        SessionNameIn(provider_id="p", session_id="s", name="n", kind="other")  # type: ignore[arg-type]


def test_ingest_request_drops_bad_entries_and_caps_the_list():
    good = {"provider_id": "p", "session_id": "s", "name": "Fake", "kind": "agent"}
    req = IngestRequest.model_validate(
        {
            "provider": "x",
            "session_names": [
                good,
                {**good, "kind": "bogus"},
                {**good, "name": "  "},
                "not-a-dict",
            ],
        }
    )
    assert req.session_names is not None and len(req.session_names) == 1

    many = [{**good, "session_id": f"s{i}"} for i in range(SESSION_NAMES_MAX_PER_REQUEST + 50)]
    capped = IngestRequest.model_validate({"provider": "x", "session_names": many})
    assert capped.session_names is not None
    assert len(capped.session_names) == SESSION_NAMES_MAX_PER_REQUEST
    assert IngestRequest.model_validate({"provider": "x"}).session_names is None
    assert IngestRequest.model_validate({"provider": "x", "session_names": 5}).session_names is None


def test_insert_then_latest_wins_per_kind(db):
    assert upsert_session_names(db, [_n("First", "generated"), _n("Second", "generated")]) == 1
    assert upsert_session_names(db, [_n("Renamed", "generated")]) == 1
    rows = db.exec(select(SessionName)).all()
    assert [(r.kind, r.name) for r in rows] == [("generated", "Renamed")]


def test_unchanged_name_is_a_noop(db):
    upsert_session_names(db, [_n("Same")])
    row = db.exec(select(SessionName)).one()
    old = datetime(2020, 1, 1, tzinfo=UTC)
    row.updated_at = old
    db.add(row)
    db.commit()
    assert upsert_session_names(db, [_n("Same")]) == 0
    db.refresh(row)
    assert row.updated_at.replace(tzinfo=UTC) == old


def test_empty_input_and_empty_names_write_nothing(db):
    assert upsert_session_names(db, None) == 0
    assert upsert_session_names(db, []) == 0
    blank = SessionNameIn.model_construct(
        provider_id="p", session_id="s", source_app=None, name="", kind="agent"
    )
    assert upsert_session_names(db, [blank]) == 0
    assert db.exec(select(SessionName)).all() == []


def test_kinds_and_providers_are_separate_rows(db):
    n = upsert_session_names(
        db, [_n("A", "agent"), _n("G", "generated"), _n("A", "agent", provider="other")]
    )
    assert n == 3


def test_chunked_upsert(db, monkeypatch):
    monkeypatch.setattr(svc, "_CHUNK", 3)
    names = [_n(f"Name {i}", sid=f"s{i}") for i in range(10)]
    assert upsert_session_names(db, names) == 10
    assert upsert_session_names(db, names) == 0
    assert len(resolve_names(db, "anthropic", [f"s{i}" for i in range(10)])) == 10


def test_resolve_precedence_agent_over_generated_over_none(db):
    upsert_session_names(
        db,
        [
            _n("Gen", "generated", sid="both"),
            _n("Agent", "agent", sid="both"),
            _n("OnlyGen", "generated", sid="gen"),
        ],
    )
    got = resolve_names(db, "anthropic", ["both", "gen", "none", "both", ""])
    assert got == {"both": "Agent", "gen": "OnlyGen"}
    assert resolve_names(db, "other", ["both"]) == {}
    assert resolve_names(db, "anthropic", []) == {}


def test_lock_is_retried_then_succeeds(db, monkeypatch):
    calls = {"n": 0}
    real = svc._upsert_once

    def flaky(session, latest):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("stmt", {}, Exception("database is locked"))
        return real(session, latest)

    monkeypatch.setattr(svc, "_upsert_once", flaky)
    monkeypatch.setattr(svc, "_LOCK_BACKOFF_SECONDS", 0)
    assert upsert_session_names(db, [_n("Fake")]) == 1


def test_lock_gives_up_after_retries(db, monkeypatch):
    def locked(session, latest):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(svc, "_upsert_once", locked)
    monkeypatch.setattr(svc, "_LOCK_BACKOFF_SECONDS", 0)
    with pytest.raises(OperationalError):
        upsert_session_names(db, [_n("Fake")])


def test_upsert_never_logs_the_name(db, caplog):
    fake_title = "Confidential wording zz9"
    with caplog.at_level(logging.DEBUG):
        upsert_session_names(db, [_n(fake_title)])
        upsert_session_names(db, [_n(fake_title + " v2")])
        resolve_names(db, "anthropic", ["sess-1"])
    assert "Confidential" not in caplog.text
