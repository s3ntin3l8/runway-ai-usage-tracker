"""Complete page chains use one exact source, durable intent and fixed budgets."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.services import claude_cloud_probe as probe
from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore
from tests.unit import test_claude_cloud_probe as source_fixtures
from tests.unit.test_claude_cloud_probe import ACCOUNT, ORG, SOURCE, SUBJECT, profile, usage

source_environment = source_fixtures.environment


def store_for(environment):
    engine, _ = environment
    return CheckpointStore(
        Path(engine.url.database), probe.encryption_service, clock=lambda: 1000.0
    )


async def start(store, **changes):
    from app.models.claude_cloud_verification import VerificationStart
    from app.services.claude_cloud_verification import start_verification

    return await start_verification(
        VerificationStart(account_id=ACCOUNT, source_id=SOURCE, **changes), store
    )


async def advance(handle, store, upstream):
    from app.services.claude_cloud_verification import advance_verification

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        state = await advance_verification(handle, store, client)
        from app.models.claude_cloud_verification import VerificationReport
        from app.services.claude_cloud_verification import build_verification_report

        VerificationReport.model_validate(build_verification_report(handle, state))
        return state


@pytest.mark.asyncio
async def test_complete_chains_resume_after_five_gets_without_replaying_pages(source_environment):
    from app.services.claude_cloud_verification import build_verification_report

    store = store_for(source_environment)
    handle = await start(store)
    calls = []
    histories = []

    def upstream(request):
        state = store.snapshot(handle)
        assert state.in_flight is True
        assert state.requests == len(calls) + 1  # durable charge before actual network
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer fixture-access"
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        assert request.headers["x-organization-uuid"] == ORG
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/code/sessions":
            assert request.url.params["limit"] == "20"
            sid = "session-fixture" if cursor is None else "session-other"
            return httpx.Response(
                200,
                json={"data": [{"id": sid}], "next_cursor": "list2" if cursor is None else None},
            )
        assert request.url.params["limit"] == "50" and request.url.params["sort_order"] == "asc"
        histories.append(cursor)
        index = {None: 1, "history2": 2, "history3": 3, "history4": 4}[cursor]
        record = usage()
        record["message"]["id"] = f"private-message-{index}"
        return httpx.Response(
            200,
            json={
                "data": [{"sequence_num": str(index), "payload": record}],
                "next_cursor": f"history{index + 1}" if index < 4 else None,
            },
        )

    first = await advance(handle, store, upstream)
    assert len(calls) == 5
    assert first.phase == "history" and first.list_complete and not first.history_complete
    assert first.next_cursor == "history3" and first.history_events == 2
    assert first.subject == SUBJECT and first.organization == ORG
    second = await advance(handle, store, upstream)
    assert histories == [None, "history2", "history3", "history4"]
    assert len(calls) == 8 and second.requests == 8
    assert second.phase == second.outcome == "complete"
    assert second.list_complete and second.history_complete
    assert second.listed_sessions == 2 and second.history_events == 4
    report = build_verification_report(handle, second)
    assert report["audit"]["assistant"]["unique_messages"] == 4
    assert report["audit"]["assistant"]["input_cache_observations"]["input_tokens"] == 40
    for key in (
        "cloud_contract_verified",
        "ready_for_ingestion",
        "live_acceptance_passed",
        "provenance_verified",
    ):
        assert report[key] is False
    encoded = json.dumps(report)
    for value in (
        ACCOUNT,
        SOURCE,
        ORG,
        SUBJECT,
        "fixture-access",
        "private-message",
        "private-transcript",
        "session-fixture",
        "history4",
        second.job_key,
        second.source_digest,
    ):
        assert value not in encoded
    again = await advance(handle, store, upstream)
    assert again.phase == "complete" and len(calls) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome",
    [(401, "rejected"), (403, "rejected"), (429, "rate_limited"), (302, "unavailable")],
)
async def test_provider_failure_is_terminal_without_refresh_or_fallback(
    source_environment, status, outcome
):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "private-provider-error"})

    state = await advance(handle, store, upstream)
    assert state.phase == "stopped" and state.outcome == outcome
    assert state.requests == 1 and state.last_http_status == status
    assert all(request.method == "GET" for request in calls)
    await advance(handle, store, upstream)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_intent_persistence_prevents_the_first_provider_call(
    source_environment, monkeypatch
):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    def fail(_):
        raise CheckpointError("checkpoint_unavailable")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        await advance(handle, store, lambda request: calls.append(request))
    assert calls == [] and store.snapshot(handle).requests == 0


@pytest.mark.asyncio
async def test_failed_response_checkpoint_never_reissues_a_known_request(
    source_environment, monkeypatch
):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []
    original = store.save

    def fail_response(claim):
        if not claim.state.in_flight:
            raise CheckpointError("checkpoint_unavailable")
        original(claim)

    monkeypatch.setattr(store, "save", fail_response)

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=profile())

    with pytest.raises(CheckpointError):
        await advance(handle, store, upstream)
    assert len(calls) == 1 and store.snapshot(handle).in_flight
    monkeypatch.setattr(store, "save", original)
    stopped = await advance(handle, store, upstream)
    assert stopped.outcome == "interrupted" and len(calls) == 1


@pytest.mark.asyncio
async def test_changed_source_before_resume_makes_no_provider_request(source_environment):
    _, cache = source_environment
    store = store_for(source_environment)
    handle = await start(store)
    calls = []
    await cache.remove_source("anthropic", ACCOUNT, SOURCE)
    state = await advance(handle, store, lambda request: calls.append(request))
    assert state.outcome == "source_changed" and calls == []


@pytest.mark.asyncio
async def test_attempt_budget_cannot_reset_on_a_new_step(source_environment):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []
    owner = store.claim(handle)
    owner.state.requests = 512
    store.save(owner)
    store.release(owner)
    state = await advance(handle, store, lambda request: calls.append(request))
    assert state.outcome == "limit_reached" and state.requests == 512 and calls == []


@pytest.mark.asyncio
async def test_whole_job_byte_budget_bounds_even_the_profile(source_environment):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []
    owner = store.claim(handle)
    owner.state.total_bytes = 256 * 1024 * 1024 - 32
    store.save(owner)
    store.release(owner)

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=profile())

    state = await advance(handle, store, upstream)
    assert state.outcome == "response_limit" and len(calls) == 1
    assert state.total_bytes == 256 * 1024 * 1024 - 32


@pytest.mark.asyncio
async def test_cancelled_inflight_step_is_not_automatically_replayed(source_environment):
    store = store_for(source_environment)
    handle = await start(store)
    entered = asyncio.Event()
    calls = []

    async def upstream(request):
        calls.append(request)
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(advance(handle, store, upstream))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert store.snapshot(handle).in_flight
    result = await advance(handle, store, upstream)
    assert result.outcome == "interrupted" and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,expected", [("session-later", "complete"), ("session-absent", "target_not_found")]
)
async def test_target_selection_exhausts_the_list_before_history(
    source_environment, target, expected
):
    store = store_for(source_environment)
    handle = await start(store, session_id=target)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            later = request.url.params.get("cursor") == "second"
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "session-later" if later else "session-first"}],
                    "next_cursor": None if later else "second",
                },
            )
        assert "session-later" in request.url.path
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    state = await advance(handle, store, upstream)
    assert state.outcome == expected and state.list_complete
    assert len(calls) == (4 if expected == "complete" else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cursor_loop", "duplicate_session"])
async def test_list_repetition_stops_without_an_automatic_retry(source_environment, mode):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        second = request.url.params.get("cursor") == "repeat"
        sid = "session-first" if not second or mode == "duplicate_session" else "session-second"
        cursor = "different" if second and mode == "duplicate_session" else "repeat"
        return httpx.Response(200, json={"data": [{"id": sid}], "next_cursor": cursor})

    state = await advance(handle, store, upstream)
    assert state.outcome == "unsupported_contract" and len(calls) == 3
    assert state.rejected_list is not None
    boundary = state.rejected_list.boundary
    assert boundary is not None
    assert boundary.cursor_repeated is (mode == "cursor_loop")
    assert boundary.previously_seen_ids == (1 if mode == "duplicate_session" else 0)
    await advance(handle, store, upstream)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_authority_change_during_history_discards_public_usage(source_environment):
    from app.services.claude_cloud_verification import build_verification_report

    _, cache = source_environment
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    async def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        if request.url.params.get("cursor") == "next":
            await cache.remove_source("anthropic", ACCOUNT, SOURCE)
        return httpx.Response(
            200,
            json={
                "data": [{"sequence_num": len(calls), "payload": usage()}],
                "next_cursor": "next" if len(calls) == 3 else None,
            },
        )

    state = await advance(handle, store, upstream)
    report = build_verification_report(handle, state)
    assert state.outcome == "source_changed" and len(calls) == 4
    assert report["account_verified"] is False and report["history_events"] == 0
    assert report["audit"]["assistant"]["records"] == 0 and state.usage_records == []


@pytest.mark.asyncio
async def test_full_history_keeps_private_context_and_exposes_only_scope_counts(source_environment):
    from app.services.claude_cloud_verification import build_verification_report

    store = store_for(source_environment)
    handle = await start(store)
    main = usage()
    main.update(session_id="private-sdk-session", version="2.1.45")
    child = usage()
    child["message"]["id"] = "private-child-message"
    child.update(
        session_id="private-sdk-session", parent_tool_use_id="private-parent", version="2.1.45"
    )
    records = [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "private-sdk-session",
            "version": "2.1.45",
            "cwd": "private-directory",
        },
        main,
        child,
    ]

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"sequence_num": index, "payload": value} for index, value in enumerate(records)
                ],
                "next_cursor": None,
            },
        )

    state = await advance(handle, store, upstream)
    assert [row.event_sequence for row in state.usage_records] == [0, 1, 2]
    report = build_verification_report(handle, state)
    assert report["scope_observations"]["initializations"] == 1
    assert report["scope_observations"]["session_scopes"] == 1
    assert report["scope_observations"]["parent_scopes"] == 1
    assert report["scope_observations"]["record_versions"] == 1
    assert report["audit"]["assistant"]["subagent_records"] == 1
    assert "private-" not in json.dumps(report)
    assert "2.1.45" not in json.dumps(report)


@pytest.mark.asyncio
async def test_local_subagent_scope_can_be_compared_without_including_it_in_main_usage(
    source_environment,
):
    from app.services.claude_cloud_ledger import usage_only_record
    from app.services.claude_cloud_verification import build_verification_report

    store = store_for(source_environment)
    key = bytes(range(32))
    raw = {**usage(), "parent_tool_use_id": "private-parent", "session_id": "private-session"}
    handle = await start(
        store,
        job_key=key.hex(),
        expected_local=[usage_only_record(raw, key)],
        declared_category="cowork",
    )

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200, json={"data": [{"sequence_num": 0, "payload": raw}], "next_cursor": None}
        )

    state = await advance(handle, store, upstream)
    report = build_verification_report(handle, state)
    assert report["subagent_comparison"]["all_expected_input_cache_match"] is True
    assert report["comparison"]["all_expected_input_cache_match"] is False
    assert report["audit"]["assistant"]["input_cache_observations"] is None
    assert report["scope_observations"]["shared_session_scopes"] == 1
    assert report["scope_observations"]["shared_parent_scopes"] == 1


@pytest.mark.asyncio
async def test_last_allowed_request_marks_the_job_limited_in_the_same_step(source_environment):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []
    owner = store.claim(handle)
    owner.state.requests = 507
    store.save(owner)
    store.release(owner)

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        index = len(calls)
        return httpx.Response(
            200, json={"data": [{"id": f"session-{index}"}], "next_cursor": f"cursor-{index}"}
        )

    state = await advance(handle, store, upstream)
    assert state.requests == 512 and len(calls) == 5
    assert state.phase == "stopped" and state.outcome == "limit_reached"


@pytest.mark.asyncio
async def test_status_projects_last_identity_check_instead_of_implying_a_fresh_lookup(
    source_environment,
):
    from app.services.claude_cloud_verification import build_verification_report

    store = store_for(source_environment)
    handle = await start(store)

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    state = await advance(handle, store, upstream)
    store.clock = lambda: 1500.0
    report = build_verification_report(handle, state)
    assert report["last_verified_at"] == 1000.0
    assert report["started_at"] == 1000.0
    assert report["deadline"] == 2800.0


@pytest.mark.asyncio
async def test_status_rechecks_local_authority_without_provider_or_checkpoint_writes(
    source_environment,
):
    from app.services.claude_cloud_verification import build_verification_report, read_verification

    _, cache = source_environment
    store = store_for(source_environment)
    handle = await start(store)
    before = store.snapshot(handle)
    assert (await read_verification(handle, store)).outcome == "pending"
    await cache.remove_source("anthropic", ACCOUNT, SOURCE)
    revoked = await read_verification(handle, store)
    report = build_verification_report(handle, revoked)
    assert report["outcome"] == "source_changed"
    assert report["account_verified"] is False and report["history_events"] == 0
    assert store.snapshot(handle) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment,kind,label",
    [
        ({"kind": "anthropic_cloud"}, "anthropic_cloud", "anthropic_cloud"),
        ({"kind": "bridge"}, "bridge", "bridge"),
        ({"kind": "byoc_runner"}, "other", "byoc_runner"),
        ({"kind": "Has Spaces/Private-Name"}, "unknown", None),
        ({"kind": 7}, "unknown", None),
        ({}, "unknown", None),
        (None, "unknown", None),
    ],
)
async def test_environment_kind_is_reported_only_as_a_safe_label(
    source_environment, environment, kind, label
):
    from app.services.claude_cloud_verification import build_verification_report

    store = store_for(source_environment)
    handle = await start(store, session_id="session-fixture")

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            row = {"id": "session-fixture"}
            if environment is not None:
                row["environment"] = environment
            return httpx.Response(200, json={"data": [row], "next_cursor": None})
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    state = await advance(handle, store, upstream)
    report = build_verification_report(handle, state)
    assert report["execution_kind"] == kind and report["environment_kind"] == label
