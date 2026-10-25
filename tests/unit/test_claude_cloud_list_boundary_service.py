"""The explicit diagnostic binds the stopped checkpoint and exact live source."""

import httpx
import pytest

from app.models.claude_cloud_verification import VerificationState
from app.services import claude_cloud_probe as probe
from app.services.claude_cloud_verification import _digest, _source_digest
from tests.unit import test_claude_cloud_probe as sources
from tests.unit.test_claude_cloud_verification import store_for

source_environment = sources.environment


async def stopped(environment):
    store = store_for(environment)
    key = b"k" * 32
    source = await probe._load_source(sources.ACCOUNT, sources.SOURCE)
    state = VerificationState(
        account_id=sources.ACCOUNT,
        source_id=sources.SOURCE,
        job_key=key.hex(),
        source_digest=_source_digest(key, source),
        created_at=1000.0,
        deadline=2800.0,
        phase="stopped",
        outcome="unsupported_contract",
        in_flight=True,
        requests=3,
        subject=sources.SUBJECT,
        organization=sources.ORG,
        list_pages=1,
        listed_sessions=1,
        seen_sessions=[_digest(key, b"listed-session", "private-first-session")],
        seen_cursors=[_digest(key, b"cursor", ["listing", "private-page"])],
        next_cursor="private-page",
        selected_session="private-first-session",
    )
    return store, store.create(state), state


async def inspect(handle, store, upstream):
    from app.services.claude_cloud_list_boundary import inspect_list_boundary

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        return await inspect_list_boundary(handle, store, client)


@pytest.mark.asyncio
async def test_exact_boundary_is_classified_without_mutating_or_reviving_the_job(
    source_environment,
):
    import json

    from app.models.claude_cloud_list_boundary import ListBoundaryReport

    store, handle, before = await stopped(source_environment)
    calls = []

    def upstream(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=sources.profile())
        assert request.url.path == "/v1/code/sessions"
        assert request.url.params["cursor"] == "private-page"
        assert request.url.params["limit"] == "20"
        return httpx.Response(
            200,
            json={"data": [{"id": "private-other-session"}], "next_cursor": "", "has_more": False},
        )

    report = await inspect(handle, store, upstream)
    ListBoundaryReport.model_validate(report)
    assert report["outcome"] == "observed" and report["requests"] == 2
    assert report["boundary"]["cursor_state"] == "empty_string"
    assert store.snapshot(handle) == before
    assert len(calls) == 2
    text = json.dumps(report)
    for value in (
        "private-page",
        "private-other-session",
        handle,
        before.job_key,
        sources.ACCOUNT,
        sources.ORG,
    ):
        assert value not in text


@pytest.mark.asyncio
async def test_revoked_source_prevents_all_provider_requests(source_environment):
    store, handle, _ = await stopped(source_environment)
    await source_environment[1].remove_source("anthropic", sources.ACCOUNT, sources.SOURCE)
    calls = []
    report = await inspect(handle, store, lambda request: calls.append(request))
    assert report["outcome"] == "source_changed" and calls == []
    assert report["boundary"] is None and report["account_verified"] is False


@pytest.mark.asyncio
async def test_checkpoint_removal_during_the_response_suppresses_evidence(source_environment):
    store, handle, _ = await stopped(source_environment)

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=sources.profile())
        store.cancel(handle)
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    report = await inspect(handle, store, upstream)
    assert report["outcome"] == "checkpoint_changed" and report["requests"] == 2
    assert report["boundary"] is None and report["account_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome", [(401, "rejected"), (429, "rate_limited"), (302, "unavailable")]
)
async def test_failed_provider_request_is_not_retried(source_environment, status, outcome):
    store, handle, before = await stopped(source_environment)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status)

    report = await inspect(handle, store, upstream)
    assert report["outcome"] == outcome and report["requests"] == len(calls) == 1
    assert store.snapshot(handle) == before


