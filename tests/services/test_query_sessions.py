"""Tests for query_sessions — specifically the by_model breakdown field."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models.db import UsageEvent
from app.services.queries.sessions import query_sessions


@pytest.fixture
def db_session():
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


_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
_SINCE = _NOW - timedelta(days=1)


def _add_event(
    session: Session,
    *,
    event_id: str,
    session_id: str,
    model_id: str | None,
    ts: datetime,
    tokens_input: int = 1_000,
    tokens_output: int = 500,
    tokens_cache_read: int = 0,
    tokens_cache_create: int = 0,
    tokens_reasoning: int = 0,
    cost_usd: float = 0.01,
    cost_input: float = 0.0,
    cost_output: float = 0.0,
    cost_cache_read: float = 0.0,
    cost_cache_create: float = 0.0,
    subagent_type: str | None = None,
    turn_id: str | None = None,
) -> None:
    ev = UsageEvent(
        provider_id="anthropic",
        account_id="acc1",
        event_id=event_id,
        session_id=session_id,
        model_id=model_id,
        ts=ts,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cache_read=tokens_cache_read,
        tokens_cache_create=tokens_cache_create,
        tokens_reasoning=tokens_reasoning,
        cost_usd=cost_usd,
        cost_input=cost_input,
        cost_output=cost_output,
        cost_cache_read=cost_cache_read,
        cost_cache_create=cost_cache_create,
        sidecar_id="local",
        kind="message",
        subagent_type=subagent_type,
        turn_id=turn_id,
    )
    session.add(ev)
    session.commit()


def _sessions(db_session: Session, **kwargs):
    return query_sessions(
        db_session,
        provider_id="anthropic",
        account_id="acc1",
        since=_SINCE,
        **kwargs,
    )


class TestByModelSingleModel:
    def test_single_model_has_one_entry(self, db_session):
        _add_event(
            db_session,
            event_id="e1",
            session_id="s1",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=2),
            tokens_input=2_000,
            tokens_output=1_000,
            cost_usd=0.05,
        )
        _add_event(
            db_session,
            event_id="e2",
            session_id="s1",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=1),
            tokens_input=500,
            tokens_output=200,
            cost_usd=0.01,
        )

        results = _sessions(db_session)
        assert len(results) == 1
        bm = results[0]["by_model"]
        assert len(bm) == 1
        assert bm[0]["model_id"] == "sonnet"
        assert bm[0]["msgs"] == 2
        assert bm[0]["tokens_input"] == 2_500
        assert bm[0]["tokens_output"] == 1_200
        assert bm[0]["tokens_total"] == 3_700
        assert abs(bm[0]["cost_usd"] - 0.06) < 1e-9


class TestByModelMultiModel:
    def test_multi_model_ordered_by_tokens_desc(self, db_session):
        # sonnet: 3 events, opus: 1 event (but higher tokens per event)
        _add_event(
            db_session,
            event_id="m1",
            session_id="s2",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=3),
            tokens_input=1_000,
            tokens_output=500,
            cost_usd=0.01,
        )
        _add_event(
            db_session,
            event_id="m2",
            session_id="s2",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=2, minutes=30),
            tokens_input=800,
            tokens_output=300,
            cost_usd=0.008,
        )
        _add_event(
            db_session,
            event_id="m3",
            session_id="s2",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=2),
            tokens_input=600,
            tokens_output=200,
            cost_usd=0.005,
        )
        _add_event(
            db_session,
            event_id="m4",
            session_id="s2",
            model_id="opus",
            ts=_NOW - timedelta(hours=1),
            tokens_input=5_000,
            tokens_output=3_000,
            cost_usd=0.20,
        )

        results = _sessions(db_session)
        assert len(results) == 1
        bm = results[0]["by_model"]
        assert len(bm) == 2
        # opus has more tokens_total → should be first
        assert bm[0]["model_id"] == "opus"
        assert bm[0]["msgs"] == 1
        assert bm[0]["tokens_total"] == 8_000
        assert bm[0]["tokens_input"] == 5_000
        assert bm[0]["tokens_output"] == 3_000

        assert bm[1]["model_id"] == "sonnet"
        assert bm[1]["msgs"] == 3
        assert bm[1]["tokens_total"] == 3_400  # (1000+500) + (800+300) + (600+200)

    def test_multi_model_cost_sums(self, db_session):
        _add_event(
            db_session,
            event_id="c1",
            session_id="s3",
            model_id="haiku",
            ts=_NOW - timedelta(hours=2),
            tokens_input=100,
            tokens_output=50,
            cost_usd=0.001,
        )
        _add_event(
            db_session,
            event_id="c2",
            session_id="s3",
            model_id="haiku",
            ts=_NOW - timedelta(hours=1, minutes=30),
            tokens_input=200,
            tokens_output=100,
            cost_usd=0.002,
        )
        _add_event(
            db_session,
            event_id="c3",
            session_id="s3",
            model_id="opus",
            ts=_NOW - timedelta(hours=1),
            tokens_input=1_000,
            tokens_output=500,
            cost_usd=0.10,
        )

        results = _sessions(db_session)
        bm = {e["model_id"]: e for e in results[0]["by_model"]}
        assert abs(bm["haiku"]["cost_usd"] - 0.003) < 1e-9
        assert abs(bm["opus"]["cost_usd"] - 0.10) < 1e-9

    def test_cache_tokens_included(self, db_session):
        _add_event(
            db_session,
            event_id="cr1",
            session_id="s4",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=2),
            tokens_input=1_000,
            tokens_output=500,
            tokens_cache_read=2_000,
            tokens_cache_create=300,
            cost_usd=0.05,
        )
        _add_event(
            db_session,
            event_id="cr2",
            session_id="s4",
            model_id="opus",
            ts=_NOW - timedelta(hours=1),
            tokens_input=500,
            tokens_output=200,
            cost_usd=0.02,
        )

        results = _sessions(db_session)
        bm = {e["model_id"]: e for e in results[0]["by_model"]}
        assert bm["sonnet"]["tokens_cache_read"] == 2_000
        assert bm["sonnet"]["tokens_cache_create"] == 300
        assert bm["sonnet"]["tokens_total"] == 1_000 + 500 + 2_000 + 300  # 3_800


class TestCostComponents:
    def test_cost_components_exposed_at_every_grain(self, db_session):
        # Two events, same session, split across a model and a subagent so the
        # session total, by_model, and subagents grains all carry components.
        _add_event(
            db_session,
            event_id="cc1",
            session_id="sc1",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=2),
            cost_usd=0.10,
            cost_input=0.04,
            cost_output=0.03,
            cost_cache_read=0.02,
            cost_cache_create=0.01,
        )
        ev = UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id="cc2",
            session_id="sc1",
            model_id="sonnet",
            subagent_type="Explore",
            ts=_NOW - timedelta(hours=1),
            tokens_input=1_000,
            tokens_output=500,
            cost_usd=0.05,
            cost_input=0.02,
            cost_output=0.02,
            cost_cache_read=0.005,
            cost_cache_create=0.005,
            sidecar_id="local",
            kind="message",
        )
        db_session.add(ev)
        db_session.commit()

        results = _sessions(db_session)
        assert len(results) == 1
        row = results[0]

        # Session-level totals sum both events.
        assert abs(row["cost_input"] - 0.06) < 1e-9
        assert abs(row["cost_output"] - 0.05) < 1e-9
        assert abs(row["cost_cache_read"] - 0.025) < 1e-9
        assert abs(row["cost_cache_create"] - 0.015) < 1e-9
        # Components sum to the authoritative total.
        component_sum = (
            row["cost_input"]
            + row["cost_output"]
            + row["cost_cache_read"]
            + row["cost_cache_create"]
        )
        assert abs(component_sum - row["cost_usd"]) < 1e-9

        bm = {e["model_id"]: e for e in row["by_model"]}
        assert abs(bm["sonnet"]["cost_input"] - 0.06) < 1e-9
        assert abs(bm["sonnet"]["cost_cache_read"] - 0.025) < 1e-9

        sa = {e["subagent_type"]: e for e in row["subagents"]}
        assert abs(sa["Explore"]["cost_input"] - 0.02) < 1e-9
        assert abs(sa["Explore"]["cost_cache_create"] - 0.005) < 1e-9


class TestByModelNullModelId:
    def test_null_model_excluded_from_by_model(self, db_session):
        _add_event(
            db_session,
            event_id="n1",
            session_id="s5",
            model_id="sonnet",
            ts=_NOW - timedelta(hours=2),
            tokens_input=1_000,
            tokens_output=500,
            cost_usd=0.05,
        )
        _add_event(
            db_session,
            event_id="n2",
            session_id="s5",
            model_id=None,
            ts=_NOW - timedelta(hours=1),
            tokens_input=200,
            tokens_output=100,
            cost_usd=0.01,
        )

        results = _sessions(db_session)
        assert len(results) == 1
        # NULL event still counted in session totals
        assert results[0]["msgs"] == 2
        assert results[0]["tokens_total"] == 1_000 + 500 + 200 + 100  # 1_800
        # by_model only covers non-NULL model_id
        bm = results[0]["by_model"]
        assert len(bm) == 1
        assert bm[0]["model_id"] == "sonnet"
        assert bm[0]["msgs"] == 1

    def test_all_null_models_gives_empty_by_model(self, db_session):
        _add_event(
            db_session,
            event_id="a1",
            session_id="s6",
            model_id=None,
            ts=_NOW - timedelta(hours=1),
            tokens_input=500,
            tokens_output=200,
            cost_usd=0.01,
        )

        results = _sessions(db_session)
        assert len(results) == 1
        assert results[0]["by_model"] == []


class TestByModelEmptyWindow:
    def test_no_sessions_in_window_returns_empty(self, db_session):
        # event outside the since window
        _add_event(
            db_session,
            event_id="old",
            session_id="s7",
            model_id="sonnet",
            ts=_NOW - timedelta(days=10),
            tokens_input=100,
            tokens_output=50,
            cost_usd=0.001,
        )

        results = _sessions(db_session)
        assert results == []


class TestByModelSessionIdRequired:
    def test_events_without_session_id_excluded(self, db_session):
        ev = UsageEvent(
            provider_id="anthropic",
            account_id="acc1",
            event_id="nosess",
            session_id=None,
            model_id="sonnet",
            ts=_NOW - timedelta(hours=1),
            tokens_input=1_000,
            tokens_output=500,
            cost_usd=0.05,
            sidecar_id="local",
            kind="message",
        )
        db_session.add(ev)
        db_session.commit()

        results = _sessions(db_session)
        assert results == []


class TestSubagentTurns:
    """The Agents card counts turns, not the events behind them."""

    def test_turns_are_counted_distinctly_when_the_provider_names_a_turn(self, db_session):
        # A provider that emits one event per request (MiniMax Code): six
        # requests, one turn.
        for index in range(6):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                subagent_type="Explore",
                turn_id="turn_1",
            )
        row = _sessions(db_session)[0]
        explore = {e["subagent_type"]: e for e in row["subagents"]}["Explore"]
        assert explore["turns"] == 1
        assert explore["requests"] == 6

    def test_several_turns_are_counted_separately(self, db_session):
        for index in range(4):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                subagent_type="Explore",
                turn_id=f"turn_{index % 2}",
            )
        row = _sessions(db_session)[0]
        explore = {e["subagent_type"]: e for e in row["subagents"]}["Explore"]
        assert explore["turns"] == 2
        assert explore["requests"] == 4

    def test_a_provider_without_a_turn_id_keeps_the_event_count(self, db_session):
        # Claude Code emits one event per message, so the turn count is the
        # event count and nothing changes.
        for index in range(3):
            _add_event(
                db_session,
                event_id=f"c{index}",
                session_id="s2",
                model_id="m",
                ts=_NOW,
                subagent_type="Explore",
            )
        row = _sessions(db_session)[0]
        explore = {e["subagent_type"]: e for e in row["subagents"]}["Explore"]
        assert explore["turns"] == 3
        assert explore["requests"] == 3

    def test_the_requests_do_not_change_the_token_totals(self, db_session):
        for index in range(6):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                tokens_input=100,
                tokens_output=10,
                subagent_type="Explore",
                turn_id="turn_1",
            )
        row = _sessions(db_session)[0]
        explore = {e["subagent_type"]: e for e in row["subagents"]}["Explore"]
        assert explore["tokens_total"] == 6 * 110


class TestSessionMessageCount:
    """The session's Messages column counts turns, not events."""

    def test_turns_are_counted_distinctly_when_the_provider_names_a_turn(self, db_session):
        # A provider that emits one event per request (MiniMax Code): four
        # requests, one turn.
        for index in range(4):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                turn_id="turn_1",
            )
        row = _sessions(db_session)[0]
        assert row["msgs"] == 1

    def test_several_turns_are_counted_separately(self, db_session):
        for index in range(4):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                turn_id=f"turn_{index % 2}",
            )
        row = _sessions(db_session)[0]
        assert row["msgs"] == 2

    def test_a_provider_without_a_turn_id_keeps_the_event_count(self, db_session):
        # Claude Code emits one event per message, so the turn count is the
        # event count and nothing changes.
        for index in range(3):
            _add_event(
                db_session,
                event_id=f"c{index}",
                session_id="s2",
                model_id="m",
                ts=_NOW,
            )
        row = _sessions(db_session)[0]
        assert row["msgs"] == 3

    def test_the_by_model_breakdown_counts_turns_too(self, db_session):
        """The breakdown lives in the session's own panel — same unit."""
        for index in range(4):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                turn_id="turn_1",
            )
        row = _sessions(db_session)[0]
        by_model = {m["model_id"]: m for m in row["by_model"]}
        assert by_model["m"]["msgs"] == 1

    def test_the_message_count_agrees_with_the_agents_card(self, db_session):
        """The session row and its Agents card speak the same unit."""
        for index in range(4):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                turn_id="turn_1",
                subagent_type="Explore",
            )
        row = _sessions(db_session)[0]
        explore = {a["subagent_type"]: a for a in row["subagents"]}["Explore"]
        assert explore["turns"] == 1
        assert explore["requests"] == 4
        # The card counts the same turns the row does.
        assert row["msgs"] == explore["turns"]


