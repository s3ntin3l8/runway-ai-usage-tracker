"""Explicit, resumable verification; never a background collector or refresher."""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any, Literal, cast, get_args
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter
from sqlalchemy.exc import SQLAlchemyError

from app.models.claude_cloud_list_observation import RejectedListEvidence
from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.models.claude_cloud_verification import (
    CheckpointRecord,
    VerificationOutcome,
    VerificationStart,
    VerificationState,
)
from app.services import claude_cloud_probe as probe
from app.services.claude_cloud_checkpoints import CheckpointError, CheckpointStore, Claim
from app.services.claude_cloud_ledger import (
    compare_subagent_records,
    compare_usage_records,
    usage_only_record,
    usage_timestamp,
)
from scripts.audit_claude_usage import audit_messages
from scripts.claude_cloud_list_boundary import (
    BoundaryDecodeError,
    decode_boundary_json,
    observe_list_boundary,
)
from scripts.claude_cloud_list_boundary import (
    job_digest as _digest,
)

_RECORD: TypeAdapter[CheckpointRecord] = TypeAdapter(CheckpointRecord)
_ENVIRONMENT_KIND = re.compile(r"[a-z][a-z0-9_]{0,31}")
# The environment kinds this verifier recognizes; any other label is reported as "other".
_KNOWN_KINDS: dict[str, Literal["anthropic_cloud", "bridge"]] = {
    "anthropic_cloud": "anthropic_cloud",
    "bridge": "bridge",
}

MAX_REQUESTS = 512
MAX_BYTES = 256 * 1024 * 1024
UNSAFE_OUTCOMES = {
    "source_changed",
    "source_unavailable",
    "identity_mismatch",
    "identity_unavailable",
}


