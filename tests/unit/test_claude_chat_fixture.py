"""An exact ordinary Chat fixture never grants credential discovery or token accounting."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.models.db import CredentialSource, ProviderConfig
from app.services import claude_cloud_probe as authority
from app.services.token_cache import TokenCache

ACCOUNT = "holder@example.invalid"
SOURCE = "manual-chat-fixture"
ORG = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"
TARGET = "33333333-3333-4333-8333-333333333333"
MESSAGE = "44444444-4444-4444-8444-444444444444"
COOKIE = "synthetic-cookie-only"


def request(**changes):
    body = {
        "account_id": ACCOUNT,
        "source_id": SOURCE,
        "organization_id": ORG,
        "conversation_id": TARGET,
    }
    return SimpleNamespace(**(body | changes))


@pytest.fixture
async def environment(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'chat.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    cache = TokenCache()
    monkeypatch.setattr(authority, "engine", engine)
    monkeypatch.setattr(authority, "token_cache", cache)
    monkeypatch.setattr(authority, "_gate", asyncio.Lock())
    monkeypatch.setattr("app.services.source_controls._ENABLED", False)
    with Session(engine) as db:
        db.add(
            CredentialSource(
                provider_id="anthropic",
                account_id=ACCOUNT,
                source_id=SOURCE,
                source_type="config",
                source_label="Synthetic",
            )
        )
        db.add(
            ProviderConfig(provider_id="anthropic", account_id=ACCOUNT, claude_organization_id=ORG)
        )
        db.commit()
    await cache.store(
        "anthropic", {"cookie_sessionKey": COOKIE}, account_id=ACCOUNT, source_id=SOURCE
    )
    yield engine, cache
    engine.dispose()


def conversation(**changes):
    return {
        "uuid": TARGET,
        "name": "private-title",
        "chat_messages": [
            {
                "uuid": MESSAGE,
                "sender": "human",
                "text": "private-transcript",
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        ],
        **changes,
    }


def upstream(calls, target=None):
    def respond(req):
        calls.append(req)
        assert req.method == "GET" and req.url.host == "claude.ai"
        assert req.headers["cookie"] == f"sessionKey={COOKIE}"
        if req.url.path == "/api/account":
            return httpx.Response(200, json={"uuid": SUBJECT, "email_address": ACCOUNT})
        if req.url.path == "/api/organizations":
            return httpx.Response(200, json=[{"uuid": ORG}])
        assert req.url.path == f"/api/organizations/{ORG}/chat_conversations/{TARGET}"
        return httpx.Response(200, json=target if target is not None else conversation())

    return respond


async def run(body, handler):
    from app.services.claude_chat_fixture import inspect_chat_fixture

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await inspect_chat_fixture(body, client)


@pytest.mark.asyncio
async def test_exact_target_is_observed_without_publishing_transcript_or_claiming_usage(
    environment,
):
    calls = []
    report = await run(request(), upstream(calls))
    assert report["outcome"] == "observed" and report["requests"] == len(calls) == 3
    assert (
        report["account_verified"] and report["organization_verified"] and report["target_verified"]
    )
    assert report["messages"] == 1 and report["roles"] == {"human": 1, "assistant": 0, "unknown": 0}
    assert (
        report["history_complete"] is False and report["exact_token_accounting_verified"] is False
    )
    assert report["provenance_verified"] is False and report["ready_for_ingestion"] is False
    saved = json.dumps(report)
    for secret in (
        COOKIE,
        ACCOUNT,
        SOURCE,
        ORG,
        SUBJECT,
        TARGET,
        MESSAGE,
        "private-title",
        "private-transcript",
    ):
        assert secret not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
async def test_http_failures_stop_without_retry_or_following_redirects(environment, status):
    calls = []

    def reject(req):
        calls.append(req)
        return httpx.Response(status, headers={"location": "https://outside.invalid/"})

    report = await run(request(), reject)
    assert report["requests"] == len(calls) == 1
    assert not report["target_verified"] and report["outcome"] != "observed"


@pytest.mark.asyncio
async def test_wrong_target_discards_conversation_observations(environment):
    calls = []
    report = await run(request(), upstream(calls, conversation(uuid=MESSAGE)))
    assert report["outcome"] == "target_mismatch" and report["messages"] == 0
    assert report["target_verified"] is False


@pytest.mark.asyncio
async def test_config_revocation_during_response_discards_late_identity(environment):
    engine, _ = environment
    calls = []
    real = upstream(calls)

    def revoke(req):
        response = real(req)
        with Session(engine) as db:
            row = db.exec(
                select(ProviderConfig).where(
                    ProviderConfig.provider_id == "anthropic", ProviderConfig.account_id == ACCOUNT
                )
            ).one()
            row.enabled = False
            db.add(row)
            db.commit()
        return response

    report = await run(request(), revoke)
    assert report["outcome"] == "source_changed" and len(calls) == 1
    assert report["account_verified"] is False and report["messages"] == 0


@pytest.mark.asyncio
async def test_two_different_cookie_fields_are_ambiguous_and_never_dispatched(environment):
    _, cache = environment
    await cache.store(
        "anthropic",
        {"cookie_sessionKey": COOKIE, "session_cookie": "other-cookie"},
        account_id=ACCOUNT,
        source_id=SOURCE,
    )
    calls = []
    report = await run(request(), upstream(calls))
    assert report["outcome"] == "source_ineligible" and not calls


@pytest.mark.asyncio
async def test_native_or_sidecar_cookie_is_not_opted_in_by_this_diagnostic(environment):
    engine, _ = environment
    with Session(engine) as db:
        row = db.exec(
            select(CredentialSource).where(
                CredentialSource.provider_id == "anthropic",
                CredentialSource.account_id == ACCOUNT,
                CredentialSource.source_id == SOURCE,
            )
        ).one()
        row.source_type = "sidecar"
        db.add(row)
        db.commit()
    calls = []
    report = await run(request(), upstream(calls))
    assert report["outcome"] == "unsupported_source" and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage,category",
    [
        ({"input_tokens": 0}, "zero"),
        ({"input_tokens": 3}, "positive_integer"),
        ({"input_tokens": False}, "boolean"),
        ({"input_tokens": -1}, "invalid_number"),
        ({"input_tokens": 0.0}, "invalid_number"),
        ({"input_tokens": None}, "null"),
        ({}, "missing"),
    ],
)
async def test_observed_counter_shapes_never_become_consumption(environment, usage, category):
    calls = []
    target = conversation(chat_messages=[{"uuid": MESSAGE, "sender": "assistant", "usage": usage}])
    report = await run(request(), upstream(calls, target))
    assert report["usage"]["messages"]["fields"]["input_tokens"][category] == 1
    assert report["exact_token_accounting_verified"] is False


@pytest.mark.asyncio
async def test_remove_and_restore_same_cookie_during_response_is_still_a_new_source(environment):
    import contextvars

    _, cache = environment
    calls = []
    real = upstream(calls)

    async def respond(req):
        response = real(req)

        async def replace():
            await cache.remove_source("anthropic", ACCOUNT, SOURCE)
            await cache.store(
                "anthropic", {"cookie_sessionKey": COOKIE}, account_id=ACCOUNT, source_id=SOURCE
            )

        # The concurrent operator task must not inherit the diagnostic's no-write context.
        await asyncio.create_task(replace(), context=contextvars.Context())
        return response

    report = await run(request(), respond)
    assert report["outcome"] == "source_changed" and len(calls) == 1
    assert report["account_verified"] is False and report["messages"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("email", [None, False, 7, "not-an-email"])
async def test_missing_account_identity_is_unknown_not_an_account_mismatch(environment, email):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(200, json={"uuid": SUBJECT, "email_address": email})

    report = await run(request(), respond)
    assert report["outcome"] == "identity_unavailable" and len(calls) == 1
    assert not report["account_verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,encoding",
    [
        pytest.param(b'{"uuid":"one","uuid":"two"}', "identity", id="duplicate-key"),
        pytest.param(b'{"counter":NaN}', "identity", id="nan-constant"),
        pytest.param(b"<html>private-transcript</html>", "identity", id="html"),
        pytest.param(b"{}", "gzip", id="content-encoding"),
        # An explicit id: pytest would otherwise build the test id from the
        # 1 MiB value, and CI's verbose log and junit report choke on it.
        pytest.param(b"x" * (1024 * 1024 + 1), "identity", id="oversized"),
    ],
)
async def test_unsupported_response_bodies_stop_without_echoing_or_retrying(
    environment, raw, encoding
):
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(
            200, stream=httpx.ByteStream(raw), headers={"content-encoding": encoding}
        )

    report = await run(request(), respond)
    assert report["outcome"] in ("unsupported_contract", "response_limit") and len(calls) == 1
    assert not report["target_verified"] and "private" not in json.dumps(report)


@pytest.mark.asyncio
async def test_whole_attempt_timeout_discards_partial_authority_and_releases_gate(
    environment, monkeypatch
):
    from app.services import claude_chat_fixture as service

    monkeypatch.setattr(service, "TOTAL_TIMEOUT", 0.05)
    calls = []

    async def respond(req):
        calls.append(req)
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    report = await asyncio.wait_for(run(request(), respond), timeout=1)
    assert report["outcome"] == "timeout" and len(calls) <= 1
    assert not report["account_verified"] and report["messages"] == 0
    assert not authority._gate.locked()


@pytest.mark.asyncio
async def test_client_cancellation_never_retries_and_releases_gate(environment):
    dispatched = asyncio.Event()
    calls = []

    async def respond(req):
        calls.append(req)
        dispatched.set()
        await asyncio.Future()

    task = asyncio.create_task(run(request(), respond))
    await asyncio.wait_for(dispatched.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert len(calls) == 1 and not authority._gate.locked()


@pytest.mark.asyncio
async def test_wrong_account_and_org_never_reach_the_target(environment):
    calls = []

    def wrong_account(req):
        calls.append(req)
        return httpx.Response(200, json={"uuid": SUBJECT, "email_address": "other@example.invalid"})

    report = await run(request(), wrong_account)
    assert report["outcome"] == "identity_mismatch" and len(calls) == 1
    calls.clear()
    real = upstream(calls)

    def wrong_org(req):
        if req.url.path == "/api/organizations":
            calls.append(req)
            return httpx.Response(200, json=[{"uuid": TARGET}])
        return real(req)

    report = await run(request(), wrong_org)
    assert report["outcome"] == "organization_mismatch" and len(calls) == 2
    assert not report["target_verified"] and report["messages"] == 0


@pytest.mark.asyncio
async def test_cookie_snapshot_and_durable_metadata_remain_unchanged_after_rejection(environment):
    engine, cache = environment
    before = await cache.peek_source_instance("anthropic", ACCOUNT, SOURCE)
    with Session(engine) as db:
        source_before = db.exec(select(CredentialSource)).one().model_dump()
        config_before = db.exec(select(ProviderConfig)).one().model_dump()
    report = await run(request(), lambda req: httpx.Response(403))
    assert report["outcome"] == "rejected"
    assert await cache.peek_source_instance("anthropic", ACCOUNT, SOURCE) == before
    with Session(engine) as db:
        assert db.exec(select(CredentialSource)).one().model_dump() == source_before
        assert db.exec(select(ProviderConfig)).one().model_dump() == config_before


@pytest.mark.asyncio
async def test_busy_probe_is_inert_and_unrecognized_pagination_never_becomes_complete(environment):
    calls = []
    async with authority._gate:
        report = await run(request(), upstream(calls))
    assert report["outcome"] == "busy" and not calls
    report = await run(
        request(), upstream(calls, conversation(has_more=True, next_cursor="private-cursor"))
    )
    assert report["outcome"] == "observed" and len(calls) == 3
    assert report["history_complete"] is False and "private-cursor" not in json.dumps(report)


def test_production_budgets_are_fixed():
    from app.services import claude_chat_fixture as service

    assert service.MAX_REQUESTS == 3 and service.TOTAL_TIMEOUT == 20
    assert service.MAX_RESPONSE_BYTES == 1024 * 1024 and service.MAX_TOTAL_BYTES == 3 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,headers",
    [
        (403, {"cf-mitigated": "challenge"}),
        (200, {"content-type": "text/html; charset=utf-8"}),
    ],
)
async def test_browser_challenge_is_distinct_without_cookie_reentry_or_retry(
    environment, status, headers
):
    from app.models.claude_chat_fixture import ChatFixtureReport
    from scripts.verify_claude_chat_fixture import validate_report

    calls = []

    def challenge(req):
        calls.append(req)
        return httpx.Response(status, headers=headers, content=b"private-challenge-body")

    report = await run(request(), challenge)
    assert report["outcome"] == "challenge" and report["requests"] == len(calls) == 1
    assert report["target_verified"] is False and report["messages"] == 0
    assert "private" not in json.dumps(report)
    ChatFixtureReport.model_validate(report)
    validate_report(report)


@pytest.mark.asyncio
async def test_source_recheck_work_fits_the_attempt_budget_without_redundant_engines(
    environment, monkeypatch
):
    checks = []
    real = authority._authority_read

    def count_checks(*args):
        checks.append(None)
        return real(*args)

    monkeypatch.setattr(authority, "_authority_read", count_checks)
    calls = []
    report = await run(request(), upstream(calls))
    assert report["outcome"] == "observed" and len(calls) == 3
    assert 3 <= len(checks) <= 6


@pytest.mark.asyncio
async def test_request_ceiling_stops_a_future_fourth_dispatch(environment):
    from app.services.claude_chat_fixture import _Attempt, _load_source

    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream(calls))) as client:
        attempt = _Attempt(request(), client)
        attempt.snapshot = await _load_source(ACCOUNT, SOURCE)
        attempt.report["requests"] = 3
        with pytest.raises(authority.ProbeError, match="^response_limit$"):
            await attempt.get("/api/account", {})
    assert not calls and attempt.report["requests"] == 3