@pytest.mark.asyncio
async def test_timeout_stops_once_without_changing_the_original_checkpoint(
    source_environment, monkeypatch
):
    import asyncio

    from app.services import claude_cloud_list_boundary as boundary

    store, handle, before = await stopped(source_environment)
    monkeypatch.setattr(boundary, "TOTAL_TIMEOUT", 0.05)
    calls = []

    async def upstream(request):
        calls.append(request)
        await asyncio.Event().wait()

    report = await inspect(handle, store, upstream)
    assert report["outcome"] == "timeout" and len(calls) == 1
    assert report["boundary"] is None and store.snapshot(handle) == before


@pytest.mark.asyncio
async def test_changed_access_bundle_is_not_substituted_for_the_recorded_source(
    source_environment, monkeypatch
):
    store, handle, _ = await stopped(source_environment)
    original = probe._load_source

    async def changed(*args):
        tokens, org, authority = await original(*args)
        return {**tokens, "oauth_token": "changed-fixture"}, org, authority

    monkeypatch.setattr(probe, "_load_source", changed)
    calls = []
    report = await inspect(handle, store, lambda request: calls.append(request))
    assert report["outcome"] == "source_changed" and calls == []


@pytest.mark.asyncio
async def test_a_different_subject_cannot_read_the_failed_page(source_environment):
    store, handle, _ = await stopped(source_environment)
    calls = []

    def upstream(request):
        calls.append(request)
        value = sources.profile()
        value["account"]["uuid"] = "33333333-3333-4333-8333-333333333333"
        return httpx.Response(200, json=value)

    report = await inspect(handle, store, upstream)
    assert report["outcome"] == "identity_mismatch" and len(calls) == 1
    assert report["boundary"] is None


@pytest.mark.asyncio
async def test_active_or_successful_jobs_are_not_inspection_targets(source_environment):
    store, handle, before = await stopped(source_environment)
    store.cancel(handle)
    handle = store.create(before.model_copy(update={"phase": "listing", "outcome": "pending"}))
    calls = []
    report = await inspect(handle, store, lambda request: calls.append(request))
    assert report["outcome"] == "checkpoint_ineligible" and calls == []


@pytest.mark.asyncio
async def test_one_mib_response_guard_remains_fail_closed(source_environment):
    store, handle, _ = await stopped(source_environment)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

    report = await inspect(handle, store, upstream)
    assert report["outcome"] == "response_limit" and len(calls) == 1
    assert report["boundary"] is None


@pytest.mark.asyncio
async def test_shared_probe_exclusion_prevents_concurrent_dispatch(source_environment):
    store, handle, _ = await stopped(source_environment)
    calls = []
    async with probe._gate:
        report = await inspect(handle, store, lambda request: calls.append(request))
    assert report["outcome"] == "busy" and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,category,duplicates,nonfinite",
    [
        (b'{"data":[],"data":[],"next_cursor":null}', "duplicate_keys", 1, 0),
        (b'{"data":[],"extra":NaN,"next_cursor":null}', "nonfinite_values", 0, 1),
        (b"{broken", "invalid_json", 0, 0),
    ],
)
async def test_failed_page_decoder_exposes_only_fixed_error_categories(
    source_environment, body, category, duplicates, nonfinite
):
    from app.models.claude_cloud_list_boundary import ListBoundaryReport

    store, handle, before = await stopped(source_environment)

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=sources.profile())
        return httpx.Response(200, content=body)

    report = await inspect(handle, store, upstream)
    ListBoundaryReport.model_validate(report)
    assert report["outcome"] == "unsupported_contract" and report["requests"] == 2
    assert report["json_state"] == category
    assert (
        report["duplicate_json_keys"] == duplicates and report["nonfinite_json_values"] == nonfinite
    )
    assert report["boundary"] is None
    assert store.snapshot(handle) == before


@pytest.mark.asyncio
async def test_unsupported_encoding_is_reported_without_decoding_or_retries(source_environment):
    from app.models.claude_cloud_list_boundary import ListBoundaryReport

    store, handle, _ = await stopped(source_environment)

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=sources.profile())
        return httpx.Response(200, content=b"", headers={"content-encoding": "gzip"})

    report = await inspect(handle, store, upstream)
    ListBoundaryReport.model_validate(report)
    assert report["outcome"] == "unsupported_contract" and report["requests"] == 2
    assert report["response_encoding"] == "unsupported" and report["json_state"] == "not_attempted"
    assert report["boundary"] is None
