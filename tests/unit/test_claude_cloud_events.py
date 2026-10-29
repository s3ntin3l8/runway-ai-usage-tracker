"""The cloud-runner history reducer: message events, per-turn remainders, project context."""

from collections import Counter
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.core.db import SQLITE_CONNECT_ARGS, configure_sqlite_engine
from app.models.db import UsageEvent
from app.services.claude_cloud_events import (
    ClaudeCloudHistoryReducer,
    ReducerState,
    SessionFacts,
    session_digest,
)
from app.services.event_ingestor import EventIngestor
from app.services.pricing_seed import seed_pricing_table
from app.services.project_label import derive_project
from app.services.queries.sessions import query_sessions
from tests.fixtures.claude_cloud_history import assistant, result, system

DIGEST = session_digest("cse_fixture_session")
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"
FIELDS = (
    ("inputTokens", "tokens_input"),
    ("outputTokens", "tokens_output"),
    ("cacheReadInputTokens", "tokens_cache_read"),
    ("cacheCreationInputTokens", "tokens_cache_create"),
)


def msg(**fields):
    """The fields of one assistant record in a turn."""
    return fields


def facts(**changes):
    base = {"digest": DIGEST, "account_id": "holder@example.com", "repos": ("the-repo",)}
    return SessionFacts(**{**base, **changes})


def run(history, **changes):
    reducer = ClaudeCloudHistoryReducer(facts(**changes))
    events = [push for record in history for push in reducer.feed(record)]
    return events, reducer


def totals(events):
    """Token totals per model slug over the events."""
    out: dict[str, Counter] = {}
    for push in events:
        bucket = out.setdefault(push.model_id, Counter())
        for _, attr in FIELDS:
            bucket[attr] += getattr(push, attr)
    return out


def turn(first_seq, messages, usage, *, at=None):
    """A turn: its assistant records, then the result carrying cumulative `usage`."""
    seq = first_seq
    records = []
    for message in messages:
        records.append(assistant(seq, **message))
        seq += 1
    records.append(result(seq, usage, at=at))
    return records


def test_a_message_event_carries_the_record():
    history = [
        system(1, cwd="/home/user/the-repo", branch="feature", claude_code_version="2.1.281"),
        assistant(
            2,
            "msg_a",
            inp=10,
            out=20,
            read=300,
            write_1h=40,
            write_5m=5,
            tools=("Bash", "Read"),
            stop="end_turn",
        ),
        result(
            3,
            {
                OPUS: {
                    "inputTokens": 10,
                    "outputTokens": 20,
                    "cacheReadInputTokens": 300,
                    "cacheCreationInputTokens": 45,
                }
            },
        ),
    ]
    events, reducer = run(history, effort="medium")
    assert len(events) == 1  # the result adds nothing the message did not already hold
    push = events[0]
    assert push.event_id == f"cloud:{DIGEST}:m:msg_a"
    assert push.model_id == "opus-5.5" and push.provider_id == "anthropic"
    assert (push.tokens_input, push.tokens_output, push.tokens_cache_read) == (10, 20, 300)
    assert (push.tokens_cache_create, push.tokens_cache_create_1h, push.tokens_cache_create_5m) == (
        45,
        40,
        5,
    )
    assert push.tool_names == ["Bash", "Read"] and push.tool_calls == 2
    assert push.stop_reason == "end_turn" and push.service_tier == "standard"
    assert push.ts == "2026-09-24T10:00:02+00:00"
    assert push.entrypoint == "cloud" and push.source_app == "claude_code"
    assert push.session_id == DIGEST and push.turn_id is None
    assert push.project_ref == "the-repo" and push.cwd == "/home/user/the-repo"
    assert push.git_branch == "feature" and push.app_version == "2.1.281"
    assert push.effort == "medium" and push.subagent_type is None
    assert reducer.stats["turns"] == 1 and reducer.stats["messages"] == 1


