"""Source-pinned cloud diagnostics never refresh, discover, publish or mix credentials."""

import asyncio
import contextvars
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.models.db import CredentialSource, ProviderConfig
from app.services import claude_cloud_probe as probe
from app.services import claude_cloud_support as support
from app.services.token_cache import TokenCache

ACCOUNT = "holder@example.com"
SOURCE = "fixture-source"
ORG = "11111111-1111-4111-8111-111111111111"
SUBJECT = "22222222-2222-4222-8222-222222222222"


def request(**changes):
    values = {
        "account_id": ACCOUNT,
        "source_id": SOURCE,
        "organization_id": None,
        "session_id": None,
        "max_history_pages": 2,
        "report_version": 1,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def profile():
    return {"account": {"uuid": SUBJECT, "email": ACCOUNT}, "organization": {"uuid": ORG}}


def usage():
    return {
        "type": "assistant",
        "message": {
            "id": "private-message",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 1,
                "cache_read_input_tokens": 2,
                "cache_creation_input_tokens": 3,
            },
            "content": "private-transcript",
        },
    }


@pytest.fixture
async def environment(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'probe.db'}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(support, "Session", Session)
    monkeypatch.setattr(support, "engine", engine)
    monkeypatch.setattr(support, "gate", asyncio.Lock())
    # This fixture exercises a legacy file contribution, not durable cache reload.
    monkeypatch.setattr("app.services.source_controls._ENABLED", False)
    from cryptography.fernet import Fernet

    from app.core.config import settings
    from app.core.encryption import EncryptionService

    monkeypatch.setattr(settings, "DB_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(support, "encryption_service", EncryptionService())
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
    yield engine, cache
    engine.dispose()


async def run(body, upstream):
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        return await probe.probe_cloud_history(body, client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin,forwarded",
    [
        ("file:/fixture/.claude/.credentials.json", False),
        ("file:/fixture/.claude/.credentials.json", True),
        ("path:/fixture/.claude/.credentials.json", True),
        ("path:/fixture/.claude/oauth_creds.json", True),
        ("env:CLAUDE_CODE_OAUTH_TOKEN", True),
    ],
    ids=["original", "sidecar-read-only", "sidecar-path", "sidecar-oauth-path", "env-read-only"],
)
async def test_verified_exact_source_gets_bounded_history_without_leaking_evidence(
    environment, origin, forwarded
):
    if forwarded:
        from scripts.sidecar_pkg.source_consent import filter_egress

        entry = filter_egress(
            {
                "entries": [
                    {
                        "provider_id": "anthropic",
                        "credential_origin": origin,
                        "oauth_token": "fixture-access",
                        "refresh_token": "unused",
                    }
                ]
            }
        )["entries"][0]
        assert entry["oauth_read_only"] == "1" and "refresh_token" not in entry
        engine, cache = environment
        with Session(engine) as session:
            row = session.get(CredentialSource, 1)
            row.credential_origin = origin
            session.add(row)
            session.commit()
        await cache.remove_source("anthropic", ACCOUNT, SOURCE)
        await cache.store(
            "anthropic",
            {key: entry[key] for key in ("oauth_token", "oauth_read_only")},
            account_id=ACCOUNT,
            source_id=SOURCE,
            source_metadata={"credential_origin": entry["credential_origin"]},
        )
    calls = []

    def upstream(r):
        calls.append(r)
        assert r.method == "GET"
        assert r.headers["authorization"] == "Bearer fixture-access"
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        assert r.headers["x-organization-uuid"] == ORG
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200, json={"data": [{"sequence_num": "1", "data": usage()}], "next_cursor": None}
        )

    before = json.dumps(environment[1]._source_cache, default=str, sort_keys=True)
    report = await run(request(), upstream)
    assert len(calls) == 3
    assert report["outcome"] == "sample_complete"
    assert report["account_verified"] and report["organization_verified"]
    assert report["history_events"] == 1 and report["history_complete"] is True
    assert report["observations"][0]["location"] == "data"
    assert (
        report["observations"][0]["report"]["assistant"]["input_cache_observations"]["input_tokens"]
        == 10
    )
    assert report["ready_for_ingestion"] is False
    encoded = json.dumps(report)
    for value in (
        ACCOUNT,
        SOURCE,
        ORG,
        SUBJECT,
        "fixture-access",
        "unused",
        "session-fixture",
        "private-message",
        "private-transcript",
    ):
        assert value not in encoded
    assert json.dumps(environment[1]._source_cache, default=str, sort_keys=True) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,outcome",
    [
        (401, "rejected"),
        (403, "rejected"),
        (429, "rate_limited"),
        (500, "unavailable"),
        (302, "unavailable"),
    ],
)
async def test_failure_stops_after_one_request_without_refresh_or_fallback(
    environment, status, outcome
):
    calls = []

    def upstream(r):
        calls.append(r)
        return httpx.Response(
            status,
            json={"secret": "private-error"},  # pragma: allowlist secret - synthetic response
            headers={"Location": "https://other.invalid/"},
        )

    report = await run(request(), upstream)
    assert report["outcome"] == outcome and len(calls) == 1
    assert "private-error" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,outcome",
    [
        ("disabled", "source_ineligible"),
        ("archived", "source_ineligible"),
        ("desktop", "unsupported_source"),
        ("unknown_read_only", "unsupported_source"),
        ("unproven_read_only", "unsupported_source"),
        ("unrecognized_path", "unsupported_source"),
        ("legacy_keychain", "source_ineligible"),
        ("pending", "source_ineligible"),
        ("expired", "expired"),
    ],
)
async def test_ineligible_source_never_contacts_provider(environment, mutation, outcome):
    engine, cache = environment
    if mutation in ("disabled", "archived"):
        with Session(engine) as s:
            if mutation == "disabled":
                row = s.get(CredentialSource, 1)
                row.enabled = False
                s.add(row)
            else:
                s.add(ProviderConfig(provider_id="anthropic", account_id=ACCOUNT, archived=True))
            s.commit()
    else:
        tokens = {"oauth_token": "fixture-access"}
        metadata = {"credential_origin": "file:/fixture/.claude/.credentials.json"}
        if mutation in ("desktop", "unproven_read_only", "unrecognized_path"):
            tokens["oauth_read_only"] = "1"
            origin = {
                "desktop": "keychain:Claude Desktop/oauth/fixture",
                "unrecognized_path": "path:/fixture/.claude/unrelated.json",
                "unproven_read_only": "config:fixture",
            }[mutation]
            metadata["credential_origin"] = origin
            with Session(engine) as s:
                row = s.get(CredentialSource, 1)
                row.credential_origin = origin
                if mutation == "unproven_read_only":
                    row.source_type = "config"
                s.add(row)
                s.commit()
        if mutation == "unknown_read_only":
            tokens["oauth_read_only"] = "unsupported"
        if mutation == "legacy_keychain":
            metadata["credential_origin"] = "keychain:Claude Code-credentials"
        if mutation == "pending":
            metadata["identity_pending"] = True
        if mutation == "expired":
            tokens["expiry_date"] = "1"
            await cache.remove_source("anthropic", ACCOUNT, SOURCE)
        await cache.store(
            "anthropic", tokens, account_id=ACCOUNT, source_id=SOURCE, source_metadata=metadata
        )

    def upstream(r):
        pytest.fail("Ineligible source made a provider request")

    assert (await run(request(), upstream))["outcome"] == outcome


