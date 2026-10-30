"""The cloud-runner collector reads session history, resumes at the last closed turn and never twice."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import ClaudeCloudSessionCheckpoint, CredentialSource, UsageEvent
from app.services import claude_cloud_collector as collector
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services.claude_cloud_events import session_digest
from app.services.token_cache import TokenCache
from tests.fixtures.claude_cloud_history import assistant, result, system

ACCOUNT = "holder@example.com"
SOURCE = "fixture-source"
ORG = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
SECRET_ID = "cse_privatesessionid"
OTHER_ID = "cse_anothersession"
OPUS = "claude-opus-5-5"
QUIET = "2026-09-24T12:00:00Z"  # long ago: the session has settled
LIVE = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()


def profile():
    return {"account": {"uuid": SUBJECT, "email": ACCOUNT}, "organization": {"uuid": ORG}}


def cloud_row(sid=SECRET_ID, last_event_at=QUIET, *, repo="owner/the-repo", block=None, **extra):
    value = {
        "id": sid,
        "environment_kind": "anthropic_cloud",
        "updated_at": "2026-01-01T00:00:00Z",  # unrelated to usage; never read
        "last_event_at": last_event_at,
        "config": {
            "effort_level": "medium",
            "outcomes": [{"type": "git_repository", "git_info": {"repo": repo, "branches": ["b"]}}],
        },
        "external_metadata": {"usage": block} if block is not None else {},
    }
    value.update(extra)
    return value


def bridge_row(sid="cse_bridgesession"):
    return {"id": sid, "environment_kind": "bridge", "last_event_at": QUIET, "config": {}}


def turn(first_seq, message_ids, cumulative, *, reads=100):
    """Assistant records then the result carrying the cumulative modelUsage."""
    records, seq = [], first_seq
    for message_id in message_ids:
        records.append(assistant(seq, message_id, inp=5, out=2, read=reads, write_1h=10))
        seq += 1
    records.append(result(seq, {OPUS: cumulative}))
    return records


def two_turns():
    return [
        system(1, cwd="/home/user/the-repo"),
        *turn(
            2,
            ["m1", "m2"],
            {
                "inputTokens": 12,
                "outputTokens": 90,
                "cacheReadInputTokens": 250,
                "cacheCreationInputTokens": 20,
            },
        ),
        *turn(
            5,
            ["m3"],
            {
                "inputTokens": 20,
                "outputTokens": 140,
                "cacheReadInputTokens": 400,
                "cacheCreationInputTokens": 30,
            },
        ),
    ]


class Server:
    """An upstream with a list, per-session histories and opaque, resumable cursors."""

    def __init__(self, rows, histories, *, page=50):
        self.rows, self.histories, self.page = rows, histories, page
        self.calls: list[tuple[str, dict]] = []
        self.reject_cursors = False

    def __call__(self, request):
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        params = dict(request.url.params)
        self.calls.append((path, params))
        match = re.fullmatch(r"/v1/code/sessions/([^/]+)/events", path)
        if match:
            if self.reject_cursors and "cursor" in params:
                return httpx.Response(400, json={})
            records = self.histories[unquote(match.group(1))]
            offset = int(params["cursor"].removeprefix("cur-")) if "cursor" in params else 0
            size = min(int(params["limit"]), self.page)
            end = offset + size
            return httpx.Response(
                200,
                json={
                    "data": records[offset:end],
                    "next_cursor": f"cur-{end}" if end < len(records) else None,
                },
            )
        assert path == "/v1/code/sessions"
        offset = int(params["cursor"].removeprefix("list-")) if "cursor" in params else 0
        end = offset + int(params["limit"])
        return httpx.Response(
            200,
            json={
                "data": self.rows[offset:end],
                "next_cursor": f"list-{end}" if end < len(self.rows) else None,
            },
        )

    def history_calls(self):
        return [c for c in self.calls if c[0].endswith("/events")]


@pytest.fixture
async def environment(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'collector.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(support, "Session", Session)
    monkeypatch.setattr(support, "engine", engine)
    monkeypatch.setattr(collector, "engine", engine)
    monkeypatch.setattr("app.services.source_controls._ENABLED", False)
    from cryptography.fernet import Fernet

    from app.core.config import settings
    from app.core.encryption import EncryptionService

    monkeypatch.setattr(settings, "DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
    service = EncryptionService()
    monkeypatch.setattr(support, "encryption_service", service)
    monkeypatch.setattr(collector, "encryption_service", service)
    cache = TokenCache()
    monkeypatch.setattr(probe, "token_cache", cache)
    with Session(engine) as session:
        session.add(
            CredentialSource(
                provider_id="anthropic",
                account_id=ACCOUNT,
                source_id=SOURCE,
                source_type="sidecar",
                source_label="Fixture",
                credential_origin="file:/fixture/.claude/.credentials.json",
            )
        )
        session.commit()
    await cache.store(
        "anthropic",
        {"oauth_token": "fixture-access", "refresh_token": "unused"},
        account_id=ACCOUNT,
        source_id=SOURCE,
        source_metadata={"credential_origin": "file:/fixture/.claude/.credentials.json"},
    )
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def no_page_pause(monkeypatch):
    monkeypatch.setattr(collector, "PAGE_PAUSE", 0)
    collector._PENDING.clear()
    yield
    collector._PENDING.clear()


async def cycle(upstream):
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        return await collector.collect_account(ACCOUNT, SOURCE, client)


def events(engine):
    with Session(engine) as session:
        return list(session.exec(select(UsageEvent).order_by(UsageEvent.id)).all())


def checkpoint(engine, sid=SECRET_ID):
    with Session(engine) as session:
        return session.exec(
            select(ClaudeCloudSessionCheckpoint).where(
                ClaudeCloudSessionCheckpoint.session_digest == session_digest(sid)
            )
        ).first()


def per_model(rows):
    out = {}
    for row in rows:
        bucket = out.setdefault(row.model_id, [0, 0, 0, 0])
        for index, attr in enumerate(
            ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_create")
        ):
            bucket[index] += getattr(row, attr)
    return out


# -- what a cycle writes ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cloud_session_is_written_from_its_history(environment):
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    report = await cycle(server)
    assert report.outcome == "ok" and report.cloud_sessions == 1 and report.sessions_read == 1
    rows = events(environment)
    assert report.events == len(rows)
    assert per_model(rows) == {"opus-5.5": [20, 140, 400, 30]}  # equals the final modelUsage
    assert {r.entrypoint for r in rows} == {"cloud"}
    assert {r.sidecar_id for r in rows} == {"anthropic-cloud"}
    assert {r.session_id for r in rows} == {session_digest(SECRET_ID)}
    assert {r.project for r in rows} == {"the-repo"} and {r.cwd for r in rows} == {
        "/home/user/the-repo"
    }
    assert any(":r:" in r.event_id for r in rows) and any(":m:" in r.event_id for r in rows)


@pytest.mark.asyncio
async def test_bridge_rows_are_never_read(environment):
    server = Server([bridge_row(), cloud_row()], {SECRET_ID: two_turns()})
    report = await cycle(server)
    assert report.cloud_sessions == 1
    assert all(session_digest(SECRET_ID) == r.session_id for r in events(environment))
    assert all("cse_bridgesession" not in path for path, _ in server.history_calls())


@pytest.mark.asyncio
async def test_an_unchanged_session_is_not_read_again(environment):
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    await cycle(server)
    reads = len(server.history_calls())
    again = await cycle(server)
    assert again.sessions_read == 0 and again.events == 0
    assert len(server.history_calls()) == reads


@pytest.mark.asyncio
async def test_new_turns_resume_from_the_stored_cursor_without_duplicates(environment):
    history = two_turns()
    server = Server([cloud_row()], {SECRET_ID: history}, page=4)
    await cycle(server)
    first = len(events(environment))
    history.extend(
        turn(
            7,
            ["m4"],
            {
                "inputTokens": 25,
                "outputTokens": 170,
                "cacheReadInputTokens": 500,
                "cacheCreationInputTokens": 40,
            },
        )
    )
    server.rows[0]["last_event_at"] = "2026-09-24T13:00:00Z"
    server.calls.clear()
    report = await cycle(server)
    assert report.sessions_read == 1 and len(events(environment)) > first
    assert per_model(events(environment)) == {"opus-5.5": [25, 170, 500, 40]}
    # It asked for the stored cursor, not the start, and the checkpoint moved past the new turn.
    assert "cursor" in server.history_calls()[0][1]
    assert checkpoint(environment).committed_sequence == 8


@pytest.mark.asyncio
async def test_a_rejected_cursor_falls_back_to_reading_from_the_start(environment):
    history = two_turns()
    server = Server([cloud_row()], {SECRET_ID: history}, page=4)
    await cycle(server)
    history.extend(
        turn(
            7,
            ["m4"],
            {
                "inputTokens": 25,
                "outputTokens": 170,
                "cacheReadInputTokens": 500,
                "cacheCreationInputTokens": 40,
            },
        )
    )
    server.rows[0]["last_event_at"] = "2026-09-24T13:00:00Z"

    # The API no longer accepts the stored cursor (the first cursor it is sent), then behaves.
    class Rejecting(Server):
        rejected = False

        def __call__(self, request):
            if (
                request.url.path.endswith("/events")
                and "cursor" in request.url.params
                and not self.rejected
            ):
                self.rejected = True
                return httpx.Response(400, json={})
            return super().__call__(request)

    rejecting = Rejecting(server.rows, server.histories, page=4)
    report = await cycle(rejecting)
    assert "cursor_rejected" in report.notes and report.outcome == "ok"
    assert per_model(events(environment)) == {"opus-5.5": [25, 170, 500, 40]}


@pytest.mark.asyncio
async def test_the_stored_cursor_is_encrypted(environment):
    server = Server([cloud_row()], {SECRET_ID: two_turns()}, page=4)
    await cycle(server)
    stored = checkpoint(environment).committed_cursor_encrypted
    assert stored and "cur-" not in stored
    assert collector.encryption_service.decrypt_json(stored)["cursor"].startswith("cur-")


@pytest.mark.asyncio
async def test_a_replay_after_losing_the_checkpoint_adds_no_rows(environment):
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    await cycle(server)
    before = len(events(environment))
    with Session(environment) as session:
        for row in session.exec(select(ClaudeCloudSessionCheckpoint)).all():
            session.delete(row)
        session.commit()
    report = await cycle(server)
    assert report.sessions_read == 1 and report.events == 0
    assert len(events(environment)) == before


@pytest.mark.asyncio
async def test_a_gap_in_the_sequence_skips_only_that_session(environment):
    broken = two_turns()
    del broken[3]  # sequence 4 is missing
    server = Server(
        [cloud_row(SECRET_ID), cloud_row(OTHER_ID)], {SECRET_ID: broken, OTHER_ID: two_turns()}
    )
    report = await cycle(server)
    assert report.outcome == "ok" and any(n.startswith("session_sequence") for n in report.notes)
    assert {r.session_id for r in events(environment)} == {session_digest(OTHER_ID)}


@pytest.mark.asyncio
async def test_messages_after_the_last_result_wait_until_the_session_is_quiet(environment):
    history = [*two_turns(), assistant(7, "m4", inp=1, out=1, read=10)]
    live = Server([cloud_row(last_event_at=LIVE)], {SECRET_ID: history})
    await cycle(live)
    assert ":m:m4" not in " ".join(r.event_id for r in events(environment))
    assert checkpoint(environment).open_tail is True
    quiet = Server([cloud_row(last_event_at=LIVE)], {SECRET_ID: history})
    quiet.rows[0]["last_event_at"] = LIVE
    # The same session, now long quiet: the stored last_event_at differs, so it is read again.
    quiet.rows[0]["last_event_at"] = QUIET
    await cycle(quiet)
    ids = " ".join(r.event_id for r in events(environment))
    assert ":m:m4" in ids and not any(":r:3:" in r.event_id for r in events(environment))
    assert checkpoint(environment).open_tail is False


@pytest.mark.asyncio
async def test_old_form_events_stop_the_cycle_before_any_request(environment):
    with Session(environment) as session:
        session.add(
            UsageEvent(
                provider_id="anthropic",
                account_id=ACCOUNT,
                sidecar_id="server",
                event_id=f"cloud:{session_digest(SECRET_ID)}:1",
                ts=datetime(2026, 9, 24, tzinfo=UTC),
                kind="message",
                model_id="opus-5.5",
                entrypoint="cloud",
            )
        )
        session.commit()
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    report = await cycle(server)
    assert report.outcome == "migration_required" and server.calls == []
    assert len(events(environment)) == 1


@pytest.mark.asyncio
async def test_new_form_events_do_not_count_as_old(environment):
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    await cycle(server)
    assert (await cycle(server)).outcome == "ok"  # its own :m:/:r: events never trip the guard


# -- budgets ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_spent_page_budget_keeps_progress_and_asks_for_a_sooner_cycle(
    environment, monkeypatch
):
    history = [system(1, cwd="/home/user/the-repo")]
    seq = 2
    for number in range(4):
        history += turn(
            seq,
            [f"t{number}"],
            {
                "inputTokens": 5 * (number + 1),
                "outputTokens": 2 * (number + 1),
                "cacheReadInputTokens": 100 * (number + 1),
                "cacheCreationInputTokens": 10 * (number + 1),
            },
        )
        seq += 2
    monkeypatch.setattr(collector, "HISTORY_PAGES_PER_CYCLE", 2)
    server = Server([cloud_row()], {SECRET_ID: history}, page=3)
    first = await cycle(server)
    assert first.outcome == "partial" and first.backlog and first.history_pages == 2
    partial = len(events(environment))
    assert 0 < partial
    done = first
    for _ in range(5):
        done = await cycle(server)
        if not done.backlog:
            break
    assert done.outcome == "ok"
    assert per_model(events(environment)) == {"opus-5.5": [20, 8, 400, 40]}


@pytest.mark.asyncio
async def test_a_catch_up_cycle_skips_the_list_pass(environment, monkeypatch):
    history = [system(1, cwd="/home/user/the-repo")]
    seq = 2
    for number in range(4):
        history += turn(seq, [f"t{number}"], {"inputTokens": 5 * (number + 1)})
        seq += 2
    monkeypatch.setattr(collector, "HISTORY_PAGES_PER_CYCLE", 2)
    server = Server([cloud_row()], {SECRET_ID: history}, page=3)
    first = await cycle(server)
    assert first.backlog and first.pages == 1
    listed = sum(1 for path, _ in server.calls if path == "/v1/code/sessions")
    second = await cycle(server)
    assert "backlog_resumed" in second.notes and second.pages == 0
    assert sum(1 for path, _ in server.calls if path == "/v1/code/sessions") == listed


@pytest.mark.asyncio
async def test_a_turn_longer_than_the_budget_gets_a_larger_one(environment, monkeypatch):
    records = [system(1)]
    for index in range(2, 12):
        records.append(assistant(index, f"long{index}", inp=1, out=1, read=10))
    records.append(
        result(12, {OPUS: {"inputTokens": 10, "outputTokens": 10, "cacheReadInputTokens": 100}})
    )
    monkeypatch.setattr(collector, "HISTORY_PAGES_PER_CYCLE", 1)
    server = Server([cloud_row()], {SECRET_ID: records}, page=3)
    finished = None
    for attempt in range(1, 8):
        report = await cycle(server)
        if report.outcome == "ok":
            finished = attempt
            break
    assert finished is not None and finished > 1
    assert per_model(events(environment))["opus-5.5"][2] == 100
    assert checkpoint(environment).stalls == 0


@pytest.mark.asyncio
async def test_a_turn_longer_than_the_request_cap_is_skipped_not_reread_forever(
    environment, monkeypatch
):
    records = [system(1)]
    for index in range(2, 42):
        records.append(assistant(index, f"long{index}", inp=1, out=1, read=10))
    records.append(
        result(42, {OPUS: {"inputTokens": 40, "outputTokens": 40, "cacheReadInputTokens": 400}})
    )
    # The turn needs 14 pages; the cap allows far fewer, so it can never close within a cycle.
    monkeypatch.setattr(collector, "HISTORY_PAGES_PER_CYCLE", 1)
    monkeypatch.setattr(collector, "REQUESTS_PER_CYCLE", 6)
    monkeypatch.setattr(collector, "MAX_STALL_FACTOR", 3)
    server = Server([cloud_row()], {SECRET_ID: records}, page=3)
    skipped = None
    for number in range(1, 8):
        server.calls.clear()
        report = await cycle(server)
        # The profile request is not in `calls`; the cap counts every request of the cycle.
        assert len(server.calls) + 1 <= collector.REQUESTS_PER_CYCLE
        if "stalled_turn_skipped" in report.notes:
            skipped = number
            assert not report.backlog and report.outcome == "ok"
            break
    assert skipped == 3  # budgets 1, 2, 3 pages, then the terminal state
    assert events(environment) == []
    # Until the session changes again it is not read at all.
    server.calls.clear()
    quiet = await cycle(server)
    assert quiet.sessions_read == 0 and server.history_calls() == []
    server.rows[0]["last_event_at"] = "2026-09-25T00:00:00Z"
    again = await cycle(server)
    assert server.history_calls() and "stalled_turn_skipped" not in again.notes


@pytest.mark.asyncio
async def test_the_escalated_budget_is_held_to_the_request_cap(environment, monkeypatch):
    records = [system(1)]
    for index in range(2, 42):
        records.append(assistant(index, f"long{index}", inp=1, out=1, read=10))
    records.append(
        result(42, {OPUS: {"inputTokens": 40, "outputTokens": 40, "cacheReadInputTokens": 400}})
    )
    # The terminal state is out of reach (stall factor 50), so only the cap can limit the budget.
    # A cycle has 6 requests and a catch-up cycle skips the list, so one request (identity) is not
    # history: at most 5 history pages, even after many stalls earned a much larger budget.
    monkeypatch.setattr(collector, "HISTORY_PAGES_PER_CYCLE", 1)
    monkeypatch.setattr(collector, "REQUESTS_PER_CYCLE", 6)
    monkeypatch.setattr(collector, "MAX_STALL_FACTOR", 50)
    server = Server([cloud_row()], {SECRET_ID: records}, page=3)
    most = 0
    for _ in range(8):
        server.calls.clear()
        await cycle(server)
        most = max(most, len(server.history_calls()))
        assert len(server.calls) + 1 <= collector.REQUESTS_PER_CYCLE
    assert most == 5  # reached after a few stalls; without the clamp it keeps growing


@pytest.mark.asyncio
async def test_a_failed_cycle_keeps_the_unfinished_sessions_for_the_next_one(
    environment, monkeypatch
):
    history = [system(1, cwd="/home/user/the-repo")]
    seq = 2
    for number in range(4):
        history += turn(seq, [f"t{number}"], {"inputTokens": 5 * (number + 1)})
        seq += 2
    monkeypatch.setattr(collector, "HISTORY_PAGES_PER_CYCLE", 2)
    server = Server([cloud_row()], {SECRET_ID: history}, page=3)
    assert (await cycle(server)).backlog
    assert collector._PENDING[ACCOUNT]

    class RateLimited(Server):
        def __call__(self, request):
            if request.url.path.endswith("/events"):
                return httpx.Response(429, json={})
            return super().__call__(request)

    failed = await cycle(RateLimited(server.rows, server.histories, page=3))
    assert failed.outcome == "rate_limited"
    assert collector._PENDING[ACCOUNT]  # the failure did not lose the catch-up list
    listed = sum(1 for path, _ in server.calls if path == "/v1/code/sessions")
    again = await cycle(server)
    assert "backlog_resumed" in again.notes
    assert sum(1 for path, _ in server.calls if path == "/v1/code/sessions") == listed


@pytest.mark.asyncio
async def test_the_migration_check_runs_once_per_collect_all(environment, monkeypatch):
    for index in range(2):
        await probe.token_cache.store(
            "anthropic",
            {"oauth_token": f"access-{index}", "refresh_token": "unused"},
            account_id=f"other{index}@example.com",
            source_id=f"other-source-{index}",
            source_metadata={"credential_origin": "file:/fixture/.claude/.credentials.json"},
        )
    calls = []
    real = collector._migration_pending

    def counted():
        calls.append(1)
        return real()

    monkeypatch.setattr(collector, "_migration_pending", counted)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Server([], {}))) as client:
        reports = await collector.collect_all(client)
    assert len(reports) == 3 and len(calls) == 1


# -- cross-check --------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block", "label"),
    [
        (None, "no_block"),
        (
            {
                "input_tokens": 20,
                "output_tokens": 140,
                "cache_read_tokens": 400,
                "cache_write_tokens": 30,
            },
            "agree",
        ),
        (
            {
                "input_tokens": 20,
                "output_tokens": 999,
                "cache_read_tokens": 400,
                "cache_write_tokens": 30,
            },
            "block_higher",
        ),
        (
            {
                "input_tokens": 20,
                "output_tokens": 1,
                "cache_read_tokens": 400,
                "cache_write_tokens": 30,
            },
            "block_lower",
        ),
        (
            {
                "input_tokens": 99,
                "output_tokens": 1,
                "cache_read_tokens": 400,
                "cache_write_tokens": 30,
            },
            "mixed",
        ),
    ],
)
async def test_the_list_block_is_only_a_logged_cross_check(environment, block, label):
    server = Server([cloud_row(block=block)], {SECRET_ID: two_turns()})
    report = await cycle(server)
    assert report.crosscheck == {label: 1}
    assert checkpoint(environment).last_crosscheck == label
    assert len(events(environment)) > 0  # a disagreement never blocks or rewrites events


# -- failures and outcomes ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_busy_endpoint_gate_skips_the_cycle_without_requests(environment):
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    async with support.gate:
        report = await cycle(server)
    assert report.outcome == "busy" and server.calls == [] and events(environment) == []


@pytest.mark.asyncio
async def test_a_rejected_login_records_the_outcome_and_writes_nothing(environment):
    def upstream(request):
        return httpx.Response(401, json={})

    report = await cycle(upstream)
    assert report.outcome == "rejected" and events(environment) == []


@pytest.mark.asyncio
async def test_a_storage_failure_is_reported_and_does_not_escape(environment, monkeypatch):
    def broken(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("locked"))

    monkeypatch.setattr(collector, "_commit", broken)
    report = await cycle(Server([cloud_row()], {SECRET_ID: two_turns()}))
    assert report.outcome == "storage_error"


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_contained(environment, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(collector, "_read", broken)
    report = await cycle(Server([cloud_row()], {SECRET_ID: two_turns()}))
    assert report.outcome == "internal_error"


@pytest.mark.asyncio
async def test_a_source_change_mid_cycle_is_not_trusted(environment, monkeypatch):
    cache = probe.token_cache
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    swapped = {"done": False}

    def upstream(request):
        if request.url.path.endswith("/events") and not swapped["done"]:
            swapped["done"] = True
            asyncio.get_event_loop().create_task(
                cache.store(
                    "anthropic",
                    {"oauth_token": "rotated", "refresh_token": "x"},
                    account_id=ACCOUNT,
                    source_id=SOURCE,
                    source_metadata={
                        "credential_origin": "file:/fixture/.claude/.credentials.json"
                    },
                )
            )
        return server(request)

    report = await cycle(upstream)
    assert report.outcome in {"source_changed", "ok"}  # the swap lands between two checks
    if report.outcome == "source_changed":
        assert report.detail in {"history_get", "recheck", "sequence", "history_shape"}


@pytest.mark.asyncio
async def test_neither_ids_secrets_nor_cursors_reach_the_logs(environment, caplog):
    caplog.set_level(logging.DEBUG)
    server = Server([cloud_row()], {SECRET_ID: two_turns()}, page=4)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        reports = await collector.collect_all(client)
    assert reports[ACCOUNT].outcome == "ok"
    assert SECRET_ID not in caplog.text and "fixture-access" not in caplog.text
    assert "cur-" not in caplog.text and "the-repo" not in caplog.text
    assert "new_events=" in caplog.text and "crosscheck=" in caplog.text


@pytest.mark.asyncio
async def test_collect_all_uses_one_login_per_account(environment):
    cache = probe.token_cache
    await cache.store(
        "anthropic",
        {"oauth_token": "second-access", "refresh_token": "unused"},
        account_id=ACCOUNT,
        source_id="second-source",
        source_metadata={"credential_origin": "file:/fixture/.claude/.credentials.json"},
    )
    with Session(environment) as session:
        session.add(
            CredentialSource(
                provider_id="anthropic",
                account_id=ACCOUNT,
                source_id="second-source",
                source_type="sidecar",
                source_label="Second",
                credential_origin="file:/fixture/.claude/.credentials.json",
            )
        )
        session.commit()
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        await collector.collect_all(client)
    assert len(server.history_calls()) > 0
    assert len({p for p, _ in server.history_calls()}) == 1 and len(events(environment)) > 0
    assert sum(1 for p, _ in server.calls if p == "/v1/code/sessions") == 1  # one list pass


@pytest.mark.asyncio
async def test_the_background_task_starts_stops_and_catches_up(monkeypatch):
    seen, delays = [], []

    async def fake_collect_all(client):
        seen.append(1)
        return {ACCOUNT: collector.CycleReport(outcome="partial", backlog=len(seen) == 1)}

    real_sleep = asyncio.sleep

    async def record(seconds):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(collector, "collect_all", fake_collect_all)
    monkeypatch.setattr(collector.asyncio, "sleep", record)
    task = collector.ClaudeCloudCollector(1800)
    task.start()
    task.start()
    for _ in range(30):
        await real_sleep(0)
    await task.stop()
    assert seen and task._task is None
    assert collector.CATCH_UP_DELAY in delays  # work was left: the next cycle came sooner
    assert 1800 in delays  # and with nothing left it returned to the interval


def test_the_collector_imports_only_what_the_server_image_ships():
    """The image copies app/ and a few named scripts; the sidecar extractors are not among them."""
    from pathlib import Path

    for module in (collector, __import__("app.services.claude_cloud_events", fromlist=["x"])):
        source = Path(module.__file__).read_text()
        assert "event_extractors" not in source and "sidecar_pkg" not in source


# -- login logging ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_login_outcome_is_logged_by_kind_without_ids(environment, caplog):
    await probe.token_cache.store(
        "anthropic",
        {"oauth_token": "desktop-access", "refresh_token": "unused"},
        account_id=ACCOUNT,
        source_id="aaa-desktop",
        source_metadata={"credential_origin": "keychain:Claude Desktop/oauth/abc"},
    )
    caplog.set_level(logging.INFO)
    server = Server([cloud_row()], {SECRET_ID: two_turns()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        reports = await collector.collect_all(client)
    assert reports[ACCOUNT].outcome == "ok"
    assert "aaa-desktop" not in caplog.text and "desktop-access" not in caplog.text
    assert "logins=keychain#" in caplog.text and ":ok" in caplog.text
    assert collector.session_digest("aaa-desktop")[:6] in caplog.text


@pytest.mark.asyncio
async def test_an_account_whose_logins_are_all_rejected_still_logs_them(
    environment, caplog, monkeypatch
):
    await probe.token_cache.store(
        "anthropic",
        {"oauth_token": "desktop-access", "refresh_token": "unused"},
        account_id=ACCOUNT,
        source_id="aaa-desktop",
        source_metadata={"credential_origin": "keychain:Claude Desktop/oauth/abc"},
    )

    async def rejected(account, source_id, client, **kwargs):
        report = collector.CycleReport()
        report.outcome = "unsupported_source" if source_id == "aaa-desktop" else "source_ineligible"
        return report

    monkeypatch.setattr(collector, "collect_account", rejected)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient() as client:
        reports = await collector.collect_all(client)
    assert reports[ACCOUNT].outcome == "unsupported_source"
    assert ":unsupported_source," in caplog.text and ":source_ineligible" in caplog.text


@pytest.mark.asyncio
async def test_login_kinds_use_the_origin_vocabulary(environment, caplog, monkeypatch):
    for source_id, origin in (
        ("bbb-path", "path:/fixture/.claude/.credentials.json"),
        ("ccc-bare", None),
    ):
        await probe.token_cache.store(
            "anthropic",
            {"oauth_token": "x-access", "refresh_token": "unused"},
            account_id=ACCOUNT,
            source_id=source_id,
            source_metadata={"credential_origin": origin} if origin else {},
        )

    async def rejected(account, source_id, client, **kwargs):
        report = collector.CycleReport()
        report.outcome = "source_ineligible"
        return report

    monkeypatch.setattr(collector, "collect_account", rejected)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient() as client:
        await collector.collect_all(client)
    assert "file#" in caplog.text and "sidecar#" in caplog.text and "path#" not in caplog.text


# -- the wire contract --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_contract_failure_names_the_step_that_failed(environment):
    twice = [cloud_row(), cloud_row()]
    report = await cycle(Server(twice, {SECRET_ID: two_turns()}))
    assert report.outcome == "unsupported_contract" and report.detail == "row_duplicate"

    report = await cycle(Server([cloud_row("not an id!")], {}))
    assert report.outcome == "unsupported_contract" and report.detail == "row_id"

    def misshaped(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": "nope", "next_cursor": None})

    report = await cycle(misshaped)
    assert report.outcome == "unsupported_contract" and report.detail == "page_shape"

    def not_json(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, content=b"<html>not json</html>")

    report = await cycle(not_json)
    assert report.outcome == "unsupported_contract" and report.detail == "page_get"


@pytest.mark.asyncio
async def test_a_history_page_that_breaks_the_contract_skips_that_session(environment):
    class BadHistory(Server):
        def __call__(self, request):
            if request.url.path.endswith("/events"):
                return httpx.Response(200, json={"data": "nope", "next_cursor": None})
            return super().__call__(request)

    report = await cycle(BadHistory([cloud_row()], {SECRET_ID: two_turns()}))
    assert report.outcome == "ok" and "session_history_shape" in report.notes
    assert events(environment) == []


@pytest.mark.asyncio
async def test_the_cycle_log_carries_the_step_and_a_recheck_failure_is_not_blamed_on_rows(
    environment, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    twice = [cloud_row(), cloud_row()]
    async with httpx.AsyncClient(transport=httpx.MockTransport(Server(twice, {}))) as client:
        await collector.collect_all(client)
    assert ":unsupported_contract/row_duplicate" in caplog.text

    real = collector._CollectorProbe.check_source

    async def rotated_after_sweep(self):
        if self.report.get("stage") == "recheck":
            raise probe.ProbeError("source_changed")
        await real(self)

    monkeypatch.setattr(collector._CollectorProbe, "check_source", rotated_after_sweep)
    report = await cycle(Server([cloud_row()], {SECRET_ID: two_turns()}))
    assert report.outcome == "source_changed" and report.detail == "recheck"


@pytest.mark.asyncio
async def test_a_last_page_without_next_cursor_ends_the_list(environment):
    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.params.get("cursor"):
            return httpx.Response(200, json={"data": [cloud_row(OTHER_ID)]})
        return httpx.Response(200, json={"data": [cloud_row()], "next_cursor": "page-2"})

    report = await cycle(upstream)
    assert report.pages == 2 and report.cloud_sessions == 2


@pytest.mark.asyncio
async def test_has_more_must_agree_with_the_cursor(environment):
    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": [cloud_row()], "has_more": True})

    report = await cycle(upstream)
    assert report.outcome == "unsupported_contract" and report.detail == "page_shape"


@pytest.mark.asyncio
async def test_a_full_last_page_without_a_cursor_is_noted(environment):
    rows = [cloud_row(f"cse_full{index:02d}") for index in range(collector.LIST_LIMIT)]

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    report = await cycle(upstream)
    assert "full_last_page" in report.notes