def test_the_remainder_holds_what_the_messages_omit():
    history = turn(
        1,
        [msg(message_id="msg_a", inp=10, out=5, read=100, write_1h=20)],
        {
            OPUS: {
                "inputTokens": 12,
                "outputTokens": 90,
                "cacheReadInputTokens": 160,
                "cacheCreationInputTokens": 20,
            }
        },
    )
    events, _ = run(history)
    message, remainder = events
    assert remainder.event_id == f"cloud:{DIGEST}:r:1:opus-5.5"
    assert (remainder.tokens_input, remainder.tokens_output, remainder.tokens_cache_read) == (
        2,
        85,
        60,
    )
    assert remainder.tokens_cache_create == 0
    assert remainder.turn_id == message.event_id  # one turn in `msgs`, not two
    assert remainder.subagent_type is None and remainder.tool_names is None
    assert remainder.ts == "2026-09-24T10:00:02+00:00"  # dated at the result


def test_per_turn_totals_equal_the_model_usage_delta_with_a_side_model():
    cumulative_1 = {
        OPUS: {
            "inputTokens": 40,
            "outputTokens": 700,
            "cacheReadInputTokens": 5000,
            "cacheCreationInputTokens": 900,
        },
        HAIKU: {"inputTokens": 30, "outputTokens": 9, "cacheCreationInputTokens": 400},
    }
    cumulative_2 = {
        OPUS: {
            "inputTokens": 55,
            "outputTokens": 950,
            "cacheReadInputTokens": 9400,
            "cacheCreationInputTokens": 1300,
        },
        HAIKU: {"inputTokens": 30, "outputTokens": 9, "cacheCreationInputTokens": 400},
    }
    history = [
        *turn(
            1,
            [
                msg(message_id="m1", inp=20, out=3, read=2000, write_1h=500),
                msg(message_id="m2", inp=10, out=2, read=2500, write_1h=300, write_5m=50),
            ],
            cumulative_1,
            at=10,
        ),
        *turn(
            20, [msg(message_id="m3", inp=15, out=4, read=4000, write_1h=400)], cumulative_2, at=30
        ),
    ]
    events, reducer = run(history)
    by_model = totals(events)
    assert dict(by_model["opus-5.5"]) == {
        "tokens_input": 55,
        "tokens_output": 950,
        "tokens_cache_read": 9400,
        "tokens_cache_create": 1300,
    }
    assert dict(by_model["haiku-4.5"]) == {
        "tokens_input": 30,
        "tokens_output": 9,
        "tokens_cache_read": 0,
        "tokens_cache_create": 400,
    }
    # Haiku only ever appears as remainder events, one in the first turn and none in the second.
    assert [e.event_id for e in events if e.model_id == "haiku-4.5"] == [
        f"cloud:{DIGEST}:r:1:haiku-4.5"
    ]
    assert reducer.stats["remainders"] == 3  # opus x2, haiku x1


def test_a_haiku_only_remainder_joins_the_turns_last_message():
    cumulative = {
        HAIKU: {"inputTokens": 7, "outputTokens": 1},
        OPUS: {"inputTokens": 5, "outputTokens": 5},
    }
    events, _ = run(turn(1, [msg(message_id="m1", inp=5, out=5)], cumulative))
    haiku = next(e for e in events if e.model_id == "haiku-4.5")
    assert haiku.turn_id == f"cloud:{DIGEST}:m:m1"


def test_a_turn_without_messages_keeps_its_own_turn_id():
    events, _ = run([result(1, {HAIKU: {"inputTokens": 7, "outputTokens": 1}})])
    assert len(events) == 1 and events[0].turn_id == events[0].event_id


def test_a_restart_counts_the_new_value_not_a_negative_delta():
    first = {
        OPUS: {
            "inputTokens": 100,
            "outputTokens": 400,
            "cacheReadInputTokens": 9000,
            "cacheCreationInputTokens": 700,
        }
    }
    after_restart = {
        OPUS: {
            "inputTokens": 12,
            "outputTokens": 40,
            "cacheReadInputTokens": 800,
            "cacheCreationInputTokens": 60,
        }
    }
    continued = {
        OPUS: {
            "inputTokens": 15,
            "outputTokens": 70,
            "cacheReadInputTokens": 1700,
            "cacheCreationInputTokens": 60,
        }
    }
    history = [result(1, first), result(2, after_restart), result(3, continued)]
    events, reducer = run(history)
    assert dict(totals(events)["opus-5.5"]) == {
        "tokens_input": 115,
        "tokens_output": 470,
        "tokens_cache_read": 10700,
        "tokens_cache_create": 760,
    }
    assert reducer.stats["restarts"] == 1 and reducer.stats["negative_remainder"] == 0


