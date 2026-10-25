"""Failure-time list observations never require replaying a provider response."""

import json

import httpx
import pytest

from app.services.claude_cloud_verification import build_verification_report, read_verification
from tests.unit import test_claude_cloud_verification as fixtures
from tests.unit.test_claude_cloud_probe import ACCOUNT, SOURCE, profile
from tests.unit.test_claude_cloud_verification import advance, start, store_for

source_environment = fixtures.source_environment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "category"),
    [
        (b'{"data":[],"next_cursor":""}', "decoded"),
        (b'{"data":[],"data":[]}', "duplicate_keys"),
        (b'{"data":NaN}', "nonfinite_values"),
        (b"{not-json}", "invalid_json"),
    ],
)
async def test_rejected_page_is_observed_and_persisted_without_another_get(
    source_environment, body, category
):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, content=body)

    state = await advance(handle, store, upstream)
    assert state.outcome == "unsupported_contract" and len(calls) == 2
    evidence = state.rejected_list
    assert evidence.json_state == category
    assert evidence.response_encoding == "identity"
    if category == "decoded":
        assert evidence.boundary.cursor_state == "empty_string"
    else:
        assert evidence.boundary is None
    assert store.snapshot(handle).rejected_list == evidence
    report = build_verification_report(handle, state)
    assert report["rejected_list"] == evidence.model_dump()
    assert report["ready_for_ingestion"] is False
    await advance(handle, store, upstream)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_old_checkpoint_has_unknown_rejection_and_source_change_suppresses_new_evidence(
    source_environment,
):
    _, cache = source_environment
    store = store_for(source_environment)
    handle = await start(store)
    assert store.snapshot(handle).rejected_list is None

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(
            200, json={"data": [{"id": "secret invalid id"}], "next_cursor": None}
        )

    state = await advance(handle, store, upstream)
    assert state.rejected_list.boundary.invalid_ids == 1
    encoded = json.dumps(build_verification_report(handle, state))
    assert "secret invalid id" not in encoded
    await cache.remove_source("anthropic", ACCOUNT, SOURCE)
    suppressed = await read_verification(handle, store)
    assert suppressed.outcome == "source_changed"
    from app.models.claude_cloud_verification import VerificationState

    VerificationState.model_validate(suppressed.model_dump())
    assert suppressed.rejected_list is None
    assert build_verification_report(handle, suppressed)["rejected_list"] is None
    assert store.snapshot(handle).rejected_list is not None


@pytest.mark.asyncio
async def test_supported_decoder_encodings_remain_accepted(source_environment):
    store = store_for(source_environment)
    handle = await start(store)

    def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, content='{"data":[],"next_cursor":null}'.encode("utf-16"))

    state = await advance(handle, store, upstream)
    assert state.outcome == "no_sessions" and state.rejected_list is None


@pytest.mark.asyncio
async def test_unsupported_encoding_is_recorded_without_reading_or_retrying(source_environment):
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(
            200, content=b"private body", headers={"content-encoding": "unsupported"}
        )

    state = await advance(handle, store, upstream)
    assert state.outcome == "unsupported_contract" and len(calls) == 2
    assert state.rejected_list.response_encoding == "unsupported"
    assert state.rejected_list.json_state == "not_attempted"
    assert state.rejected_list.boundary is None


@pytest.mark.asyncio
async def test_failed_rejection_persistence_cannot_replay_the_page(source_environment, monkeypatch):
    from app.services.claude_cloud_checkpoints import CheckpointError

    store = store_for(source_environment)
    handle = await start(store)
    save = store.save
    calls = []

    def fail(claim):
        if claim.state.rejected_list is not None:
            raise CheckpointError("checkpoint_unavailable")
        return save(claim)

    monkeypatch.setattr(store, "save", fail)

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=profile() if request.url.path.endswith("/profile") else {})

    with pytest.raises(CheckpointError, match="^checkpoint_unavailable$"):
        await advance(handle, store, upstream)
    assert len(calls) == 2 and store.snapshot(handle).in_flight is True
    state = await advance(handle, store, upstream)
    assert state.outcome == "interrupted" and len(calls) == 2
    assert state.rejected_list is None


@pytest.mark.asyncio
async def test_legacy_checkpoint_and_report_without_new_field_remain_valid(source_environment):
    from app.models.claude_cloud_verification import VerificationReport, VerificationState
    from scripts.verify_claude_cloud import _history_report

    store = store_for(source_environment)
    handle = await start(store)
    old = store.snapshot(handle).model_dump()
    old.pop("rejected_list")
    state = VerificationState.model_validate(old)
    assert state.rejected_list is None
    public = build_verification_report(handle, state)
    public.pop("rejected_list")
    VerificationReport.model_validate(public)
    public.pop("job_handle")
    _history_report(public)