@pytest.mark.asyncio
async def test_missing_exact_source_does_not_use_another_or_merged_credential(environment):
    def upstream(r):
        pytest.fail("Missing source fell back")

    assert (await run(request(source_id="missing"), upstream))["outcome"] == "source_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["account", "organization", "malformed"])
async def test_identity_proof_failure_prevents_history_access(environment, failure):
    calls = []

    def upstream(r):
        calls.append(r)
        data = profile()
        if failure == "account":
            data["account"]["email"] = "other@example.com"
        if failure == "organization":
            data["organization"]["uuid"] = "invalid"
        if failure == "malformed":
            data = []
        return httpx.Response(200, json=data)

    report = await run(request(), upstream)
    assert report["outcome"] in ("identity_mismatch", "identity_unavailable")
    assert len(calls) == 1 and report["observations"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rotate", "disable", "remove"])
async def test_change_while_request_is_in_flight_discards_late_result(environment, change):
    engine, cache = environment
    calls = []

    async def upstream(r):
        calls.append(r)
        if change == "rotate":
            await asyncio.create_task(
                cache.store(
                    "anthropic",
                    {"oauth_token": "replacement"},
                    account_id=ACCOUNT,
                    source_id=SOURCE,
                    source_metadata={
                        "credential_origin": "file:/fixture/.claude/.credentials.json"
                    },
                ),
                context=contextvars.Context(),
            )
        elif change == "remove":
            await cache.remove_source("anthropic", ACCOUNT, SOURCE)
        else:
            with Session(engine) as s:
                row = s.get(CredentialSource, 1)
                row.enabled = False
                s.add(row)
                s.commit()
        return httpx.Response(200, json=profile())

    report = await run(request(), upstream)
    assert report["outcome"] == "source_changed" and len(calls) == 1
    assert not report["account_verified"] and report["observations"] == []


@pytest.mark.asyncio
async def test_cursor_loop_stops_and_reports_sample_as_incomplete(environment):
    calls = []

    def upstream(r):
        calls.append(r)
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(200, json={"data": [{"id": "fixture"}], "next_cursor": None})
        return httpx.Response(
            200, json={"data": [{"sequence_num": "2"}], "next_cursor": "private-cursor"}
        )

    report = await run(request(max_history_pages=3), upstream)
    assert len(calls) == 4 and report["outcome"] == "unsupported_contract"
    assert report["history_complete"] is False
    assert "private-cursor" not in json.dumps(report)


@pytest.mark.asyncio
async def test_history_budget_and_numeric_sequence_evidence_are_explicit(environment):
    def upstream(r):
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(200, json={"data": [{"id": "fixture"}], "next_cursor": "more"})
        return httpx.Response(
            200,
            json={"data": [{"sequence_num": n} for n in ("1", 3, 3, "bad")], "next_cursor": "more"},
        )

    report = await run(request(max_history_pages=1), upstream)
    assert report["outcome"] == "sample_limited" and report["session_list_complete"] is False
    assert report["history_complete"] is False and report["history_pages"] == 1
    assert report["sequence"] == {
        "numeric": 3,
        "invalid": 1,
        "duplicates": 1,
        "gaps": 1,
        "out_of_order": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"x" * (1024 * 1024 + 1), b'{"private":NaN}', b'{"data":[],"data":[]}'],
    ids=["oversized", "nonfinite-json", "duplicate-keys"],
)
async def test_oversized_or_ambiguous_responses_never_escape(environment, payload):
    report = await run(request(), lambda r: httpx.Response(200, content=payload))
    assert report["outcome"] in ("response_limit", "unsupported_contract")
    assert report["observations"] == []