def test_a_negative_remainder_is_clamped_and_counted():
    history = turn(
        1,
        [msg(message_id="m1", inp=10, out=50, read=100)],
        {OPUS: {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 100}},
    )
    events, reducer = run(history)
    assert [e.event_id for e in events] == [f"cloud:{DIGEST}:m:m1"]  # no event for a clamp
    assert reducer.stats["negative_remainder"] == 1


def test_synthetic_messages_are_skipped():
    history = turn(
        1, [msg(message_id="m1", model="<synthetic>", inp=1), msg(message_id="m2")], {OPUS: {}}
    )
    events, reducer = run(history)
    assert [e.event_id for e in events] == [f"cloud:{DIGEST}:m:m2"]
    assert reducer.stats["synthetic_skipped"] == 1


def test_the_last_record_of_a_message_id_wins_and_keeps_its_position():
    history = [
        assistant(1, "m1", out=1),
        assistant(2, "m2", out=7),
        assistant(3, "m1", out=40, tools=("Edit",)),
        result(4, {OPUS: {"outputTokens": 47}}),
    ]
    events, _ = run(history)
    assert [e.event_id for e in events] == [f"cloud:{DIGEST}:m:m1", f"cloud:{DIGEST}:m:m2"]
    assert events[0].tokens_output == 40 and events[0].tool_names == ["Edit"]


@pytest.mark.parametrize(
    ("parent", "kind", "expected"),
    [(None, None, None), ("toolu_x", "Explore", "Explore"), ("toolu_x", None, "subagent")],
)
def test_subagent_messages_are_marked(parent, kind, expected):
    history = turn(1, [msg(message_id="m1", parent=parent, subagent=kind)], {OPUS: {}})
    events, _ = run(history)
    assert events[0].subagent_type == expected


def test_the_cache_write_split_of_a_remainder_follows_the_message_split():
    history = turn(
        1,
        [msg(message_id="m1", write_1h=300, write_5m=100)],
        {OPUS: {"cacheCreationInputTokens": 500}},
    )
    events, _ = run(history)
    remainder = events[-1]
    assert remainder.tokens_cache_create == 100
    assert (remainder.tokens_cache_create_1h, remainder.tokens_cache_create_5m) == (75, 25)


def test_a_remainder_with_no_messages_assumes_one_hour_writes():
    events, _ = run([result(1, {HAIKU: {"cacheCreationInputTokens": 90}})])
    assert (events[0].tokens_cache_create_1h, events[0].tokens_cache_create_5m) == (90, 0)


def test_a_split_that_does_not_add_up_is_adjusted_to_the_total():
    record = assistant(1, "m1", write_1h=50, write_5m=0)
    record["payload"]["message"]["usage"]["cache_creation_input_tokens"] = 30
    events, reducer = run([record, result(2, {OPUS: {"cacheCreationInputTokens": 30}})])
    push = events[0]
    assert push.tokens_cache_create == 30
    assert push.tokens_cache_create_1h + push.tokens_cache_create_5m == 30
    assert reducer.stats["ttl_adjusted"] == 1


def test_a_missing_split_stays_unknown():
    record = assistant(1, "m1", write_1h=20, split=False)
    events, _ = run([record, result(2, {OPUS: {"cacheCreationInputTokens": 20}})])
    assert events[0].tokens_cache_create == 20
    assert (events[0].tokens_cache_create_1h, events[0].tokens_cache_create_5m) == (0, 0)


def test_invalid_numbers_count_as_zero():
    record = assistant(1, "m1", inp=-5, out=True)
    events, _ = run([record, result(2, {OPUS: {}})])
    assert (events[0].tokens_input, events[0].tokens_output) == (0, 0)