def _verification_page(payload: object, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    """Follow supplied continuation, including optional terminal omission.

    Page length never signals completion. A declared has_more must agree with
    continuation; the legacy sample parser and diagnostic-v1 policy stay frozen.
    """
    if isinstance(payload, dict):
        if "has_more" in payload and type(payload["has_more"]) is not bool:
            raise probe.ProbeError("unsupported_contract")
        if "next_cursor" not in payload:
            payload = {**payload, "next_cursor": None}
    rows, cursor = probe._page(payload, limit)
    if (
        isinstance(payload, dict)
        and "has_more" in payload
        and payload["has_more"] != (cursor is not None)
    ):
        raise probe.ProbeError("unsupported_contract")
    return rows, cursor


def _source_digest(key: bytes, source: tuple[Any, ...]) -> str:
    return _digest(key, b"source", source)


async def start_verification(body: VerificationStart, store: CheckpointStore) -> str:
    """Record intent with one eligible source. This performs no provider requests."""
    if not store.crypto.is_enabled:
        raise CheckpointError("encryption_required")
    if await asyncio.to_thread(store.current_handle) is not None:
        raise CheckpointError("job_exists")
    account = probe.canonical_account_id(body.account_id)
    source = await probe._load_source(account, body.source_id)
    key = bytes.fromhex(body.job_key) if body.job_key is not None else secrets.token_bytes(32)
    created = store.clock()
    state = VerificationState(
        account_id=account,
        source_id=body.source_id,
        job_key=key.hex(),
        source_digest=_source_digest(key, source),
        created_at=created,
        deadline=created + 1800,
        organization=body.organization_id,
        target_session=body.session_id,
        expected_local=body.expected_local,
        declared_category=body.declared_category,
    )
    return await asyncio.to_thread(store.create, state)


async def read_verification(handle: str, store: CheckpointStore) -> VerificationState:
    """Read committed state and local authority only; never contact the provider.

    A status read suppresses stale evidence without mutating a concurrently leased
    checkpoint. The next explicit step durably stops a changed-source job.
    """
    state = await asyncio.to_thread(store.snapshot, handle)
    try:
        source = await probe._load_source(state.account_id, state.source_id)
        if _source_digest(bytes.fromhex(state.job_key), source) == state.source_digest:
            return state
    except (probe.ProbeError, SQLAlchemyError):
        # Authority is unavailable: suppress the projection below, without writing a lease.
        pass
    return state.model_copy(
        update={
            "phase": "stopped",
            "outcome": "source_changed",
            "usage_records": [],
            "rejected_list": None,
        }
    )


class _VerificationProbe(probe._Probe):
    def __init__(self, claim: Claim, store: CheckpointStore, client: httpx.AsyncClient):
        self.claim = claim
        self.store = store
        self.key = bytes.fromhex(claim.state.job_key)
        self.start_bytes = claim.state.total_bytes
        self.profile_ids: tuple[str | None, str | None] = (None, None)
        self.inspect_list_response = False
        self.list_metadata: dict[str, Any] = {}
        request = ClaudeCloudProbeRequest(
            account_id=claim.state.account_id,
            source_id=claim.state.source_id,
            organization_id=claim.state.organization,
            report_version=2,
        )
        super().__init__(
            request,
            client,
            total_byte_limit=min(probe.MAX_TOTAL_BYTES, MAX_BYTES - self.start_bytes),
        )

    @property
    def state(self) -> VerificationState:
        # Store.save replaces the object after validation. Never keep a stale alias.
        return self.claim.state

    async def get(
        self, path: str, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> Any:
        if self.state.in_flight:
            raise CheckpointError("interrupted")
        if (
            self.state.requests >= MAX_REQUESTS
            or self.report["requests"] >= 5
            or self.state.total_bytes >= MAX_BYTES
        ):
            raise probe.ProbeError("limit_reached")
        if not self.state.created_at <= self.store.clock() < self.state.deadline:
            raise probe.ProbeError("expired")
        await self.check_source()
        self.state.requests += 1
        self.state.in_flight = True
        await asyncio.to_thread(self.store.save, self.claim)
        value = await super().get(path, headers, params)
        if path == "/api/oauth/profile" and isinstance(value, dict):
            holder, org = value.get("account"), value.get("organization")
            self.profile_ids = (
                probe._uuid(holder.get("uuid")) if isinstance(holder, dict) else None,
                probe._uuid(org.get("uuid")) if isinstance(org, dict) else None,
            )
        return value

    async def commit_response(self) -> None:
        await self.check_source()
        self.state.total_bytes = self.start_bytes + self.total_bytes
        self.state.last_http_status = self.report["http_status"]
        self.state.in_flight = False
        await asyncio.to_thread(self.store.save, self.claim)

    def _cursor(self, cursor: str | None, phase: str) -> None:
        if cursor is not None:
            digest = _digest(self.key, b"cursor", [phase, cursor])
            if digest in self.state.seen_cursors:
                raise probe.ProbeError("unsupported_contract")
            self.state.seen_cursors.append(digest)
        self.state.next_cursor = cursor

    def _response_encoding(self, response: httpx.Response) -> str:
        encoding = super()._response_encoding(response)
        if self.inspect_list_response:
            self.list_metadata["response_encoding"] = (
                "identity" if encoding == "identity" else "unsupported"
            )
        return encoding

    def _decode(self, body: bytearray) -> Any:
        if not self.inspect_list_response:
            return super()._decode(body)
        try:
            # Keep the existing worker decoder's acceptance semantics unchanged.
            payload = super()._decode(body)
        except (probe.ProbeError, ValueError, RecursionError):
            try:
                decode_boundary_json(body)
            except BoundaryDecodeError as exc:
                self.list_metadata.update(
                    json_state=exc.category,
                    duplicate_json_keys=exc.duplicates,
                    nonfinite_json_values=exc.nonfinite,
                )
            else:
                # A future decoder disagreement provides no established failure category.
                self.list_metadata.clear()
            raise probe.ProbeError("unsupported_contract") from None
        self.list_metadata.update(
            json_state="decoded",
            boundary=observe_list_boundary(
                payload, self.key, set(self.state.seen_sessions), set(self.state.seen_cursors)
            ),
        )
        return payload

    async def list_page(self, headers: dict[str, str]) -> None:
        self.inspect_list_response = True
        self.list_metadata = {}
        try:
            params: dict[str, Any] = {"limit": 20}
            if self.state.next_cursor is not None:
                params["cursor"] = self.state.next_cursor
            rows, cursor = _verification_page(
                await self.get("/v1/code/sessions", headers, params), 20
            )
            ids = [row.get("id") for row in rows]
            if any(not isinstance(sid, str) or not probe._SESSION_ID.fullmatch(sid) for sid in ids):
                raise probe.ProbeError("unsupported_contract")
            digests = [_digest(self.key, b"listed-session", sid) for sid in ids]
            if len(set(digests)) != len(digests) or set(digests).intersection(
                self.state.seen_sessions
            ):
                raise probe.ProbeError("unsupported_contract")
            if self.state.listed_sessions + len(rows) > 20000:
                raise probe.ProbeError("limit_reached")
            self._cursor(cursor, "listing")
            self.state.seen_sessions.extend(digests)
            self.state.listed_sessions += len(rows)
            self.state.list_pages += 1
            for row, sid in zip(rows, ids, strict=True):
                target = self.state.target_session
                exact = target is None or sid == target
                # An exact listed id wins over a prefix-equivalent one wherever it sits in
                # the list, so an equivalent is held back until the list is exhausted.
                equivalent = (
                    not exact
                    and probe.session_key(cast(str, target)) == probe.session_key(cast(str, sid))
                    and self.state.equivalent_session is None
                )
                if self.state.selected_session is not None or not (exact or equivalent):
                    continue
                if exact:
                    self.state.selected_session = cast(str, sid)
                else:
                    self.state.equivalent_session = cast(str, sid)
                environment = row.get("environment")
                kind = environment.get("kind") if isinstance(environment, dict) else None
                if isinstance(kind, str) and _ENVIRONMENT_KIND.fullmatch(kind):
                    # An unrecognized label is observed, never mapped onto a known kind.
                    self.state.execution_kind = _KNOWN_KINDS.get(kind, "other")
                    self.state.environment_kind = kind
            if cursor is None:
                self.state.list_complete = True
                if self.state.selected_session is None:
                    self.state.selected_session = self.state.equivalent_session
                if self.state.selected_session is None:
                    self.state.phase = "complete"
                    self.state.outcome = (
                        "target_not_found" if self.state.target_session else "no_sessions"
                    )
                else:
                    self.state.phase = "history"
            await self.commit_response()
        except probe.ProbeError as exc:
            if str(exc) == "unsupported_contract" and self.list_metadata:
                await self.check_source()
                self.state.rejected_list = RejectedListEvidence.model_validate(self.list_metadata)
            raise
        finally:
            self.inspect_list_response = False

    def _sequence(self, event: dict[str, Any]) -> int | None:
        raw = event.get("sequence_num")
        value = None
        if type(raw) is int and 0 <= raw < 2**63:
            value = raw
        elif isinstance(raw, str) and re.fullmatch("[0-9]{1,19}", raw) and int(raw) < 2**63:
            value = int(raw)
        if value is None:
            self.state.sequence.invalid += 1
            return None
        self.state.sequence.numeric += 1
        previous = self.state.previous_sequence
        if value in self.state.sequences:
            self.state.sequence.duplicates += 1
        else:
            self.state.sequences.append(value)
            if previous is not None:
                self.state.sequence.gaps += value > previous + 1
                self.state.sequence.out_of_order += value < previous
        self.state.previous_sequence = value
        return value

    async def history_page(self, headers: dict[str, str]) -> None:
        sid = self.state.selected_session
        if sid is None or not probe._SESSION_ID.fullmatch(sid):
            raise probe.ProbeError("unsupported_contract")
        params: dict[str, Any] = {"limit": 50, "sort_order": "asc"}
        if self.state.next_cursor is not None:
            params["cursor"] = self.state.next_cursor
        rows, cursor = _verification_page(
            await self.get(f"/v1/code/sessions/{quote(sid, safe='')}/events", headers, params), 50
        )
        if self.state.history_events + len(rows) > 100000:
            raise probe.ProbeError("limit_reached")
        self._cursor(cursor, "history")
        self.state.history_pages += 1
        self.state.history_events += len(rows)
        for row in rows:
            event_sequence = self._sequence(row)
            reduced = usage_only_record(row.get("payload"), self.key)
            if reduced is not None:
                reduced["event_sequence"] = event_sequence
                if row.get("created_at") is not None:
                    timestamp = usage_timestamp(row["created_at"])
                    if timestamp is None:
                        reduced["provider_timestamp_unknown"] = True
                    else:
                        reduced["provider_timestamp"] = timestamp
                self.state.usage_records.append(_RECORD.validate_python(reduced))
        if cursor is None:
            self.state.history_complete = True
            self.state.phase = "complete"
            self.state.outcome = "complete"
        await self.commit_response()


async def _stop(
    claim: Claim, store: CheckpointStore, outcome: str, attempt: _VerificationProbe | None
) -> VerificationState:
    reason = outcome if outcome in get_args(VerificationOutcome) else "unavailable"
    claim.state.phase = "stopped"
    claim.state.outcome = cast(VerificationOutcome, reason)
    if attempt is not None:
        claim.state.last_http_status = attempt.report["http_status"]
    if reason in UNSAFE_OUTCOMES:
        claim.state.usage_records = []
    await asyncio.to_thread(store.save, claim)
    return await asyncio.to_thread(store.snapshot, claim.handle)


async def advance_verification(
    handle: str, store: CheckpointStore, client: httpx.AsyncClient
) -> VerificationState:
    """One bounded batch. A terminal/interrupted job cannot issue another GET."""
    if probe._gate.locked():
        raise CheckpointError("busy")
    async with probe._gate:
        try:
            claim = await asyncio.to_thread(store.claim, handle)
        except CheckpointError as exc:
            if str(exc) in ("terminal", "interrupted", "expired"):
                return await asyncio.to_thread(store.snapshot, handle)
            raise
        attempt = None
        try:
            if claim.state.requests >= MAX_REQUESTS or claim.state.total_bytes >= MAX_BYTES:
                return await _stop(claim, store, "limit_reached", None)
            with probe.probing():
                async with asyncio.timeout(
                    min(probe.TOTAL_TIMEOUT, max(0, claim.state.deadline - store.clock()))
                ):
                    try:
                        source = await probe._load_source(
                            claim.state.account_id, claim.state.source_id
                        )
                    except probe.ProbeError:
                        raise probe.ProbeError("source_changed") from None
                    if (
                        _source_digest(bytes.fromhex(claim.state.job_key), source)
                        != claim.state.source_digest
                    ):
                        raise probe.ProbeError("source_changed")
                    attempt = _VerificationProbe(claim, store, client)
                    attempt.tokens, attempt.expected_org, attempt.authority = source
                    async with probe.token_cache.using_source(
                        "anthropic", attempt.account, claim.state.source_id
                    ):
                        headers = await attempt.identity()
                        subject, org = attempt.profile_ids
                        if (
                            subject is None
                            or org is None
                            or (claim.state.subject is not None and claim.state.subject != subject)
                        ):
                            raise probe.ProbeError("identity_mismatch")
                        if not claim.state.created_at <= store.clock() < claim.state.deadline:
                            raise probe.ProbeError("expired")
                        claim.state.subject = subject
                        claim.state.organization = org
                        claim.state.last_verified_at = store.clock()
                        await attempt.commit_response()
                        while (
                            claim.state.phase in ("listing", "history")
                            and attempt.report["requests"] < 5
                        ):
                            if claim.state.phase == "listing":
                                await attempt.list_page(headers)
                            else:
                                await attempt.history_page(headers)
                        await attempt.check_source()
                        if claim.state.phase in ("listing", "history") and (
                            claim.state.requests >= MAX_REQUESTS
                            or claim.state.total_bytes >= MAX_BYTES
                        ):
                            return await _stop(claim, store, "limit_reached", attempt)
                        return await asyncio.to_thread(store.snapshot, handle)
        except CheckpointError:
            # Never retry a failed intent/response write or publish uncommitted state.
            raise
        except probe.ProbeError as exc:
            return await _stop(claim, store, str(exc), attempt)
        except (TimeoutError, httpx.TimeoutException):
            return await _stop(claim, store, "timeout", attempt)
        except SQLAlchemyError:
            return await _stop(claim, store, "source_unavailable", attempt)
        except httpx.HTTPError:
            return await _stop(claim, store, "unavailable", attempt)
        except (ValueError, RecursionError):
            return await _stop(claim, store, "unsupported_contract", attempt)
        finally:
            await asyncio.shield(asyncio.to_thread(store.release, claim))


def _scope_summary(local: list[dict[str, Any]], records: list[dict[str, Any]]) -> dict[str, int]:
    def values(rows: list[dict[str, Any]], name: str) -> set[str]:
        return {row[name] for row in rows if isinstance(row.get(name), str)}

    sessions, parents = values(records, "session_scope"), values(records, "parent_tool_use_id")
    versions = values(records, "record_version")
    previous = None
    changes = 0
    for row in records:
        version = row.get("record_version")
        if isinstance(version, str):
            changes += previous is not None and previous != version
            previous = version
    return {
        "initializations": sum(
            row["type"] == "system" and row.get("subtype") == "init" for row in records
        ),
        "session_scopes": len(sessions),
        "parent_scopes": len(parents),
        "unknown_parent_records": sum(row.get("parent_tool_use_id") is True for row in records),
        "request_scopes": len(values(records, "request_scope")),
        "record_versions": len(versions),
        "record_version_changes": changes,
        "scope_conflicts": sum(row.get("scope_conflict") is True for row in records),
        "unknown_scope_records": sum(row.get("scope_unknown") is True for row in records),
        "shared_session_scopes": len(sessions & values(local, "session_scope")),
        "shared_parent_scopes": len(parents & values(local, "parent_tool_use_id")),
    }


def build_verification_report(handle: str, state: VerificationState) -> dict[str, Any]:
    """Fixed public projection. Pagination and input/cache matches are not acceptance."""
    safe = state.outcome not in UNSAFE_OUTCOMES
    records = [row.model_dump() for row in state.usage_records] if safe else []
    local = [row.model_dump() for row in state.expected_local] if safe else []
    return {
        "format_version": 1,
        "job_handle": handle,
        "rejected_list": state.rejected_list.model_dump() if safe and state.rejected_list else None,
        "started_at": state.created_at,
        "deadline": state.deadline,
        "last_verified_at": state.last_verified_at if safe else None,
        "phase": state.phase,
        "outcome": state.outcome,
        "requests": state.requests,
        "accepted_response_bytes": state.total_bytes,
        "http_status": state.last_http_status,
        "account_verified": safe and state.subject is not None,
        "organization_verified": safe
        and state.organization is not None
        and state.subject is not None,
        "listed_sessions": state.listed_sessions if safe else 0,
        "list_pages": state.list_pages if safe else 0,
        "list_complete": state.list_complete if safe else False,
        "history_pages": state.history_pages if safe else 0,
        "history_events": state.history_events if safe else 0,
        "history_complete": state.history_complete if safe else False,
        "sequence": state.sequence.model_dump()
        if safe
        else dict.fromkeys(("numeric", "invalid", "duplicates", "gaps", "out_of_order"), 0),
        "audit": audit_messages(records),
        "comparison": compare_usage_records(local, records),
        "subagent_comparison": compare_subagent_records(local, records),
        "scope_observations": _scope_summary(local, records),
        "declared_category": state.declared_category,
        "execution_kind": state.execution_kind if safe else "unknown",
        # A checkpoint from a build before the label existed recorded only a known kind.
        "environment_kind": (
            state.environment_kind
            or (state.execution_kind if state.execution_kind in _KNOWN_KINDS else None)
        )
        if safe
        else None,
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
        "live_acceptance_passed": False,
        "provenance_verified": False,
    }
