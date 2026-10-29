"""Observable continuation, never page length, determines verification traversal."""

import copy
import json

import httpx
import pytest

from app.services import claude_cloud_probe as probe
from tests.unit import test_claude_cloud_verification as fixtures
from tests.unit.test_claude_cloud_probe import profile, usage

source_environment = fixtures.source_environment


@pytest.mark.parametrize(
    ("limit", "count", "explicit_end"),
    [(20, 0, False), (20, 14, False), (20, 20, False), (50, 50, False), (20, 14, True)],
)
def test_omitted_terminal_cursor_is_accepted_without_mutating_evidence(limit, count, explicit_end):
    from app.services.claude_cloud_verification import _verification_page

    payload = {"data": [{"id": "fixture-" + str(i)} for i in range(count)]}
    if explicit_end:
        payload["has_more"] = False
    original = copy.deepcopy(payload)
    rows, cursor = _verification_page(payload, limit)
    assert rows == payload["data"] and cursor is None
    assert payload == original and "next_cursor" not in payload
    with pytest.raises(probe.ProbeError, match="^unsupported_contract$"):
        probe.page(payload, limit)  # Legacy v1/v2 parsing deliberately stays unchanged.


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "has_more": True},
        {"data": [], "has_more": None},
        {"data": [], "has_more": "false"},
        {"data": [], "has_more": 0},
        {"data": [], "next_cursor": None, "has_more": True},
        {"data": [], "next_cursor": "next", "has_more": False},
        {"data": [], "next_cursor": "", "has_more": False},
        {"data": [], "next_cursor": False},
        {"data": None},
        {"other": []},
        {"data": [None]},
        [],
    ],
)
def test_omission_never_hides_invalid_or_contradictory_paging(payload):
    from app.services.claude_cloud_verification import _verification_page

    with pytest.raises(probe.ProbeError, match="^unsupported_contract$"):
        _verification_page(payload, 20)


def test_a_short_page_with_a_cursor_still_requires_continuation():
    from app.services.claude_cloud_verification import _verification_page

    rows, cursor = _verification_page(
        {"data": [{"id": "fixture"}], "next_cursor": "continue", "has_more": True}, 20
    )
    assert len(rows) == 1 and cursor == "continue"
    with pytest.raises(probe.ProbeError, match="^unsupported_contract$"):
        _verification_page({"data": [{}] * 21}, 20)


@pytest.mark.asyncio
async def test_list_and_history_omission_finish_durably_without_replaying(source_environment):
    from app.services.claude_cloud_verification import build_verification_report

    store = fixtures.store_for(source_environment)
    handle = await fixtures.start(store)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/code/sessions":
            # A short nonterminal page must not finish the list.
            body = {"data": [{"id": "fixture-first" if cursor is None else "fixture-last"}]}
            if cursor is None:
                body["next_cursor"] = "list-next"
            return httpx.Response(200, json=body)
        body = {"data": [{"sequence_num": 1 if cursor is None else 2, "payload": usage()}]}
        if cursor is None:
            body["next_cursor"] = "history-next"
            body["has_more"] = True
        else:
            body["has_more"] = False
        return httpx.Response(200, json=body)

    state = await fixtures.advance(handle, store, upstream)
    assert state.outcome == "complete" and state.list_complete and state.history_complete
    assert state.list_pages == 2 and state.history_pages == 2 and state.listed_sessions == 2
    assert len(calls) == state.requests == 5 and state.history_events == 2
    assert state.rejected_list is None and store.snapshot(handle) == state
    await fixtures.advance(handle, store, upstream)
    assert len(calls) == 5
    public = build_verification_report(handle, state)
    assert public["ready_for_ingestion"] is False and public["cloud_contract_verified"] is False
    assert "fixture-first" not in json.dumps(public)


@pytest.mark.asyncio
async def test_frozen_omission_diagnostic_still_decrypts_and_archives(source_environment):
    from app.models.claude_cloud_list_observation import RejectedListEvidence
    from app.models.claude_cloud_verification import VerificationReport
    from app.services.claude_cloud_verification import build_verification_report
    from scripts.claude_cloud_list_boundary import observe_list_boundary
    from tests.unit.test_claude_cloud_probe import ORG, SUBJECT

    store = fixtures.store_for(source_environment)
    handle = await fixtures.start(store)
    claim = store.claim(handle)
    state = claim.state
    state.phase = "stopped"
    state.outcome = "unsupported_contract"
    state.requests = 2
    state.in_flight = True
    state.subject = SUBJECT
    state.organization = ORG
    state.last_verified_at = 1000.0
    state.last_http_status = 200
    state.rejected_list = RejectedListEvidence(
        boundary=observe_list_boundary(
            {"data": [{"id": "fixture-legacy"}]}, bytes.fromhex(state.job_key), set(), set()
        ),
        response_encoding="identity",
        json_state="decoded",
    )
    assert state.rejected_list.boundary.cursor_supported is False
    store.save(claim)
    store.release(claim)
    restored = store.snapshot(handle)
    VerificationReport.model_validate(build_verification_report(handle, restored))
    store.archive(handle, claim.revision)
    assert store.archived_snapshot(handle) == restored
    assert store.archived_snapshot(handle).rejected_list.boundary.cursor_supported is False


@pytest.mark.asyncio
async def test_an_advertised_continuation_without_cursor_stays_terminal_failure(source_environment):
    store = fixtures.store_for(source_environment)
    handle = await fixtures.start(store)
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=profile()
            if request.url.path.endswith("/profile")
            else {"data": [], "has_more": True},
        )

    state = await fixtures.advance(handle, store, upstream)
    assert state.outcome == "unsupported_contract" and not state.list_complete and len(calls) == 2
    assert state.rejected_list.boundary.has_more_state == "true"
    await fixtures.advance(handle, store, upstream)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_history_contradiction_cannot_claim_completion_or_replay(source_environment):
    store = fixtures.store_for(source_environment)
    handle = await fixtures.start(store)
    calls = []
    contradictory = {
        "data": [{"sequence_num": 1, "payload": usage()}],
        "next_cursor": "history-next",
        "has_more": False,
    }

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if request.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "fixture-history"}], "next_cursor": None}
            )
        return httpx.Response(200, json=contradictory)

    state = await fixtures.advance(handle, store, upstream)
    assert state.outcome == "unsupported_contract" and state.list_complete
    assert not state.history_complete and state.history_pages == state.history_events == 0
    assert state.requests == len(calls) == 3 and state.rejected_list is None
    await fixtures.advance(handle, store, upstream)
    assert len(calls) == 3
    # The stricter agreement rule belongs only to complete-history verification.
    assert probe.page(contradictory, 50)[1] == "history-next"