@pytest.mark.parametrize(
    ("repos", "cwd", "expected"),
    [
        (("the-repo",), "/home/user/the-repo", ("the-repo", "/home/user/the-repo")),
        (("the-repo",), "/home/user/the-repo/src", ("the-repo", "/home/user/the-repo/src")),
        (("the-repo",), "/home/user", (None, None)),  # a bare home is not a project
        (("the-repo",), "/home/user/other", (None, None)),
        (("the-repo",), None, ("the-repo", None)),
        (("a", "b", "c"), "/home/user", (None, None)),
        (("a", "b", "c"), "/home/user/b", ("b", "/home/user/b")),
        (
            ("https://example.invalid/owner/the-repo.git",),
            "/home/user/the-repo",
            ("the-repo", "/home/user/the-repo"),
        ),
        ((), "/home/user/the-repo", (None, None)),
    ],
)
def test_project_context_follows_the_repository(repos, cwd, expected):
    history = []
    if cwd is not None:
        history.append(system(1, cwd=cwd))
    history += turn(2, [msg(message_id="m1")], {OPUS: {}})
    events, _ = run(history, repos=repos)
    assert (events[0].project_ref, events[0].cwd) == expected
    if expected == (None, None):
        # The ingestor would label a bare home `user`; nothing is passed that could.
        assert derive_project(events[0].cwd) is None


def test_the_branch_comes_from_the_session_or_the_list_row():
    with_branch, _ = run(
        [system(1, branch="from-system"), *turn(2, [msg(message_id="m1")], {OPUS: {}})],
        branch="from-row",
    )
    without, _ = run(turn(1, [msg(message_id="m1")], {OPUS: {}}), branch="from-row")
    assert with_branch[0].git_branch == "from-system" and without[0].git_branch == "from-row"


@pytest.mark.parametrize(("effort", "expected"), [("high", "high"), ("weird", None), (None, None)])
def test_effort_is_only_passed_when_known(effort, expected):
    events, _ = run(turn(1, [msg(message_id="m1")], {OPUS: {}}), effort=effort)
    assert events[0].effort == expected


def test_timestamps_prefer_the_payload_and_fall_back_to_the_envelope():
    stamped = assistant(1, "m1", at=5)
    unstamped = assistant(2, "m2", at=9, stamped=False)
    broken = assistant(3, "m3")
    broken["payload"]["timestamp"] = "not a time"
    broken["created_at"] = "also not"
    events, reducer = run([stamped, unstamped, broken, result(4, {OPUS: {}})])
    assert [e.ts for e in events] == ["2026-09-24T10:00:05+00:00", "2026-09-24T10:00:09+00:00"]
    assert reducer.stats["bad_timestamp"] == 1


def test_a_result_without_a_usable_time_borrows_the_last_messages():
    close = result(2, {OPUS: {"outputTokens": 9}})
    close["payload"]["timestamp"] = "bad"
    close["created_at"] = "bad"
    events, _ = run([assistant(1, "m1", at=7), close])
    assert events[-1].ts == events[0].ts


def test_an_open_turn_emits_only_when_asked_and_never_a_remainder():
    reducer = ClaudeCloudHistoryReducer(facts())
    assert reducer.feed(assistant(1, "m1", out=3)) == []
    tail = reducer.tail_events()
    assert [e.event_id for e in tail] == [f"cloud:{DIGEST}:m:m1"]
    closed = reducer.feed(result(2, {OPUS: {"outputTokens": 9}}))
    assert [e.event_id for e in closed] == [f"cloud:{DIGEST}:m:m1", f"cloud:{DIGEST}:r:1:opus-5.5"]


def test_reading_again_yields_identical_events():
    history = [
        system(1, cwd="/home/user/the-repo"),
        *turn(2, [msg(message_id="m1", out=2, read=50)], {OPUS: {"outputTokens": 9}}),
    ]
    first, _ = run(history)
    second, _ = run(history)
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]


