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


@pytest.mark.asyncio
async def test_a_hint_that_never_resolves_is_only_rechecked_once_a_day(
    environment, hints, monkeypatch
):
    hints(OTHER)
    server = HintServer([], {}, {})
    server.statuses[OTHER] = 404
    await cycle(server)
    await cycle(server)
    assert fetched(server) == [OTHER]
    monkeypatch.setattr(remote_sessions, "SETTLED_RECHECK", 0.0)  # a day has passed
    await cycle(server)
    assert fetched(server) == [OTHER, OTHER]


def test_a_heartbeat_resending_the_whole_list_writes_only_new_hints(environment, hints):
    from sqlmodel import select

    from app.models.db import ClaudeRemoteSessionHint

    hints(REMOTE, now=1.0)
    with Session(environment) as session:
        written = remote_sessions.upsert_hints(
            session,
            [
                RemoteSessionHintIn(session_id=sid, account_uuid=SUBJECT, org_uuid=ORG)
                for sid in (REMOTE, OTHER, OTHER)
            ],
            "mac",
            now=2.0,
        )
        assert written == 1
        assert len(session.exec(select(ClaudeRemoteSessionHint)).all()) == 2


@pytest.mark.asyncio
async def test_hints_are_fetched_even_when_the_list_fills_the_request_budget(
    environment, hints, monkeypatch
):
    # Prod's list alone took 142 pages; the hint pass must not depend on what the list spent.
    monkeypatch.setattr(collector, "REQUESTS_PER_CYCLE", 2)
    hints(REMOTE)
    server = HintServer(
        [cloud_row()], {base.SECRET_ID: two_turns(), REMOTE: two_turns()}, {REMOTE: remote_row()}
    )
    first = await cycle(server)
    assert fetched(server) == [REMOTE]
    assert first.backlog and first.outcome == "partial"
    monkeypatch.setattr(collector, "REQUESTS_PER_CYCLE", 200)
    await cycle(server)  # the catch-up cycle reads the waiting histories
    assert {r.session_id for r in events(environment)} == {
        session_digest(base.SECRET_ID),
        session_digest(REMOTE),
    }


# -- discovery from the account's claude.ai conversations (#1397) ------------------------------

PHONE = "cse_01RnkWmQ71CuyetqrPAhjUYB"  # pragma: allowlist secret


class ClaudeAiServer(HintServer):
    """Adds claude.ai: the cookie's account and the conversation list."""

    def __init__(self, *args, conversations=(), email=base.ACCOUNT, status=200):
        super().__init__(*args)
        self.conversations, self.email, self.status = list(conversations), email, status
        self.web_calls: list[str] = []
        self.list_status = 200
        self.hang = False

    def __call__(self, request):
        if request.url.host != "claude.ai":
            return super().__call__(request)
        self.web_calls.append(request.url.path)
        assert "sessionKey=fixture-cookie" in request.headers["cookie"]
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if self.hang:
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/api/account":
            return httpx.Response(200, json={"email_address": self.email})
        if self.list_status != 200:
            return httpx.Response(self.list_status, json={})
        assert request.url.path == f"/api/organizations/{ORG}/chat_conversations_v2"
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        return httpx.Response(200, json=self.conversations[offset : offset + limit])


@pytest.fixture
async def cookie(environment):
    await base.probe.token_cache.store(
        "anthropic",
        {"cookie_sessionKey": "fixture-cookie"},
        account_id=base.ACCOUNT,
        source_id="fixture-cookie-source",
        source_metadata={"credential_origin": "cookie:anthropic/Chrome/Default/Cookies"},
    )


@pytest.fixture(autouse=True)
def fresh_discovery():
    remote_sessions._LAST_DISCOVERY.clear()
    yield
    remote_sessions._LAST_DISCOVERY.clear()


def conversation(workspace=None, **extra):
    row = {"uuid": "conv", "name": "a private title", **extra}
    if workspace is not None:
        row["workspace_session_id"] = workspace
    return row


@pytest.mark.asyncio
async def test_sessions_linked_to_conversations_are_discovered_and_read(environment, cookie):
    phone = remote_row(PHONE, kind="anthropic_cloud")  # started on the phone: cloud runner
    server = ClaudeAiServer(
        [cloud_row()],
        {base.SECRET_ID: two_turns(), PHONE: two_turns(), REMOTE: two_turns()},
        {PHONE: phone, REMOTE: remote_row()},
        conversations=[
            conversation(PHONE.replace("cse_", "session_")),
            conversation(REMOTE),
            conversation(),  # an ordinary chat
            conversation("not-a-session-id"),
        ],
    )
    report = await cycle(server)
    assert report.outcome == "ok" and report.discovered == 2 and report.remote_sessions == 2
    assert {r.session_id for r in events(environment)} == {
        session_digest(base.SECRET_ID),
        session_digest(PHONE),
        session_digest(REMOTE),
    }


