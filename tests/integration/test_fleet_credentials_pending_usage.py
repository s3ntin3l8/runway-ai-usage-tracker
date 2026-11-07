"""Integration tests for pending usage event sessions, batch assignment, and deduplication."""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models.db import ProviderConfig
from tests.fixtures.test_client import TestClient
from tests.integration._fleet_credentials_helpers import (
    _add_provider_config,
    _isolated_ingest_key,  # noqa: F401 — fixture
    client_fixture,  # noqa: F401 — fixture
    session_fixture,  # noqa: F401 — fixture
)


def test_pending_usage_event_can_be_assigned_and_promoted(client: TestClient, session: Session):
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(
        session,
        provider_id="anthropic",
        account_id="alice@example.com",
    )
    push = UsageEventPush(
        provider_id="anthropic",
        account_id="default",
        account_source="default",
        event_id="pending-manual-1",
        ts="2026-09-01T10:00:00Z",
        model_id="sonnet",
        tokens_input=1000,
    )
    session.add(
        PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="alpha",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).first()
    listed = client.get("/api/v1/fleet/events/pending")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert [item["id"] for item in listed.json()["items"]] == [pending.id]

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [pending.id], "account_id": "alice@example.com"},
    )
    assert response.status_code == 200, response.text
    assert session.exec(select(PendingUsageEvent)).all() == []
    stored = session.exec(select(UsageEvent)).first()
    assert stored.account_id == "alice@example.com"
    assert stored.attribution_source == "tag"
    assert stored.tokens_input == 1000


def test_pending_usage_sessions_group_events_and_keep_missing_sessions_separate(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(
        session,
        provider_id="antigravity",
        account_id="alice@example.com",
    )
    pushes = [
        UsageEventPush(
            provider_id="antigravity",
            account_id="default",
            account_source="default",
            event_id=event_id,
            ts=timestamp,
            model_id="flash-3.8",
            session_id=session_id,
        )
        for event_id, timestamp, session_id in (
            ("session-a-1", "2026-09-01T10:00:00Z", "session-a"),
            ("session-a-2", "2026-09-01T10:01:00Z", "session-a"),
            ("no-session", "2026-09-01T10:02:00Z", None),
        )
    ]
    for push in pushes:
        session.add(
            PendingUsageEvent(
                provider_id=push.provider_id,
                event_id=push.event_id,
                sidecar_id="laptop",
                ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
                payload_json=push.model_dump_json(),
            )
        )
    session.commit()

    response = client.get("/api/v1/fleet/events/pending/sessions")

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_events"] == 3
    assert data["matching_events"] == 3
    assert data["total_groups"] == 2
    assert data["sidecars"] == ["laptop"]
    assert data["providers"] == ["antigravity"]
    grouped = next(item for item in data["items"] if item["session_id"] == "session-a")
    ungrouped = next(item for item in data["items"] if item["session_id"] is None)
    assert grouped["event_count"] == 2
    assert len(grouped["event_ids"]) == 2
    assert grouped["model_ids"] == ["flash-3.8"]
    assert ungrouped["event_count"] == 1
    assert len(ungrouped["event_ids"]) == 1

    assigned = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": grouped["event_ids"], "account_id": "alice@example.com"},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned"] == 2
    promoted = session.exec(select(UsageEvent)).all()
    assert len(promoted) == 2
    assert {event.account_id for event in promoted} == {"alice@example.com"}
    remaining = session.exec(select(PendingUsageEvent)).all()
    assert len(remaining) == 1
    assert remaining[0].event_id == "no-session"


