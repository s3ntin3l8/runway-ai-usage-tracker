"""Tests for the MiniMax Code background-session repair."""

import os
import tempfile
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.db import UsageEvent
from app.services.maintenance.minimax_background_sessions import (
    BackgroundLink,
    apply_background_attribution,
    plan_background_attribution,
)

_NOW = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def db_session():
    fd, db_path = tempfile.mkstemp()
    os.close(fd)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()
        os.remove(db_path)


def _add_event(
    session: Session,
    *,
    event_id: str,
    session_id: str = "mvs_bg",
    subagent_type: str | None = None,
    turn_id: str | None = None,
    tokens_input: int = 100,
    tokens_output: int = 10,
    cost_usd: float = 0.5,
) -> UsageEvent:
    ev = UsageEvent(
        provider_id="minimax",
        account_id="acc1",
        event_id=event_id,
        session_id=session_id,
        model_id="MiniMax-M3.1-Flash",
        ts=_NOW,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cost_usd=cost_usd,
        sidecar_id="local",
        kind="message",
        source_app="minimax_code",
        subagent_type=subagent_type,
        turn_id=turn_id,
    )
    session.add(ev)
    session.commit()
    return ev


def _link(event_id: str, **kwargs) -> BackgroundLink:
    return BackgroundLink(
        event_id=event_id,
        parent_session_id=kwargs.pop("parent_session_id", "mvs_main"),
        subagent_type=kwargs.pop("subagent_type", "Explore"),
        **kwargs,
    )


def test_the_apply_moves_the_row_under_the_parent(db_session):
    _add_event(db_session, event_id="e1")
    plan = plan_background_attribution(
        db_session,
        [_link("e1", turn_id="turn_1", raw_json='{"source_session_id": "mvs_bg"}')],
    )
    assert plan.total == 1
    assert len(plan.linked) == 1

    result = apply_background_attribution(db_session, plan)
    assert result.linked == 1

    row = db_session.exec(
        UsageEvent.__table__.select().where(UsageEvent.__table__.c.event_id == "e1")
    ).one()
    assert row.session_id == "mvs_main"
    assert row.subagent_type == "Explore"
    assert row.turn_id == "turn_1"
    assert row.raw_json == '{"source_session_id": "mvs_bg"}'


def test_the_apply_never_inserts_a_row(db_session):
    _add_event(db_session, event_id="e1")
    before = len(db_session.exec(select(UsageEvent)).all())
    plan = plan_background_attribution(db_session, [_link("e1", turn_id="turn_1")])
    apply_background_attribution(db_session, plan)
    assert len(db_session.exec(select(UsageEvent)).all()) == before


def test_the_apply_leaves_the_totals_untouched(db_session):
    _add_event(db_session, event_id="e1", tokens_input=100, tokens_output=10, cost_usd=0.5)
    before = db_session.exec(
        UsageEvent.__table__.select().where(UsageEvent.__table__.c.event_id == "e1")
    ).one()
    plan = plan_background_attribution(db_session, [_link("e1", turn_id="turn_1")])
    apply_background_attribution(db_session, plan)
    after = db_session.exec(
        UsageEvent.__table__.select().where(UsageEvent.__table__.c.event_id == "e1")
    ).one()
    assert after.tokens_input == before.tokens_input
    assert after.tokens_output == before.tokens_output
    assert after.cost_usd == before.cost_usd
    assert after.ts == before.ts


def test_the_apply_is_idempotent(db_session):
    _add_event(db_session, event_id="e1")
    link = _link("e1", turn_id="turn_1")
    first = plan_background_attribution(db_session, [link])
    apply_background_attribution(db_session, first)

    second = plan_background_attribution(db_session, [link])
    assert second.already_linked == 1
    assert second.linked == []
    result = apply_background_attribution(db_session, second)
    assert result.linked == 0
    assert result.already_linked == 1


def test_a_link_without_a_stored_row_is_reported_not_invented(db_session):
    plan = plan_background_attribution(db_session, [_link("missing")])
    assert plan.missing == ["missing"]
    assert plan.linked == []
    result = apply_background_attribution(db_session, plan)
    assert result.linked == 0
    assert result.missing == 1
    assert len(db_session.exec(select(UsageEvent)).all()) == 0


def test_rows_of_another_client_app_are_left_alone(db_session):
    ev = UsageEvent(
        provider_id="minimax",
        account_id="acc1",
        event_id="e1",
        session_id="mvs_bg",
        model_id="MiniMax-M3.1-Flash",
        ts=_NOW,
        tokens_input=100,
        sidecar_id="local",
        kind="message",
        source_app="minimax_api",
    )
    db_session.add(ev)
    db_session.commit()

    plan = plan_background_attribution(db_session, [_link("e1", turn_id="turn_1")])
    assert plan.missing == ["e1"]
    assert plan.linked == []
    assert db_session.get(UsageEvent, ev.id).session_id == "mvs_bg"