@pytest.mark.asyncio
async def test_authority_change_during_response_has_no_public_or_durable_rejection(
    source_environment,
):
    _, cache = source_environment
    store = store_for(source_environment)
    handle = await start(store)
    calls = []

    async def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        await cache.remove_source("anthropic", ACCOUNT, SOURCE)
        return httpx.Response(200, content=b'{"private raw key":NaN}')

    state = await advance(handle, store, upstream)
    assert state.outcome == "source_changed" and len(calls) == 2
    assert state.rejected_list is None
    assert build_verification_report(handle, state)["rejected_list"] is None


@pytest.mark.asyncio
async def test_rejection_metadata_roundtrips_standalone_and_rejects_unknown_fields(
    source_environment, tmp_path
):
    import copy
    import subprocess
    import sys
    from pathlib import Path

    from pydantic import ValidationError

    from app.models.claude_cloud_list_observation import RejectedListEvidence
    from scripts.verify_claude_cloud import EvidenceError, _history_report

    store = store_for(source_environment)
    handle = await start(store)

    def upstream(request):
        return httpx.Response(200, json=profile() if request.url.path.endswith("/profile") else {})

    state = await advance(handle, store, upstream)
    public = build_verification_report(handle, state)
    public.pop("job_handle")
    bad = copy.deepcopy(public)
    bad["rejected_list"]["private_key"] = (
        "private-value"  # pragma: allowlist secret (synthetic forbidden field)
    )
    with pytest.raises(EvidenceError, match="^invalid_evidence$"):
        _history_report(bad)
    with pytest.raises(ValidationError):
        RejectedListEvidence.model_validate(bad["rejected_list"])
    value = {
        "format_version": 2,
        "source_commit": "d" * 40,
        "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "e" * 64,
        "observed_at": "2026-10-25T00:00:00Z",
        "deployment": "isolated-test",
        "report": public,
    }
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(Path("scripts/verify_claude_cloud.py").resolve())],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=10,
    )
    assert result.returncode == 2
    decoded = json.loads(result.stdout)
    assert decoded["status"] == "stop"
    assert decoded["report"]["rejected_list"] == public["rejected_list"]


@pytest.mark.asyncio
async def test_source_change_on_rejected_encoding_is_rechecked_before_persistence(
    source_environment,
):
    _, cache = source_environment
    store = store_for(source_environment)
    handle = await start(store)

    async def upstream(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        await cache.remove_source("anthropic", ACCOUNT, SOURCE)
        return httpx.Response(
            200, content=b"private body", headers={"content-encoding": "unsupported"}
        )

    state = await advance(handle, store, upstream)
    assert state.outcome == "source_changed"
    assert store.snapshot(handle).rejected_list is None


@pytest.mark.parametrize(
    "change",
    [
        {"response_encoding": "unknown"},
        {"response_encoding": "identity"},
        {"duplicate_json_keys": True},
        {"json_state": "duplicate_keys", "response_encoding": "identity"},
        {"json_state": "decoded", "response_encoding": "identity"},
        {"raw_payload": "private"},
    ],
)
def test_rejected_metadata_strict_models_and_standalone_validator_agree(change):
    from pydantic import ValidationError

    from app.models.claude_cloud_list_observation import RejectedListEvidence
    from scripts.verify_claude_cloud import EvidenceError, _rejected_list

    evidence = {
        "boundary": None,
        "response_encoding": "unsupported",
        "json_state": "not_attempted",
        "duplicate_json_keys": 0,
        "nonfinite_json_values": 0,
        **change,
    }
    with pytest.raises(ValidationError):
        RejectedListEvidence.model_validate(evidence)
    with pytest.raises(EvidenceError, match="^invalid_evidence$"):
        _rejected_list(evidence)


@pytest.mark.asyncio
async def test_future_decoder_disagreement_keeps_rejection_evidence_unknown(
    source_environment, monkeypatch
):
    from app.services import claude_cloud_verification as worker

    store = store_for(source_environment)
    handle = await start(store)
    monkeypatch.setattr(worker, "decode_boundary_json", lambda body: {})
    calls = []

    def upstream(request):
        calls.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, content=b'{"data":[],"data":[]}')

    state = await advance(handle, store, upstream)
    assert state.outcome == "unsupported_contract" and len(calls) == 2
    assert state.rejected_list is None
    assert build_verification_report(handle, state)["rejected_list"] is None