@pytest.mark.asyncio
async def test_an_ordinary_cloud_session_is_not_read_through_a_conversation(environment, cookie):
    # A cloud-runner session without the Cowork tag is the list's business, never a hint's.
    server = ClaudeAiServer(
        [],
        {PHONE: two_turns()},
        {PHONE: remote_row(PHONE, kind="anthropic_cloud", tags=[])},
        conversations=[conversation(PHONE)],
    )
    report = await cycle(server)
    assert "hint_skipped" in report.notes and events(environment) == []


@pytest.mark.asyncio
async def test_a_cookie_of_another_account_discovers_nothing(environment, cookie):
    server = ClaudeAiServer(
        [], {}, {}, conversations=[conversation(PHONE)], email="someone-else@example.com"
    )
    report = await cycle(server)
    assert "discovery_cookie_other_account" in report.notes and report.discovered == 0
    assert server.web_calls == ["/api/account"] and server.session_reads() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,note", [(403, "permission_denied"), (429, "rate_limited")])
async def test_a_failing_conversation_list_never_fails_the_cycle(environment, cookie, status, note):
    server = ClaudeAiServer([cloud_row()], {base.SECRET_ID: two_turns()}, {}, status=status)
    report = await cycle(server)
    assert report.outcome == "ok" and report.sessions_read == 1
    assert f"discovery_{note}" in report.notes


@pytest.mark.asyncio
async def test_conversations_are_listed_at_most_once_an_hour(environment, cookie, monkeypatch):
    server = ClaudeAiServer([], {}, {}, conversations=[conversation()])
    await cycle(server)
    await cycle(server)
    assert server.web_calls.count("/api/account") == 1
    monkeypatch.setattr(remote_sessions, "DISCOVERY_INTERVAL", 0.0)
    await cycle(server)
    assert server.web_calls.count("/api/account") == 2


@pytest.mark.asyncio
async def test_the_conversation_list_is_paged_and_bounded(environment, cookie, monkeypatch):
    monkeypatch.setattr(remote_sessions, "DISCOVERY_PAGE", 1)
    monkeypatch.setattr(remote_sessions, "DISCOVERY_MAX_PAGES", 2)
    server = ClaudeAiServer(
        [], {}, {}, conversations=[conversation(PHONE), conversation(REMOTE), conversation(OTHER)]
    )
    report = await cycle(server)
    assert report.discovered == 2 and "discovery_max_pages" in report.notes


@pytest.mark.asyncio
async def test_without_a_cookie_nothing_is_asked_of_claude_ai(environment):
    server = ClaudeAiServer([], {}, {})
    report = await cycle(server)
    assert report.outcome == "ok" and server.web_calls == []


@pytest.mark.asyncio
async def test_conversation_titles_never_reach_the_logs(environment, cookie, caplog):
    server = ClaudeAiServer(
        [], {PHONE: two_turns()}, {PHONE: remote_row(PHONE)}, conversations=[conversation(PHONE)]
    )
    with caplog.at_level("DEBUG"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            await collector.collect_all(client)
    assert "a private title" not in caplog.text and PHONE not in caplog.text
    assert "fixture-cookie" not in caplog.text and "discovered=1" in caplog.text


@pytest.mark.asyncio
async def test_a_failing_list_after_the_account_check_never_fails_the_cycle(environment, cookie):
    server = ClaudeAiServer([cloud_row()], {base.SECRET_ID: two_turns()}, {})
    server.list_status = 403
    report = await cycle(server)
    assert report.outcome == "ok" and report.sessions_read == 1
    assert "discovery_permission_denied" in report.notes


@pytest.mark.asyncio
async def test_a_slow_claude_ai_ends_discovery_not_the_cycle(environment, cookie, monkeypatch):
    import asyncio

    async def stall(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(collector, "DISCOVERY_TIMEOUT", 0.05)
    monkeypatch.setattr(remote_sessions, "discover_from_conversations", stall)
    report = await cycle(ClaudeAiServer([cloud_row()], {base.SECRET_ID: two_turns()}, {}))
    assert report.outcome == "ok" and report.sessions_read == 1
    assert "discovery_timeout" in report.notes


@pytest.mark.asyncio
async def test_a_connection_failure_is_a_discovery_note(environment, cookie):
    server = ClaudeAiServer([], {}, {})
    server.hang = True
    report = await cycle(server)
    assert report.outcome == "ok" and "discovery_timeout" in report.notes


@pytest.mark.asyncio
async def test_relisting_known_sessions_does_not_count_them_as_discovered(
    environment, cookie, monkeypatch
):
    server = ClaudeAiServer([], {}, {}, conversations=[conversation(PHONE)])
    server.statuses[PHONE] = 404
    first = await cycle(server)
    monkeypatch.setattr(remote_sessions, "DISCOVERY_INTERVAL", 0.0)
    again = await cycle(server)
    assert (first.discovered, again.discovered) == (1, 0)