def test_resuming_from_the_saved_state_equals_one_pass():
    cumulative_1 = {
        OPUS: {
            "inputTokens": 10,
            "outputTokens": 60,
            "cacheReadInputTokens": 700,
            "cacheCreationInputTokens": 90,
        }
    }
    cumulative_2 = {
        OPUS: {
            "inputTokens": 25,
            "outputTokens": 100,
            "cacheReadInputTokens": 1500,
            "cacheCreationInputTokens": 190,
        }
    }
    history = [
        system(1, cwd="/home/user/the-repo", claude_code_version="2.1.281"),
        *turn(2, [msg(message_id="m1", inp=5, read=300, write_1h=40)], cumulative_1),
        *turn(10, [msg(message_id="m2", inp=9, read=600, write_5m=80)], cumulative_2),
    ]
    one_pass, _ = run(history)
    boundary = next(i for i, r in enumerate(history) if r["payload"]["type"] == "result") + 1
    head = ClaudeCloudHistoryReducer(facts())
    first = [push for record in history[:boundary] for push in head.feed(record)]
    saved = ReducerState.from_dict(head.state.to_dict())
    tail = ClaudeCloudHistoryReducer(facts(), saved)
    second = [push for record in history[boundary:] for push in tail.feed(record)]
    assert [e.model_dump() for e in first + second] == [e.model_dump() for e in one_pass]


def test_malformed_records_are_ignored():
    reducer = ClaudeCloudHistoryReducer(facts())
    for record in (
        {},
        {"payload": "x"},
        {"payload": {"type": "assistant"}},
        {"payload": {"type": "assistant", "message": {"id": ""}}},
        {"payload": {"type": "result", "modelUsage": "bad"}},
    ):
        reducer.feed(record)
    assert reducer.tail_events() == []


def test_events_carry_no_session_id_only_the_digest():
    events, _ = run(turn(1, [msg(message_id="m1")], {OPUS: {"outputTokens": 9}}))
    assert "cse_fixture_session" not in repr([e.model_dump() for e in events])
    assert all(e.session_id == DIGEST for e in events)


def _database():
    engine = create_engine("sqlite://", connect_args=SQLITE_CONNECT_ARGS, poolclass=StaticPool)
    configure_sqlite_engine(engine)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    seed_pricing_table(session)
    return session


def test_the_events_ingest_exactly_and_replay_adds_nothing():
    cumulative = {
        OPUS: {
            "inputTokens": 30,
            "outputTokens": 800,
            "cacheReadInputTokens": 4000,
            "cacheCreationInputTokens": 500,
        },
        HAIKU: {"inputTokens": 20, "outputTokens": 4, "cacheCreationInputTokens": 200},
    }
    history = [
        system(1, cwd="/home/user/the-repo"),
        *turn(
            2,
            [
                msg(message_id="m1", inp=10, out=6, read=1500, write_1h=300, tools=("Bash",)),
                msg(message_id="m2", inp=12, out=8, read=2000, write_1h=100),
            ],
            cumulative,
            at=60,
        ),
    ]
    events, _ = run(history)
    database = _database()
    ingested = EventIngestor(database).ingest(events, sidecar_id="server")
    assert ingested.events_inserted == len(events) and ingested.events_duplicate == 0
    rows = database.exec(select(UsageEvent)).all()
    sums: dict[str, int] = {}
    for row in rows:
        sums[row.model_id] = sums.get(row.model_id, 0) + row.tokens_output
        assert row.cost_usd is not None and row.cost_usd > 0
        assert row.project == "the-repo" and row.entrypoint == "cloud"
    assert sums == {"opus-5.5": 800, "haiku-4.5": 4}
    again = EventIngestor(database).ingest(events, sidecar_id="server")
    assert again.events_inserted == 0 and again.events_duplicate == len(events)


def test_the_sessions_view_counts_messages_not_remainders():
    cumulative = {
        OPUS: {
            "inputTokens": 30,
            "outputTokens": 800,
            "cacheReadInputTokens": 4000,
            "cacheCreationInputTokens": 500,
        },
        HAIKU: {"inputTokens": 20, "outputTokens": 4, "cacheCreationInputTokens": 200},
    }
    history = turn(
        1, [msg(message_id="m1", inp=10, read=1500), msg(message_id="m2", inp=12)], cumulative
    )
    events, _ = run(history)
    database = _database()
    EventIngestor(database).ingest(events, sidecar_id="server")
    rows = query_sessions(
        database,
        provider_id="anthropic",
        account_id="holder@example.com",
        since=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert len(rows) == 1
    assert rows[0]["msgs"] == 2  # two messages; the two remainders share their turn's id
    assert rows[0]["project"] == "the-repo"
    assert {m["model_id"] for m in rows[0]["by_model"]} == {"opus-5.5", "haiku-4.5"}