@pytest.mark.asyncio
async def test_network_error_and_total_deadline_are_sanitized(environment, monkeypatch):
    def unavailable(r):
        raise httpx.ReadError("private-token-and-session")

    report = await run(request(), unavailable)
    assert report["outcome"] == "unavailable" and "private-token" not in json.dumps(report)
    monkeypatch.setattr(probe, "TOTAL_TIMEOUT", 0.01)

    async def slow(r):
        await asyncio.sleep(1)
        return httpx.Response(200, json=profile())

    assert (await run(request(), slow))["outcome"] == "timeout"


@pytest.mark.asyncio
async def test_concurrent_operator_probe_is_rejected_and_gate_recovers(environment):
    async with support.gate:
        report = await run(request(), lambda r: pytest.fail("Busy probe issued request"))
    assert report["outcome"] == "busy"
    assert not support.gate.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked",
    [None, "off", "paused", "revoked_device", "withdrawn", "old_revision", "bad_generation"],
)
async def test_cached_keychain_source_requires_current_device_consent(
    environment, monkeypatch, blocked
):
    await seed_keychain(environment, monkeypatch, blocked)
    calls = []

    def upstream(r):
        calls.append(r)
        if blocked is not None:
            pytest.fail("Revoked/ineligible consent made a provider request")
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    result = await run(request(), upstream)
    assert result["outcome"] == ("no_sessions" if blocked is None else "source_ineligible")
    assert len(calls) == (2 if blocked is None else 0)