class TestMessageCountEdgeCases:
    """The guards around the turn-aware message count."""

    def test_an_empty_turn_id_falls_back_to_the_event_count(self, db_session):
        """A writer that stores an empty turn id must not collapse the count."""
        for index in range(3):
            _add_event(
                db_session,
                event_id=f"c{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                turn_id="",
            )
        row = _sessions(db_session)[0]
        assert row["msgs"] == 3

    def test_the_subagent_message_count_speaks_the_same_unit(self, db_session):
        """It sits in the same response object as the row's message count."""
        for index in range(4):
            _add_event(
                db_session,
                event_id=f"t{index}",
                session_id="s1",
                model_id="m",
                ts=_NOW,
                turn_id="turn_1",
                subagent_type="Explore",
            )
        row = _sessions(db_session)[0]
        assert row["subagent_msgs"] == 1

    def test_a_turn_that_used_two_models_is_counted_in_both_buckets(self, db_session):
        """A turn is attributed to every model it touched — like its tokens.

        The session row counts the turn once; the per-model buckets count it
        once per model, so a turn that used two models appears in both. The
        breakdown is scoped by the model, so this is the same attribution the
        token breakdown already does.
        """
        _add_event(
            db_session,
            event_id="t1",
            session_id="s1",
            model_id="opus",
            ts=_NOW,
            turn_id="turn_1",
        )
        _add_event(
            db_session,
            event_id="t2",
            session_id="s1",
            model_id="sonnet",
            ts=_NOW,
            turn_id="turn_1",
        )
        row = _sessions(db_session)[0]
        assert row["msgs"] == 1
        by_model = {m["model_id"]: m for m in row["by_model"]}
        assert by_model["opus"]["msgs"] == 1
        assert by_model["sonnet"]["msgs"] == 1