def test_a_row_collected_before_the_source_app_column_is_still_repaired(db_session):
    """The backfill gives a pre-column row the provider id as its source app."""
    ev = UsageEvent(
        provider_id="minimax",
        account_id="acc1",
        event_id="e1",
        session_id="mvs_bg",
        model_id="MiniMax-M3.1-Flash",
        ts=_NOW,
        tokens_input=100,
        sidecar_id="local",
        kind="message",
        source_app="minimax",
    )
    db_session.add(ev)
    db_session.commit()

    plan = plan_background_attribution(db_session, [_link("e1", turn_id="turn_1")])
    assert len(plan.linked) == 1
    apply_background_attribution(db_session, plan)
    assert db_session.get(UsageEvent, ev.id).session_id == "mvs_main"


def test_a_link_without_a_parent_is_only_tagged(db_session):
    """A parent that is not in the store is never invented — but the row is
    still labelled as the background work it is, exactly as the extractor
    labels it."""
    _add_event(db_session, event_id="e1")
    link = _link("e1", turn_id="turn_1", parent_session_id=None)
    plan = plan_background_attribution(db_session, [link])
    assert len(plan.linked) == 1

    apply_background_attribution(db_session, plan)
    row = db_session.exec(
        UsageEvent.__table__.select().where(UsageEvent.__table__.c.event_id == "e1")
    ).one()
    # The row keeps its own session id; only the tag and the turn are written.
    assert row.session_id == "mvs_bg"
    assert row.subagent_type == "Explore"
    assert row.turn_id == "turn_1"


def test_a_tagged_link_without_a_parent_is_idempotent(db_session):
    _add_event(db_session, event_id="e1")
    link = _link("e1", turn_id="turn_1", parent_session_id=None)
    apply_background_attribution(db_session, plan_background_attribution(db_session, [link]))

    plan = plan_background_attribution(db_session, [link])
    assert plan.already_linked == 1
    assert plan.linked == []


def test_the_script_keeps_a_background_task_whose_parent_is_missing(tmp_path):
    """The repair must not drop it: the rows are still the background work."""
    from scripts.link_minimax_background_sessions import collect_links
    from tests.fixtures.minimax_code_fixture import make_minimax_code_store

    db_path = make_minimax_code_store(
        tmp_path,
        sessions=[
            {
                "session_id": "mvs_bg",
                "agent_name": "explore",
                "workspace_dir": "/home/user/project",
                "extra_data_json": {"effectiveModel": "minimax/MiniMax-M3.1-Flash"},
                # The parent session row is gone — it was pruned or reset.
                "parent_session_id": "mvs_main",
                "session_kind": "task",
                "purpose": "local-background-task:bg_1234",
            },
        ],
        usage=[
            {
                "id": 10,
                "session_id": "mvs_bg",
                "agent_name": "explore",
                "turn_id": "turn_task_bg_bg_1234",
                "ts": 1792699951617,
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "cache_read_tokens": 500,
                "cache_write_tokens": 0,
            }
        ],
        parent_links=True,
    )

    links = collect_links(db_path)
    assert len(links) == 1
    assert links[0].parent_session_id is None
    assert links[0].subagent_type == "Explore"
    assert links[0].turn_id == "turn_task_bg_bg_1234"


def test_the_script_builds_the_links_from_the_runtime_store(tmp_path):
    """The script's mapping must match the ids the sidecar pushes."""
    from scripts.link_minimax_background_sessions import collect_links
    from tests.fixtures.minimax_code_fixture import make_minimax_code_store

    db_path = make_minimax_code_store(
        tmp_path,
        sessions=[
            {
                "session_id": "mvs_main",
                "workspace_dir": "/home/user/project",
                "extra_data_json": {"effectiveModel": "minimax/MiniMax-M3.1-Flash"},
            },
            {
                "session_id": "mvs_bg",
                "agent_name": "explore",
                "workspace_dir": "/home/user/project",
                "extra_data_json": {"effectiveModel": "minimax/MiniMax-M3.1-Flash"},
                "parent_session_id": "mvs_main",
                "session_kind": "task",
                "purpose": "local-background-task:bg_1234",
            },
        ],
        usage=[
            {
                "id": 10,
                "session_id": "mvs_bg",
                "agent_name": "explore",
                "turn_id": "turn_task_bg_bg_1234",
                "ts": 1792699951617,
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "cache_read_tokens": 500,
                "cache_write_tokens": 0,
            }
        ],
        parent_links=True,
    )

    links = collect_links(db_path)
    assert len(links) == 1
    link = links[0]
    assert link.parent_session_id == "mvs_main"
    assert link.subagent_type == "Explore"
    assert link.turn_id == "turn_task_bg_bg_1234"
    # The id the sidecar pushes for the same ledger row.
    assert link.event_id == "mcode:mvs_bg:turn_task_bg_bg_1234:1792699951617:100:20:500"