@pytest.mark.asyncio
async def test_requested_organization_mismatch_stops_before_listing(environment):
    calls = []

    def upstream(r):
        calls.append(r)
        return httpx.Response(200, json=profile())

    result = await run(request(organization_id=SUBJECT), upstream)
    assert result["outcome"] == "identity_mismatch" and len(calls) == 1


@pytest.mark.asyncio
async def test_non_sample_session_is_never_requested(environment):
    calls = []

    def upstream(r):
        calls.append(r)
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": [{"id": "listed"}], "next_cursor": "more"})

    result = await run(request(session_id="not-listed"), upstream)
    assert result["outcome"] == "sample_not_found" and len(calls) == 2
    assert not result["history_complete"]


@pytest.mark.asyncio
async def test_missing_cursor_does_not_claim_exhaustion(environment):
    def upstream(r):
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(200, json={"data": []})

    result = await run(request(), upstream)
    assert result["outcome"] == "unsupported_contract"
    assert result["session_list_complete"] is None


@pytest.mark.asyncio
async def test_cancelled_probe_closes_stream_and_releases_admission(environment):
    started = asyncio.Event()

    class HangingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            await asyncio.sleep(10)
            yield b"{}"

        async def aclose(self):
            self.closed = True

    stream = HangingStream()
    task = asyncio.create_task(run(request(), lambda r: httpx.Response(200, stream=stream)))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert stream.closed and not support.gate.locked()


@pytest.mark.asyncio
async def test_configured_organization_binding_is_required(environment):
    engine, _ = environment
    with Session(engine) as session:
        session.add(
            ProviderConfig(
                provider_id="anthropic", account_id=ACCOUNT, claude_organization_id=SUBJECT
            )
        )
        session.commit()
    calls = []

    def upstream(r):
        calls.append(r)
        return httpx.Response(200, json=profile())

    report = await run(request(), upstream)
    assert report["outcome"] == "identity_mismatch" and len(calls) == 1


@pytest.mark.asyncio
async def test_database_read_failure_is_sanitized_and_cannot_publish(environment):
    engine, _ = environment
    CredentialSource.__table__.drop(engine)
    report = await run(
        request(), lambda r: pytest.fail("Unavailable authority database issued request")
    )
    assert report["outcome"] == "source_unavailable" and report["requests"] == 0
    assert report["observations"] == []


async def seed_keychain(environment, monkeypatch, blocked=None):
    from app.models.db import SidecarDevice, SidecarSourceContribution, SidecarSourceState
    from app.services import source_controls
    from scripts.sidecar_pkg.source_catalog import control_catalog

    engine, cache = environment
    monkeypatch.setattr(source_controls, "Session", Session)
    scraper = "anthropic.claude-code"
    origin = "keychain:Claude Code-credentials"
    with Session(engine) as session:
        row = session.get(CredentialSource, 1)
        row.credential_origin = origin
        session.add(row)
        session.add(
            SidecarDevice(
                device_id="fixture-device",
                secret_encrypted="opaque",  # pragma: allowlist secret - inert fixture
                revoked=blocked == "revoked_device",
            )
        )
        session.add(
            SidecarSourceState(
                device_id="fixture-device",
                source_id=scraper,
                provider_id="anthropic",
                label="Fixture",
                revision="old"
                if blocked == "old_revision"
                else control_catalog()[scraper].revision,
                generation=1,
                local_read=blocked != "off",
                remote_paused=blocked == "paused",
                revoked_through=1 if blocked == "withdrawn" else -1,
            )
        )
        session.add(
            SidecarSourceContribution(
                device_id="fixture-device",
                scraper_id=scraper,
                provider_id="anthropic",
                credential_source_id=SOURCE,
                origin=origin,
                account_id=ACCOUNT,
                generation=1,
                payload_encrypted=support.encryption_service.encrypt_json(
                    {"tokens": {"oauth_token": "fixture-access", "oauth_read_only": "1"}}
                ),
            )
        )
        session.commit()
    await cache.remove_source("anthropic", ACCOUNT, SOURCE)
    await cache.store(
        "anthropic",
        {"oauth_token": "fixture-access", "oauth_read_only": "1"},
        account_id=ACCOUNT,
        source_id=SOURCE,
        source_metadata={
            "credential_origin": origin,
            "device_id": "fixture-device",
            "scraper_id": scraper,
            "durable_source": True,
            "consent_generation": True if blocked == "bad_generation" else 1,
        },
    )


