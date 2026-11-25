"""Remote Cowork sessions are missing from every list: the collector fetches the ids a sidecar reported."""

import re
from urllib.parse import unquote

import httpx
import pytest
from sqlmodel import Session

from app.models.schemas_ingest import RemoteSessionHintIn
from app.services import claude_cloud_collector as collector
from app.services import claude_remote_sessions as remote_sessions
from app.services.claude_cloud_events import session_digest
from tests.unit import test_claude_cloud_collector as base
from tests.unit.test_claude_cloud_collector import (
    ORG,
    SUBJECT,
    Server,
    cloud_row,
    cycle,
    events,
    per_model,
    two_turns,
)

# The cloud collector's fixtures, shared by name.
environment = base.environment
no_page_pause = base.no_page_pause

REMOTE = "cse_01Qx7pLm3RtV9wKe2NbYh4Ds"  # pragma: allowlist secret
OTHER = "cse_01Hn4TzW8cJq2XvPa6LmRe9K"  # pragma: allowlist secret
COWORK_TAGS = ["config:cowork-remote", "cowork-remote", "product:cowork-remote"]


def remote_row(sid=REMOTE, *, kind="ccs", tags=COWORK_TAGS, last_event_at=base.QUIET):
    row = cloud_row(sid, last_event_at, environment_kind=kind, tags=list(tags))
    row["config"] = {"effort_level": "high", "outcomes": [], "sources": []}
    return row


class HintServer(Server):
    """The list never returns remote sessions; a single-session read does, wrapped."""

    def __init__(self, rows, histories, sessions):
        super().__init__(rows, histories)
        self.sessions = sessions
        self.statuses: dict[str, int] = {}

    def __call__(self, request):
        match = re.fullmatch(r"/v1/code/sessions/([^/]+)", request.url.path)
        if match:
            self.calls.append((request.url.path, dict(request.url.params)))
            sid = unquote(match.group(1))
            if sid in self.statuses:
                return httpx.Response(self.statuses[sid], json={})
            row = self.sessions.get(sid)
            if row is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"response_shape": row})
        return super().__call__(request)

    def session_reads(self):
        return [c for c in self.calls if re.fullmatch(r"/v1/code/sessions/[^/]+", c[0])]


@pytest.fixture
def hints(environment):
    def add(sid, account=SUBJECT, org=ORG, now=None):
        with Session(environment) as session:
            remote_sessions.upsert_hints(
                session,
                [RemoteSessionHintIn(session_id=sid, account_uuid=account, org_uuid=org)],
                "mac",
                now=now,
            )

    return add


@pytest.mark.asyncio
async def test_a_hinted_remote_cowork_session_is_read_like_a_cloud_session(environment, hints):
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row()})
    report = await cycle(server)
    assert report.outcome == "ok" and report.remote_sessions == 1 and report.sessions_read == 1
    rows = events(environment)
    assert per_model(rows) == {"opus-5.5": [20, 140, 400, 30]}
    assert {r.session_id for r in rows} == {session_digest(REMOTE)}
    assert {r.sidecar_id for r in rows} == {"anthropic-cloud"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        remote_row(kind="bridge"),
        remote_row(kind="anthropic_cloud", tags=[]),
        remote_row(tags=["cowork-remote"]),  # the kind alone is not enough
        remote_row(tags="product:cowork-remote"),
    ],
)
async def test_a_hint_never_makes_another_kind_of_session_readable(environment, hints, row):
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: row})
    report = await cycle(server)
    assert report.outcome == "ok" and report.remote_sessions == 0
    assert "hint_skipped" in report.notes
    assert server.history_calls() == [] and events(environment) == []


@pytest.mark.asyncio
async def test_hints_of_another_account_or_organization_are_not_fetched(environment, hints):
    hints(REMOTE, account="33333333-3333-4333-8333-333333333333")
    hints(OTHER, org="44444444-4444-4444-8444-444444444444")
    server = HintServer([], {}, {REMOTE: remote_row(), OTHER: remote_row(OTHER)})
    report = await cycle(server)
    assert report.outcome == "ok" and server.session_reads() == []


@pytest.mark.asyncio
async def test_a_listed_session_is_not_fetched_again_through_its_hint(environment, hints):
    hints(REMOTE)
    listed = cloud_row(REMOTE)
    server = HintServer([listed], {REMOTE: two_turns()}, {REMOTE: remote_row()})
    report = await cycle(server)
    assert report.sessions_read == 1 and server.session_reads() == []


