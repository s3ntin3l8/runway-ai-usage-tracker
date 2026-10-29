"""The cloud-runner collector writes only increases, only for cloud rows, and never twice."""

import asyncio
import logging

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import (
    ClaudeCloudCollectorState,
    ClaudeCloudSessionUsage,
    CredentialSource,
    UsageEvent,
)
from app.services import claude_cloud_collector as collector
from app.services import claude_cloud_probe as probe
from app.services.token_cache import TokenCache

ACCOUNT = "holder@example.com"
SOURCE = "fixture-source"
ORG = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
SECRET_ID = "cse_privatesessionid"


def profile():
    return {"account": {"uuid": SUBJECT, "email": ACCOUNT}, "organization": {"uuid": ORG}}


def block(inp=10, out=20, read=300, write=40):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": read,
        "cache_write_tokens": write,
        "cost_usd": 9.99,
    }


def row(sid=SECRET_ID, kind="anthropic_cloud", usage=None, updated="2026-10-27T10:00:00Z", **extra):
    value = {
        "id": sid,
        "environment_kind": kind,
        "updated_at": updated,
        "last_served_model": "claude-opus-4-5-20250929",
        "external_metadata": {"usage": usage} if usage is not None else {},
    }
    value.update(extra)
    return value


@pytest.fixture
async def environment(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'collector.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(probe, "Session", Session)
    monkeypatch.setattr(probe, "engine", engine)
    monkeypatch.setattr(collector, "engine", engine)
    monkeypatch.setattr("app.services.source_controls._ENABLED", False)
    from cryptography.fernet import Fernet

    from app.core.config import settings
    from app.core.encryption import EncryptionService

    monkeypatch.setattr(settings, "DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(probe, "encryption_service", EncryptionService())
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


def listing(pages):
    """An upstream whose list is `pages` (a list of row lists); returns (handler, list calls)."""
    calls = []

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        assert request.url.path == "/v1/code/sessions"
        calls.append(request)
        index = int(request.url.params.get("cursor") or 0)
        more = index + 1 < len(pages)
        return httpx.Response(
            200, json={"data": pages[index], "next_cursor": str(index + 1) if more else None}
        )

    return upstream, calls


async def sweep(upstream):
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        return await collector.collect_account(ACCOUNT, SOURCE, client)


def events(engine):
    with Session(engine) as session:
        return list(session.exec(select(UsageEvent).order_by(UsageEvent.id)).all())


@pytest.fixture(autouse=True)
def no_page_pause(monkeypatch):
    monkeypatch.setattr(collector, "PAGE_PAUSE", 0)


def test_usage_delta_counts_only_reported_increases():
    cur = {
        "input_tokens": 5,
        "output_tokens": None,
        "cache_read_tokens": 9,
        "cache_write_tokens": 0,
    }
    assert collector.usage_delta(None, cur) == ({"input_tokens": 5, "cache_read_tokens": 9}, False)
    prev = {"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 4, "cache_write_tokens": 0}
    assert collector.usage_delta(prev, cur) == ({"cache_read_tokens": 5}, False)
    assert collector.usage_delta(cur, cur) == ({}, False)
    assert collector.usage_delta(cur, {**cur, "input_tokens": 4}) == ({}, True)


def test_read_usage_and_model_ignore_malformed_values():
    bad = row(usage={"input_tokens": -1, "output_tokens": True, "cache_read_tokens": "7"})
    assert collector.read_usage(bad) is None
    assert collector.read_usage(row()) is None
    assert collector.read_usage(row(usage=block()))["cache_read_tokens"] == 300
    assert collector.read_model(row(last_served_model="has space")) is None
    assert collector.read_model({"external_metadata": {"last_served_model": "claude-x"}}) == (
        "claude-x"
    )


@pytest.mark.asyncio
async def test_first_sweep_writes_the_block_then_only_the_increase(environment):
    upstream, _ = listing([[row(usage=block())]])
    report = await sweep(upstream)
    assert report.outcome == "ok" and report.events == 1 and report.cloud_sessions == 1
    first = events(environment)
    assert len(first) == 1
    event = first[0]
    assert (event.tokens_input, event.tokens_output) == (10, 20)
    assert (event.tokens_cache_read, event.tokens_cache_create) == (300, 40)
    assert event.model_id == "opus-4.5" and event.entrypoint == "cloud"
    assert event.session_id != SECRET_ID and SECRET_ID not in event.event_id
    assert event.sidecar_id == "server" and event.account_id == ACCOUNT

    # Nothing changed: nothing written.
    again = await sweep(listing([[row(usage=block())]])[0])
    assert again.events == 0 and len(events(environment)) == 1

    # More usage: only the increase, under a new identity.
    grown = await sweep(listing([[row(usage=block(inp=15, out=20, read=350, write=40))]])[0])
    assert grown.events == 1
    second = events(environment)[1]
    assert (second.tokens_input, second.tokens_output) == (5, 0)
    assert (second.tokens_cache_read, second.tokens_cache_create) == (50, 0)
    assert second.event_id != event.event_id


@pytest.mark.asyncio
async def test_bridge_rows_and_rows_without_a_block_write_nothing(environment):
    upstream, _ = listing(
        [
            [
                row("cse_bridge", kind="bridge", usage=block()),
                row("cse_noblock"),
                row("cse_empty", usage={}),
                row("cse_nokind", kind=None, usage=block()),
            ]
        ]
    )
    report = await sweep(upstream)
    assert report.outcome == "ok" and report.events == 0 and report.cloud_sessions == 0
    assert report.listed == 4 and events(environment) == []
    with Session(environment) as session:
        assert session.exec(select(ClaudeCloudSessionUsage)).all() == []


@pytest.mark.asyncio
async def test_a_one_sweep_dip_is_not_billed_when_it_recovers(environment):
    await sweep(listing([[row(usage=block())]])[0])
    report = await sweep(listing([[row(usage=block(read=100))]])[0])
    assert report.events == 0 and report.regressions == 1 and len(events(environment)) == 1
    recovered = await sweep(listing([[row(usage=block())]])[0])
    assert recovered.events == 0 and len(events(environment)) == 1


@pytest.mark.asyncio
async def test_a_shrink_held_for_two_sweeps_becomes_the_baseline(environment):
    await sweep(listing([[row(usage=block())]])[0])
    await sweep(listing([[row(usage=block(read=100))]])[0])
    await sweep(listing([[row(usage=block(read=100))]])[0])
    with Session(environment) as session:
        stored = session.exec(select(ClaudeCloudSessionUsage)).one()
        assert stored.tokens_cache_read == 100 and stored.regressions == 2
        assert session.get(ClaudeCloudCollectorState, ACCOUNT).last_regressions == 1
    # The session counts again from the new baseline instead of staying dead.
    grown = await sweep(listing([[row(usage=block(read=160))]])[0])
    assert grown.events == 1 and events(environment)[1].tokens_cache_read == 60


@pytest.mark.asyncio
async def test_an_unexpected_apply_failure_is_contained(environment, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(collector, "_apply", broken)
    report = await sweep(listing([[row(usage=block())]])[0])
    assert report.outcome == "internal_error"


@pytest.mark.asyncio
async def test_a_busy_endpoint_gate_skips_the_sweep_without_requests(environment):
    upstream, calls = listing([[row(usage=block())]])
    async with probe._gate:
        report = await sweep(upstream)
    assert report.outcome == "busy" and calls == [] and events(environment) == []


@pytest.mark.asyncio
async def test_a_storage_failure_is_recorded_and_does_not_escape(environment, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def broken(*args, **kwargs):
        raise OperationalError("insert", {}, Exception("locked"))

    monkeypatch.setattr(collector, "_apply", broken)
    report = await sweep(listing([[row(usage=block())]])[0])
    assert report.outcome == "storage_error"
    with Session(environment) as session:
        assert session.get(ClaudeCloudCollectorState, ACCOUNT).last_outcome == "storage_error"


@pytest.mark.asyncio
async def test_an_oversized_ordered_list_still_advances_the_high_water_mark(
    environment, monkeypatch
):
    monkeypatch.setattr(collector, "MAX_PAGES", 2)
    pages = [
        [row("cse_a", usage=block(), updated="2026-10-27T10:00:00Z")],
        [row("cse_b", usage=block(), updated="2026-10-26T10:00:00Z")],
        [row("cse_c", usage=block(), updated="2026-10-25T10:00:00Z")],
    ]
    report = await sweep(listing(pages)[0])
    assert report.outcome == "truncated" and report.notes == ["max_pages"]
    with Session(environment) as session:
        state = session.get(ClaudeCloudCollectorState, ACCOUNT)
        assert state.high_water.startswith("2026-10-27T10:00:00")
        assert state.last_outcome == "truncated"


@pytest.mark.asyncio
async def test_a_replayed_increase_is_a_duplicate_not_a_second_event(environment):
    await sweep(listing([[row(usage=block())]])[0])
    # Simulate a crash after the events landed but before the state moved forward.
    with Session(environment) as session:
        stored = session.exec(select(ClaudeCloudSessionUsage)).one()
        session.delete(stored)
        session.commit()
    report = await sweep(listing([[row(usage=block())]])[0])
    assert report.events == 1  # attempted, identical identity
    assert len(events(environment)) == 1


@pytest.mark.asyncio
async def test_a_source_change_mid_sweep_discards_the_results(environment, monkeypatch):
    real = probe._load_source
    calls = []

    async def changed(account, source_id):
        tokens, org, authority = await real(account, source_id)
        calls.append(1)
        if len(calls) > 1:
            tokens = {**tokens, "oauth_token": "someone-else"}
        return tokens, org, authority

    monkeypatch.setattr(probe, "_load_source", changed)
    report = await sweep(listing([[row(usage=block())]])[0])
    assert report.outcome == "source_changed" and events(environment) == []
    with Session(environment) as session:
        assert session.exec(select(ClaudeCloudSessionUsage)).all() == []
        assert session.get(ClaudeCloudCollectorState, ACCOUNT).last_outcome == "source_changed"


@pytest.mark.asyncio
async def test_a_rejected_login_records_the_outcome_and_writes_nothing(environment):
    def upstream(request):
        return httpx.Response(401, json={})

    report = await sweep(upstream)
    assert report.outcome == "rejected" and events(environment) == []


@pytest.mark.asyncio
async def test_paging_stops_at_the_high_water_mark_only_for_an_ordered_list(environment):
    def pages(stamp):
        return [
            [row("cse_a", usage=block(), updated=f"2026-10-27T1{stamp}:00:00Z")],
            [row("cse_b", usage=block(), updated="2026-10-26T09:00:00Z")],
            [row("cse_c", usage=block(), updated="2026-10-25T09:00:00Z")],
        ]

    upstream, calls = listing(pages(0))
    first = await sweep(upstream)
    assert first.pages == 3 and first.cloud_sessions == 3

    # Everything on the first page is older than the high-water mark, and the list is ordered.
    old = [[row("cse_a", usage=block(), updated="2026-10-20T09:00:00Z")]] + pages(0)[1:]
    upstream, calls = listing(old)
    second = await sweep(upstream)
    assert second.pages == 1 and len(calls) == 1

    # Disorder seen on a page means the list is not newest-first: no early stop.
    shuffled = [
        [
            row("cse_a", usage=block(), updated="2026-10-20T09:00:00Z"),
            row("cse_x", usage=block(), updated="2026-10-27T09:00:00Z"),
        ],
        [row("cse_b", usage=block(), updated="2026-10-27T09:00:00Z")],
    ]
    upstream, calls = listing(shuffled)
    third = await sweep(upstream)
    assert third.pages == 2

    # A row without updated_at gives no proof either.
    missing = [[row("cse_a", usage=block(), updated=None)], [row("cse_b", usage=block())]]
    upstream, calls = listing(missing)
    assert (await sweep(upstream)).pages == 2


@pytest.mark.asyncio
async def test_neither_ids_nor_secrets_reach_the_logs(environment, caplog):
    caplog.set_level(logging.DEBUG)
    upstream, _ = listing([[row(usage=block())]])
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        reports = await collector.collect_all(client)
    assert reports[ACCOUNT].outcome == "ok"
    assert SECRET_ID not in caplog.text and "fixture-access" not in caplog.text
    assert "events=1" in caplog.text
    assert "logins=" in caplog.text and ":ok" in caplog.text


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
    upstream, calls = listing([[row(usage=block())]])
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        await collector.collect_all(client)
    assert len(calls) == 1 and len(events(environment)) == 1


@pytest.mark.asyncio
async def test_the_background_task_starts_and_stops(monkeypatch):
    seen = []

    async def fake_collect_all(client):
        seen.append(1)
        return {}

    real_sleep = asyncio.sleep

    async def instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(collector, "collect_all", fake_collect_all)
    monkeypatch.setattr(collector.asyncio, "sleep", instant)
    task = collector.ClaudeCloudCollector(60)
    task.start()
    task.start()
    for _ in range(20):
        await real_sleep(0)
    await task.stop()
    assert seen and task._task is None


def test_the_collector_imports_only_what_the_server_image_ships():
    """The image copies app/ and a few named scripts; the sidecar extractors are not among them."""
    from pathlib import Path

    source = Path(collector.__file__).read_text()
    assert "event_extractors" not in source and "sidecar_pkg" not in source


@pytest.mark.asyncio
async def test_every_login_outcome_is_logged_by_kind_without_ids(environment, caplog):
    cache = probe.token_cache
    await cache.store(
        "anthropic",
        {"oauth_token": "desktop-access", "refresh_token": "unused"},
        account_id=ACCOUNT,
        source_id="aaa-desktop",
        source_metadata={"credential_origin": "keychain:Claude Desktop/oauth/abc"},
    )
    caplog.set_level(logging.INFO)
    upstream, _ = listing([[row(usage=block())]])
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        reports = await collector.collect_all(client)
    assert reports[ACCOUNT].outcome == "ok"
    assert "aaa-desktop" not in caplog.text and "desktop-access" not in caplog.text
    assert "logins=keychain#" in caplog.text and ":ok" in caplog.text
    assert collector.session_digest("aaa-desktop")[:6] in caplog.text


@pytest.mark.asyncio
async def test_an_account_whose_logins_are_all_rejected_still_logs_them(
    environment, caplog, monkeypatch
):
    cache = probe.token_cache
    await cache.store(
        "anthropic",
        {"oauth_token": "desktop-access", "refresh_token": "unused"},
        account_id=ACCOUNT,
        source_id="aaa-desktop",
        source_metadata={"credential_origin": "keychain:Claude Desktop/oauth/abc"},
    )

    async def rejected(account, source_id, client):
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

    async def rejected(account, source_id, client):
        report = collector.CycleReport()
        report.outcome = "source_ineligible"
        return report

    monkeypatch.setattr(collector, "collect_account", rejected)
    caplog.set_level(logging.INFO)
    async with httpx.AsyncClient() as client:
        await collector.collect_all(client)
    assert "file#" in caplog.text and "sidecar#" in caplog.text
    assert "path#" not in caplog.text


@pytest.mark.asyncio
async def test_a_contract_failure_names_the_step_that_failed(environment):
    twice = [row(usage=block()), row(usage=block())]
    report = await sweep(listing([twice])[0])
    assert report.outcome == "unsupported_contract" and report.detail == "row_duplicate"

    bad_id = [{**row(usage=block()), "id": "not an id!"}]
    report = await sweep(listing([bad_id])[0])
    assert report.outcome == "unsupported_contract" and report.detail == "row_id"

    def misshaped(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": "nope", "next_cursor": None})

    report = await sweep(misshaped)
    assert report.outcome == "unsupported_contract" and report.detail == "page_shape"