@pytest.mark.asyncio
async def test_reenable_with_identical_token_invalidates_old_generation(environment, monkeypatch):
    from app.models.db import SidecarSourceContribution, SidecarSourceState

    await seed_keychain(environment, monkeypatch)
    engine, cache = environment
    calls = []

    def upstream(r):
        calls.append(r)
        with Session(engine) as session:
            state = session.get(SidecarSourceState, 1)
            state.generation = 2
            state.revoked_through = 1
            contribution = session.get(SidecarSourceContribution, 1)
            contribution.generation = 2
            session.add(state)
            session.add(contribution)
            session.commit()
        tokens, metadata, ts = cache._source_cache["anthropic"][ACCOUNT][SOURCE]
        cache._source_cache["anthropic"][ACCOUNT][SOURCE] = (
            tokens,
            {**metadata, "consent_generation": 2},
            ts,
        )
        return httpx.Response(200, json=profile())

    report = await run(request(), upstream)
    assert report["outcome"] == "source_changed" and len(calls) == 1
    assert not report["account_verified"] and report["observations"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_blocked_authority_thread_cannot_defeat_deadline_or_publish_late(
    environment, monkeypatch, version
):
    import threading

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original = support.authority_read

    def slow(*args):
        started.set()
        release.wait(2)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(support, "authority_read", slow)
    monkeypatch.setattr(probe, "TOTAL_TIMEOUT", 0.03)
    try:
        result = await asyncio.wait_for(
            run(
                request(report_version=version),
                lambda r: pytest.fail("Late authority work made a request"),
            ),
            0.3,
        )
        assert started.is_set() and result["outcome"] == "timeout"
        assert result["observations"] == [] and not support.gate.locked()
        assert result["format_version"] == version
        if version == 2:
            assert result["structure"]["events"] == 0
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("withdraw", ["device", "payload"])
async def test_enrolled_file_contribution_is_not_a_device_revocation_bypass(
    environment, monkeypatch, withdraw
):
    from app.models.db import SidecarDevice, SidecarSourceContribution

    await seed_keychain(environment, monkeypatch)
    engine, cache = environment
    origin = "file:/fixture/.claude/.credentials.json"
    with Session(engine) as session:
        source = session.get(CredentialSource, 1)
        source.credential_origin = origin
        contribution = session.get(SidecarSourceContribution, 1)
        contribution.origin = origin
        contribution.scraper_id = None
        contribution.generation = 0
        if withdraw == "payload":
            contribution.payload_encrypted = None
        else:
            device = session.get(SidecarDevice, "fixture-device")
            device.revoked = True
            session.add(device)
        session.add(source)
        session.add(contribution)
        session.commit()
    tokens, metadata, ts = cache._source_cache["anthropic"][ACCOUNT][SOURCE]
    cache._source_cache["anthropic"][ACCOUNT][SOURCE] = (
        tokens,
        {**metadata, "credential_origin": origin, "scraper_id": None, "consent_generation": 0},
        ts,
    )
    result = await run(request(), lambda r: pytest.fail("Revoked file contribution was used"))
    assert result["outcome"] == "source_ineligible" and result["requests"] == 0


@pytest.mark.asyncio
async def test_selected_session_uses_only_a_validated_listed_identifier(environment):
    paths = []

    def upstream(r):
        paths.append(r.url.path)
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "first"}, {"id": "second"}], "next_cursor": None}
            )
        assert r.url.path == "/v1/code/sessions/second/events"
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    result = await run(request(session_id="second"), upstream)
    assert result["outcome"] == "sample_complete" and len(paths) == 3