def test_pending_usage_sessions_filter_groups_before_pagination(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush

    for provider_id, event_id, sidecar_id, model_id in (
        ("xai", "filter-xai-a", "laptop-a", "grok-4"),
        ("xai", "filter-xai-b", "laptop-b", "grok-4"),
        ("minimax", "filter-minimax", "laptop-a", "abab-7"),
    ):
        push = UsageEventPush(
            provider_id=provider_id,
            account_id="default",
            account_source="default",
            event_id=event_id,
            ts="2026-09-01T10:00:00Z",
            model_id=model_id,
            session_id=f"session-{event_id}",
        )
        session.add(
            PendingUsageEvent(
                provider_id=provider_id,
                event_id=event_id,
                sidecar_id=sidecar_id,
                ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
                payload_json=push.model_dump_json(),
            )
        )
    session.commit()

    response = client.get(
        "/api/v1/fleet/events/pending/sessions",
        params={"sidecar_id": "laptop-a", "provider_id": "xai", "search": "GROK"},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_events"] == 3
    assert data["matching_events"] == 1
    assert data["total_groups"] == 1
    assert data["sidecars"] == ["laptop-a", "laptop-b"]
    assert data["providers"] == ["minimax", "xai"]
    assert data["items"][0]["event_ids"]


def test_pending_usage_sessions_groups_a_mixed_backlog_the_same_way_in_sql(
    client: TestClient, session: Session
):
    """The grouping runs in SQL now; the result must not change.

    A row only shares a group with another row when both carry the same
    usable session id. A missing, empty or non-string session id — and a
    payload that is not JSON at all — keeps the row on its own.
    """
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush

    rows = [
        # Two rows share a session, so they share a group.
        ("xai", "shared-1", "host-a", "session-a", "grok-4", "2026-09-01T10:00:00Z"),
        ("xai", "shared-2", "host-a", "session-a", "grok-4", "2026-09-01T10:01:00Z"),
        # A second model in the same session joins the same group.
        ("xai", "shared-3", "host-a", "session-a", "grok-3", "2026-09-01T10:02:00Z"),
        # The same session id on another host is another group.
        ("xai", "other-host", "host-b", "session-a", "grok-4", "2026-09-01T10:03:00Z"),
        # Another provider with the same session id is another group.
        ("minimax", "other-provider", "host-a", "session-a", "abab-7", "2026-09-01T10:04:00Z"),
        # No session id at all: the row stands alone.
        ("xai", "no-session", "host-a", None, "grok-4", "2026-09-01T10:05:00Z"),
    ]
    for provider_id, event_id, sidecar_id, session_id, model_id, ts in rows:
        push = UsageEventPush(
            provider_id=provider_id,
            account_id="default",
            account_source="default",
            event_id=event_id,
            ts=ts,
            model_id=model_id,
            session_id=session_id,
        )
        session.add(
            PendingUsageEvent(
                provider_id=provider_id,
                event_id=push.event_id,
                sidecar_id=sidecar_id,
                ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
                payload_json=push.model_dump_json(),
            )
        )
    # A payload that is not JSON, and one whose session id is not a string.
    session.add(
        PendingUsageEvent(
            provider_id="xai",
            event_id="broken-payload",
            sidecar_id="host-a",
            ts=datetime.fromisoformat("2026-09-01T10:06:00+00:00"),
            payload_json="not json at all",
        )
    )
    session.add(
        PendingUsageEvent(
            provider_id="xai",
            event_id="numeric-session",
            sidecar_id="host-a",
            ts=datetime.fromisoformat("2026-09-01T10:07:00+00:00"),
            payload_json='{"session_id": 4711, "model_id": "grok-4"}',
        )
    )
    session.commit()

    response = client.get("/api/v1/fleet/events/pending/sessions")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_events"] == 8
    assert data["matching_events"] == 8
    assert data["total_groups"] == 6
    assert data["sidecars"] == ["host-a", "host-b"]
    assert data["providers"] == ["minimax", "xai"]

    by_session = {item["session_id"]: item for item in data["items"]}
    shared = by_session["session-a"]
    assert shared["provider_id"] == "xai"
    assert shared["sidecar_id"] == "host-a"
    assert shared["event_count"] == 3
    assert len(shared["event_ids"]) == 3
    assert shared["model_ids"] == ["grok-3", "grok-4"]
    assert shared["first_ts"] == "2026-09-01T10:00:00+00:00"
    assert shared["last_ts"] == "2026-09-01T10:02:00+00:00"

    # The rows without a usable session id stand alone.
    standalone = [item for item in data["items"] if item["session_id"] is None]
    assert len(standalone) == 3
    assert all(item["event_count"] == 1 for item in standalone)

    # The groups come back newest first.
    assert [item["last_ts"] for item in data["items"]] == sorted(
        (item["last_ts"] for item in data["items"]), reverse=True
    )


def test_pending_usage_sessions_search_matches_sessions_and_models(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush

    for event_id, session_id, model_id in (
        ("search-a", "alpha-session", "grok-4"),
        ("search-b", "beta-session", "claude-3-5"),
        ("search-c", "gamma-session", "grok-4"),
    ):
        push = UsageEventPush(
            provider_id="xai",
            account_id="default",
            account_source="default",
            event_id=event_id,
            ts="2026-09-01T10:00:00Z",
            model_id=model_id,
            session_id=session_id,
        )
        session.add(
            PendingUsageEvent(
                provider_id="xai",
                event_id=push.event_id,
                sidecar_id="host-a",
                ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
                payload_json=push.model_dump_json(),
            )
        )
    session.commit()

    def groups(**params):
        response = client.get("/api/v1/fleet/events/pending/sessions", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    # A model id matches, and the whole group comes back with it.
    by_model = groups(search="grok")
    assert by_model["total_groups"] == 2
    assert by_model["matching_events"] == 2

    # A session id matches.
    by_session = groups(search="beta")
    assert by_session["total_groups"] == 1
    assert by_session["items"][0]["session_id"] == "beta-session"

    # The search is a plain substring search: a wildcard is taken literally.
    literal = groups(search="%")
    assert literal["total_groups"] == 0
    underscore = groups(search="_")
    assert underscore["total_groups"] == 0

    # The filters narrow the groups but not the totals.
    narrowed = groups(provider_id="xai", search="grok")
    assert narrowed["total_groups"] == 2
    other = groups(provider_id="minimax", search="grok")
    assert other["total_groups"] == 0
    assert other["total_events"] == 3


def test_pending_usage_sessions_paginates_the_groups(client: TestClient, session: Session):
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush

    for index in range(5):
        push = UsageEventPush(
            provider_id="xai",
            account_id="default",
            account_source="default",
            event_id=f"page-{index}",
            ts=f"2026-09-01T10:0{index}:00Z",
            model_id="grok-4",
            session_id=f"page-session-{index}",
        )
        session.add(
            PendingUsageEvent(
                provider_id="xai",
                event_id=push.event_id,
                sidecar_id="host-a",
                ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
                payload_json=push.model_dump_json(),
            )
        )
    session.commit()

    response = client.get("/api/v1/fleet/events/pending/sessions", params={"offset": 1, "limit": 2})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total_groups"] == 5
    assert data["matching_events"] == 5
    assert [item["session_id"] for item in data["items"]] == [
        "page-session-3",
        "page-session-2",
    ]
    assert data["offset"] == 1
    assert data["limit"] == 2

    # A page past the end still reports the totals.
    empty = client.get("/api/v1/fleet/events/pending/sessions", params={"offset": 99, "limit": 2})
    assert empty.status_code == 200, empty.text
    assert empty.json()["items"] == []
    assert empty.json()["total_groups"] == 5
    assert empty.json()["total_events"] == 5


def test_pending_usage_assignment_guards_the_request_size_and_keeps_the_events(
    client: TestClient, session: Session
):
    """An oversized request is refused without touching the backlog.

    The guard is what stops one request from carrying an unbounded number
    of events. It must refuse *before* anything is promoted: the events stay
    pending and can be assigned in smaller batches.
    """
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="xai", account_id="alice@example.com")
    push = UsageEventPush(
        provider_id="xai",
        account_id="default",
        account_source="default",
        event_id="guard-event",
        ts="2026-09-01T10:00:00Z",
        model_id="grok-4",
        session_id="guard-session",
    )
    session.add(
        PendingUsageEvent(
            provider_id="xai",
            event_id=push.event_id,
            sidecar_id="host-a",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).first()
    assert pending is not None

    oversized_batch = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={
            "assignments": [
                {
                    "event_ids": list(range(10_001)),
                    "account_id": "alice@example.com",
                }
            ]
        },
    )
    assert oversized_batch.status_code == 422
    assert oversized_batch.json()["detail"] == "Select between 1 and 10000 events."

    oversized_single = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": list(range(1001)), "account_id": "alice@example.com"},
    )
    assert oversized_single.status_code == 422
    assert oversized_single.json()["detail"] == "Select between 1 and 1000 events."

    # Nothing was promoted: the event is still pending and no usage event
    # was written.
    still_pending = session.exec(select(PendingUsageEvent)).all()
    assert [row.event_id for row in still_pending] == ["guard-event"]
    assert session.exec(select(UsageEvent)).all() == []


def test_pending_usage_batch_assigns_a_split_session_group_in_two_calls(
    client: TestClient, session: Session
):
    """A session group larger than the batch limit is assigned in two calls.

    The Fleet UI splits a selection into requests of at most 10,000 event
    ids. The second call carries the remainder of the same session group, so
    it has to succeed like any other assignment — and every event of the
    group has to end up attributed, with nothing left pending.

    The group is small on purpose: promoting ten thousand rows takes minutes,
    and the 10,000 boundary itself is pinned by the guard test above and by
    the frontend's chunking test.
    """
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="xai", account_id="alice@example.com")
    rows: list[PendingUsageEvent] = []
    for index in range(250):
        push = UsageEventPush(
            provider_id="xai",
            account_id="default",
            account_source="default",
            event_id=f"split-{index}",
            ts="2026-09-01T10:00:00Z",
            model_id="grok-4",
            session_id="split-session",
        )
        rows.append(
            PendingUsageEvent(
                provider_id="xai",
                event_id=push.event_id,
                sidecar_id="host-a",
                ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
                payload_json=push.model_dump_json(),
            )
        )
    session.add_all(rows)
    session.flush()
    ids = [row.id for row in rows if row.id is not None]
    session.commit()
    assert len(ids) == 250

    first = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={"assignments": [{"event_ids": ids[:150], "account_id": "alice@example.com"}]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["assigned"] == 150

    second = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={"assignments": [{"event_ids": ids[150:], "account_id": "alice@example.com"}]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["assigned"] == 100

    # Everything is attributed: nothing is left pending.
    assert session.exec(select(PendingUsageEvent)).all() == []
    assert len(session.exec(select(UsageEvent)).all()) == 250


def test_pending_usage_batch_assigns_provider_specific_accounts_and_host_tags(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import CredentialTag, PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush
    from app.services.credential_tags import CredentialTagRepo

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    _add_provider_config(session, provider_id="xai", account_id="bob@example.com")
    event_ids: dict[str, int] = {}
    for provider_id, account_id, sidecar_id in (
        ("anthropic", "alice@example.com", "host-a"),
        ("xai", "bob@example.com", "host-b"),
    ):
        push = UsageEventPush(
            provider_id=provider_id,
            account_id="default",
            account_source="default",
            event_id=f"batch-{provider_id}",
            ts="2026-09-01T10:00:00Z",
            model_id=f"{provider_id}-model",
        )
        row = PendingUsageEvent(
            provider_id=provider_id,
            event_id=push.event_id,
            sidecar_id=sidecar_id,
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
        session.add(row)
        session.flush()
        event_ids[provider_id] = row.id
    session.commit()

    response = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={
            "assignments": [
                {"event_ids": [event_ids["anthropic"]], "account_id": "alice@example.com"},
                {"event_ids": [event_ids["xai"]], "account_id": "bob@example.com"},
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "assigned": 2,
        "providers": ["anthropic", "xai"],
        "mappings": [
            {
                "provider_id": "anthropic",
                "sidecar_id": "host-a",
                "target_provider_id": "anthropic",
                "account_id": "alice@example.com",
            },
            {
                "provider_id": "xai",
                "sidecar_id": "host-b",
                "target_provider_id": "xai",
                "account_id": "bob@example.com",
            },
        ],
    }
    assert session.exec(select(PendingUsageEvent)).all() == []
    assert {
        event.provider_id: event.account_id for event in session.exec(select(UsageEvent)).all()
    } == {"anthropic": "alice@example.com", "xai": "bob@example.com"}
    tags = session.exec(select(CredentialTag)).all()
    assert {(tag.provider_id, tag.account_id, tag.sidecar_id) for tag in tags} == {
        ("anthropic", "alice@example.com", "host-a"),
        ("xai", "bob@example.com", "host-b"),
    }
    assert CredentialTagRepo.list_pending_payload(
        session, providers=["anthropic"], sidecar_id="host-a"
    ) == {"anthropic": {"provider:anthropic": "alice@example.com"}}
    assert (
        CredentialTagRepo.list_pending_payload(
            session, providers=["anthropic"], sidecar_id="host-b"
        )
        == {}
    )


def test_pending_usage_batch_validates_every_account_before_promoting(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    _add_provider_config(
        session, provider_id="xai", account_id="disabled@example.com", enabled=False
    )
    pending_ids = []
    for provider_id in ("anthropic", "xai"):
        push = UsageEventPush(
            provider_id=provider_id,
            account_id="default",
            account_source="default",
            event_id=f"batch-invalid-{provider_id}",
            ts="2026-09-01T10:00:00Z",
            model_id=f"{provider_id}-model",
        )
        row = PendingUsageEvent(
            provider_id=provider_id,
            event_id=push.event_id,
            sidecar_id="laptop",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
        session.add(row)
        session.flush()
        pending_ids.append(row.id)
    session.commit()

    response = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={
            "assignments": [
                {"event_ids": [pending_ids[0]], "account_id": "alice@example.com"},
                {"event_ids": [pending_ids[1]], "account_id": "disabled@example.com"},
            ]
        },
    )

    assert response.status_code == 404
    assert len(session.exec(select(PendingUsageEvent)).all()) == 2
    assert session.exec(select(UsageEvent)).all() == []


def test_pending_usage_batch_rejects_conflicting_future_mapping_targets(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    _add_provider_config(session, provider_id="anthropic", account_id="bob@example.com")
    pending_ids = []
    for event_id in ("conflict-one", "conflict-two"):
        push = UsageEventPush(
            provider_id="anthropic",
            account_id="default",
            account_source="default",
            event_id=event_id,
            ts="2026-09-01T10:00:00Z",
            model_id="sonnet",
        )
        row = PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="same-host",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
        session.add(row)
        session.flush()
        pending_ids.append(row.id)
    session.commit()

    response = client.post(
        "/api/v1/fleet/events/pending/assign-batch",
        json={
            "assignments": [
                {"event_ids": [pending_ids[0]], "account_id": "alice@example.com"},
                {"event_ids": [pending_ids[1]], "account_id": "bob@example.com"},
            ]
        },
    )

    assert response.status_code == 422
    assert len(session.exec(select(PendingUsageEvent)).all()) == 2
    assert session.exec(select(UsageEvent)).all() == []


def test_pending_assignment_preserves_deduplication_across_sidecars(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="anthropic", account_id="alice@example.com")
    pending_ids = []
    for sidecar_id in ("host-a", "host-b"):
        push = UsageEventPush(
            provider_id="anthropic",
            account_id="default",
            account_source="default",
            event_id="shared-pending-event",
            ts="2026-09-01T10:00:00Z",
            model_id="sonnet",
        )
        row = PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id=sidecar_id,
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
        session.add(row)
        session.flush()
        pending_ids.append(row.id)
    session.commit()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": pending_ids, "account_id": "alice@example.com"},
    )

    assert response.status_code == 200, response.text
    stored = session.exec(select(UsageEvent)).all()
    assert len(stored) == 1
    assert stored[0].account_id == "alice@example.com"
    assert session.exec(select(PendingUsageEvent)).all() == []


@pytest.mark.parametrize("tier_provider_id", ["opencode-free", "opencode-zen"])
def test_opencode_tier_events_assign_to_shared_account_without_merging_provider(
    client: TestClient, session: Session, tier_provider_id: str
):
    from datetime import datetime

    from app.models.db import CredentialTag, PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(session, provider_id="opencode", account_id="alice@example.com")
    push = UsageEventPush(
        provider_id=tier_provider_id,
        account_id="default",
        account_source="default",
        event_id=f"{tier_provider_id}-pending",
        ts="2026-09-01T10:00:00Z",
        model_id="tier-model",
    )
    session.add(
        PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="laptop",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).one()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [pending.id], "account_id": "alice@example.com"},
    )

    assert response.status_code == 200, response.text
    event = session.exec(select(UsageEvent)).one()
    assert (event.provider_id, event.account_id) == (tier_provider_id, "alice@example.com")
    tag = session.exec(select(CredentialTag)).one()
    assert (tag.provider_id, tag.account_id, tag.sidecar_id) == (
        tier_provider_id,
        "alice@example.com",
        "laptop",
    )


def test_pending_event_assignment_accepts_a_known_discovered_account(
    client: TestClient, session: Session, monkeypatch
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent, UsageEvent
    from app.models.schemas import UsageEventPush

    async def active_accounts():
        return [("antigravity", "alice@example.com", "Alice")]

    monkeypatch.setattr(
        "app.api.endpoints.fleet_pending_events.token_cache.get_all_active_accounts",
        active_accounts,
    )
    push = UsageEventPush(
        provider_id="antigravity",
        account_id="default",
        account_source="default",
        event_id="discovered-account-pending",
        ts="2026-09-01T10:00:00Z",
        model_id="flash-3.8",
    )
    session.add(
        PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="laptop",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).first()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [pending.id], "account_id": "alice@example.com"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["assigned"] == 1
    assert session.exec(select(PendingUsageEvent)).all() == []
    stored = session.exec(select(UsageEvent)).first()
    assert stored.account_id == "alice@example.com"


def test_pending_event_assignment_rejects_disabled_account(client: TestClient, session: Session):
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush

    _add_provider_config(
        session,
        provider_id="antigravity",
        account_id="alice@example.com",
        enabled=False,
    )
    push = UsageEventPush(
        provider_id="antigravity",
        account_id="default",
        account_source="default",
        event_id="disabled-account-pending",
        ts="2026-09-01T10:00:00Z",
        model_id="flash-3.8",
    )
    session.add(
        PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="laptop",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).first()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [pending.id], "account_id": "alice@example.com"},
    )

    assert response.status_code == 404
    assert session.exec(select(PendingUsageEvent)).first() is not None


def test_pending_event_assignment_does_not_resurrect_hidden_historical_identity(
    client: TestClient, session: Session, monkeypatch
):
    from datetime import datetime

    from app.models.db import LatestUsage, PendingUsageEvent
    from app.models.schemas import UsageEventPush

    async def no_active_accounts():
        return []

    monkeypatch.setattr(
        "app.api.endpoints.fleet_pending_events.token_cache.get_all_active_accounts",
        no_active_accounts,
    )
    session.add(
        ProviderConfig(provider_id="antigravity", account_id="alice@example.com", enabled=True)
    )
    session.add(
        LatestUsage(
            provider_id="antigravity",
            account_id="historical@example.com",
            sidecar_id="laptop",
            window_type="weekly",
            variant="default",
            model_id="flash-3.8",
            card_json="{}",
        )
    )
    push = UsageEventPush(
        provider_id="antigravity",
        account_id="default",
        account_source="default",
        event_id="historical-account-pending",
        ts="2026-09-01T10:00:00Z",
        model_id="flash-3.8",
    )
    session.add(
        PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="laptop",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).first()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [pending.id], "account_id": "historical@example.com"},
    )

    assert response.status_code == 404
    assert session.exec(select(PendingUsageEvent)).first() is not None


def test_assigning_opencode_pending_event_persists_provider_mapping(
    client: TestClient, session: Session
):
    from datetime import datetime

    from app.models.db import PendingUsageEvent
    from app.models.schemas import UsageEventPush
    from app.services.credential_tags import CredentialTagRepo

    _add_provider_config(session, provider_id="xai", account_id="alice@example.com")
    push = UsageEventPush(
        provider_id="xai",
        account_id="default",
        account_source="default",
        event_id="opencode-xai-pending",
        ts="2026-09-01T10:00:00Z",
        model_id="grok-4",
    )
    session.add(
        PendingUsageEvent(
            provider_id=push.provider_id,
            event_id=push.event_id,
            sidecar_id="alpha",
            ts=datetime.fromisoformat(push.ts.replace("Z", "+00:00")),
            payload_json=push.model_dump_json(),
        )
    )
    session.commit()
    pending = session.exec(select(PendingUsageEvent)).first()

    response = client.post(
        "/api/v1/fleet/events/pending/assign",
        json={"event_ids": [pending.id], "account_id": "alice@example.com"},
    )

    assert response.status_code == 200, response.text
    tag = CredentialTagRepo.get(
        session,
        provider_id="xai",
        credential_origin="provider:xai",
        sidecar_id="alpha",
    )
    assert tag is not None
    assert tag.account_id == "alice@example.com"