@pytest.mark.asyncio
async def test_a_missing_session_skips_only_that_hint(environment, hints):
    hints(OTHER)
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row()})
    report = await cycle(server)
    assert report.outcome == "ok" and "hint_unavailable" in report.notes
    assert report.sessions_read == 1 and len(events(environment)) > 0


@pytest.mark.asyncio
async def test_a_read_that_returns_another_session_is_ignored(environment, hints):
    hints(REMOTE)
    server = HintServer([], {OTHER: two_turns()}, {REMOTE: remote_row(OTHER)})
    report = await cycle(server)
    assert "hint_unsupported_contract" in report.notes and events(environment) == []


@pytest.mark.asyncio
async def test_an_unchanged_remote_session_is_not_read_again(environment, hints):
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row()})
    await cycle(server)
    reads = len(server.history_calls())
    again = await cycle(server)
    assert again.sessions_read == 0 and again.events == 0
    assert len(server.history_calls()) == reads


@pytest.mark.asyncio
async def test_remote_ids_never_reach_the_logs(environment, hints, caplog):
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row()})
    with caplog.at_level("DEBUG"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            await collector.collect_all(client)
    assert REMOTE not in caplog.text and "remote_cowork=1" in caplog.text


@pytest.mark.asyncio
async def test_unreadable_hints_do_not_fail_the_cycle(environment, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def broken(*args, **kwargs):
        raise OperationalError("select", {}, Exception("no table"))

    monkeypatch.setattr(remote_sessions, "load_hints", broken)
    report = await cycle(HintServer([cloud_row()], {base.SECRET_ID: two_turns()}, {}))
    assert report.outcome == "ok" and report.sessions_read == 1
    assert report.notes == ["hints_unavailable"]


def fetched(server):
    return [unquote(path.rsplit("/", 1)[1]) for path, _ in server.session_reads()]


@pytest.mark.asyncio
async def test_every_hint_is_reached_however_many_a_mac_reports(environment, hints, monkeypatch):
    monkeypatch.setattr(collector, "MAX_HINTS_PER_CYCLE", 1)
    hints(REMOTE, now=1000.0)
    hints(OTHER, now=1000.0)  # same stamp: the order must still rotate
    server = HintServer(
        [],
        {REMOTE: two_turns(), OTHER: two_turns()},
        {REMOTE: remote_row(), OTHER: remote_row(OTHER)},
    )
    await cycle(server)
    await cycle(server)
    assert sorted(fetched(server)) == sorted([REMOTE, OTHER])


@pytest.mark.asyncio
async def test_a_settled_session_is_only_rechecked_once_a_day(environment, hints, monkeypatch):
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row()})  # last event long ago
    await cycle(server)
    await cycle(server)
    assert fetched(server) == [REMOTE]
    monkeypatch.setattr(remote_sessions, "SETTLED_RECHECK", 0.0)  # a day has passed
    await cycle(server)
    assert fetched(server) == [REMOTE, REMOTE]


@pytest.mark.asyncio
async def test_an_active_session_is_fetched_every_cycle(environment, hints):
    hints(REMOTE)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row(last_event_at=base.LIVE)})
    await cycle(server)
    await cycle(server)
    assert fetched(server) == [REMOTE, REMOTE]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,note", [(429, "hint_rate_limited"), (404, "hint_unavailable")])
async def test_a_failing_hint_read_never_fails_the_cycle(environment, hints, status, note):
    hints(OTHER)
    server = HintServer([cloud_row()], {base.SECRET_ID: two_turns()}, {})
    server.statuses[OTHER] = status
    report = await cycle(server)
    assert report.outcome == "ok" and note in report.notes
    assert report.sessions_read == 1  # the listed session is still read


@pytest.mark.asyncio
async def test_a_rate_limit_ends_the_hint_pass(environment, hints):
    hints(OTHER, now=1.0)
    hints(REMOTE, now=2.0)
    server = HintServer([], {REMOTE: two_turns()}, {REMOTE: remote_row()})
    server.statuses[OTHER] = 429
    report = await cycle(server)
    assert report.outcome == "ok" and fetched(server) == [OTHER]


def test_a_later_report_never_moves_a_hint_to_another_account(environment, hints):
    from sqlmodel import select

    from app.models.db import ClaudeRemoteSessionHint

    hints(REMOTE, now=1.0)
    hints(REMOTE, account="33333333-3333-4333-8333-333333333333", now=10_000.0)
    with Session(environment) as session:
        row = session.exec(select(ClaudeRemoteSessionHint)).one()
    assert (row.account_uuid, row.last_seen) == (SUBJECT, 10_000.0)