@pytest.mark.asyncio
async def test_unsafe_provider_session_id_never_becomes_a_request_path(environment):
    paths = []

    def upstream(r):
        paths.append(r.url.path)
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        return httpx.Response(
            200, json={"data": [{"id": "../../other?private"}], "next_cursor": None}
        )

    result = await run(request(), upstream)
    assert result["outcome"] == "unsupported_contract" and len(paths) == 2


@pytest.mark.asyncio
async def test_history_sink_rejects_unsafe_identifier_even_for_internal_callers(environment):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("Unsafe path reached HTTP"))
    ) as client:
        attempt = probe.Probe(request(), client)
        with pytest.raises(probe.ProbeError, match="^unsupported_contract$"):
            await attempt.history("../other?private", {})


@pytest.mark.asyncio
async def test_v2_reports_only_fixed_structure_without_decoding_strings(environment):
    def upstream(r):
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "sequence_num": "1",
                        "type": "private-type",
                        "private-key": "private-value",
                        "payload": {"type": "user", "content": "private-transcript"},
                        "content": '{"type":"assistant","private":"private-transcript"}',
                    }
                ],
                "next_cursor": None,
            },
        )

    report = await run(request(report_version=2), upstream)
    assert report["format_version"] == 2 and report["requests"] == 3
    assert report["structure"]["events"] == report["history_events"] == 1
    rows = report["structure"]["locations"]
    assert rows["event"]["categories"]["other"] == 1
    assert rows["payload"]["categories"]["user"] == 1
    assert rows["content"]["types"]["string"] == 1
    assert report["observations"] == []
    assert all(
        secret not in json.dumps(report)
        for secret in ("private-type", "private-key", "private-value", "private-transcript")
    )


@pytest.mark.asyncio
async def test_v2_source_change_discards_prior_structural_observations(environment):
    _, cache = environment
    history_calls = 0

    def upstream(r):
        nonlocal history_calls
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        history_calls += 1
        if history_calls == 2:
            cache._source_cache.clear()
        return httpx.Response(
            200,
            json={
                "data": [{"sequence_num": str(history_calls), "data": usage()}],
                "next_cursor": "next",
            },
        )

    report = await run(request(report_version=2), upstream)
    assert history_calls == 2 and report["outcome"] == "source_changed"
    assert report["format_version"] == 2
    assert report["history_events"] == report["structure"]["events"] == 0
    assert report["observations"] == [] and report["account_verified"] is False
    assert all(not any(row["types"].values()) for row in report["structure"]["locations"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_payload_usage_is_an_explicit_v2_location_with_independent_accounting(
    environment, version
):
    first = usage()
    duplicate = usage()
    duplicate["message"]["usage"]["output_tokens"] = 7
    snapshot = {
        "type": "result",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 50,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 6,
        },
        "modelUsage": {
            "private-model": {
                "inputTokens": 20,
                "outputTokens": 50,
                "cacheReadInputTokens": 4,
                "cacheCreationInputTokens": 6,
            }
        },
    }
    smaller = {
        "type": "result",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    payloads = [first, duplicate, snapshot, smaller]

    def upstream(r):
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"sequence_num": index, "payload": value}
                    for index, value in enumerate(payloads)
                ],
                "next_cursor": None,
            },
        )

    report = await run(request(report_version=version), upstream)
    if version == 1:
        assert report["observations"] == []
        return
    assert [row["location"] for row in report["observations"]] == ["payload"]
    audit = report["observations"][0]["report"]
    assert audit["assistant"]["records"] == 2
    assert audit["assistant"]["unique_messages"] == 1
    assert audit["assistant"]["duplicate_messages"] == 1
    assert audit["assistant"]["input_cache_observations"] == {
        "input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 3,
    }
    assert "output_placeholder_divergence" in audit["diagnostics"]
    assert audit["result"]["usage_observations"] == 2
    assert audit["result"]["model_usage_observations"] == 1
    assert audit["result"]["usage_decreases"] == 1
    assert audit["result"]["last_usage_observation"]["output_tokens"] == 2
    assert audit["result"]["last_model_usage_observation"] is None
    assert audit["ready_for_ingestion"] is report["cloud_contract_verified"] is False
    for secret in ("private-model", "private-message", "private-transcript"):
        assert secret not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [True, -1, 1.5, "4", None, 2**63])
async def test_payload_malformed_usage_is_not_rescued_by_another_location(environment, bad):
    invalid = usage()
    invalid["message"]["usage"]["input_tokens"] = bad

    def upstream(r):
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200,
            json={
                "data": [{"sequence_num": 1, "data": usage(), "payload": invalid}],
                "next_cursor": None,
            },
        )

    report = await run(request(report_version=2), upstream)
    assert [row["location"] for row in report["observations"]] == ["data", "payload"]
    good, bad_audit = [row["report"] for row in report["observations"]]
    assert good["assistant"]["input_cache_observations"]["input_tokens"] == 10
    assert bad_audit["assistant"]["input_cache_observations"] is None
    assert "invalid_assistant_usage" in bad_audit["diagnostics"]


@pytest.mark.asyncio
async def test_payload_preserves_conflicts_subagents_and_error_results_without_decoding(
    environment,
):
    conflict = usage()
    conflict["message"]["usage"]["input_tokens"] = 99
    subagent = {**usage(), "parent_tool_use_id": "private-parent"}
    payloads = [
        usage(),
        conflict,
        subagent,
        {"type": "result", "subtype": "error_during_execution", "usage": None},
        json.dumps(usage()),
        [usage()],
        {"private-dynamic-key": "private-content"},
    ]

    def upstream(r):
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": "session-fixture"}], "next_cursor": None}
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"sequence_num": index, "payload": value}
                    for index, value in enumerate(payloads)
                ],
                "next_cursor": None,
            },
        )

    report = await run(request(report_version=2), upstream)
    assert len(report["observations"]) == 1
    audit = report["observations"][0]["report"]
    assert audit["assistant"]["records"] == 3
    assert audit["assistant"]["conflicting_messages"] == 1
    assert audit["assistant"]["subagent_records"] == 1
    assert audit["assistant"]["input_cache_observations"] is None
    assert audit["result"]["records"] == audit["result"]["error_results"] == 1
    assert audit["result"]["last_usage_observation"] is None
    assert {
        "conflicting_assistant_usage",
        "subagent_scope_excluded",
        "error_result_observed",
    } <= set(audit["diagnostics"])
    from app.models.claude_cloud_probe import ClaudeCloudProbeReportV2
    from scripts.verify_claude_cloud import evaluate

    validated = ClaudeCloudProbeReportV2.model_validate(report).model_dump()
    evidence = {
        "format_version": 1,
        "source_commit": "a" * 40,
        "image": "ghcr.io/s3ntin3l8/runway@sha256:" + "b" * 64,
        "observed_at": "2026-10-25T00:00:00Z",
        "deployment": "selected-production",
        "report": validated,
    }
    result = evaluate(evidence)
    assert result["status"] == "stop" and "usage_requires_review" in result["reasons"]
    encoded = json.dumps(result)
    for secret in (
        "private-parent",
        "private-message",
        "private-transcript",
        "private-dynamic-key",
        "private-content",
    ):
        assert secret not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,listed,requested",
    [
        ("session_abc", ["first", "cse_abc"], "cse_abc"),
        ("cse_abc", ["first", "abc"], "abc"),
        # An exact listed id wins over a prefix-equivalent one listed earlier.
        ("abc", ["cse_abc", "abc"], "abc"),
    ],
)
async def test_sample_target_matches_prefix_equivalent_ids_exact_first(
    environment, target, listed, requested
):
    paths = []

    def upstream(r):
        paths.append(r.url.path)
        if r.url.path.endswith("/profile"):
            return httpx.Response(200, json=profile())
        if r.url.path == "/v1/code/sessions":
            return httpx.Response(
                200, json={"data": [{"id": sid} for sid in listed], "next_cursor": None}
            )
        assert r.url.path == f"/v1/code/sessions/{requested}/events"
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    result = await run(request(session_id=target), upstream)
    assert result["outcome"] == "sample_complete" and len(paths) == 3
