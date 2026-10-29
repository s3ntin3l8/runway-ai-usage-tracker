"""One explicit, read-only cloud-history sample from an eligible cached source.

No discovery, refresh, promotion, credential/usage persistence or provider logging.
The cloud envelope is unverified: SDK-shaped objects are audited per location.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, cast
from urllib.parse import quote

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.core.utils import IdentityExtractor
from app.models.claude_cloud_probe import ClaudeCloudProbeRequest
from app.services import claude_cloud_support as support
from app.services.account_identity import canonical_account_id
from app.services.claude_cloud_support import ProbeError
from app.services.probe_mode import probing
from app.services.token_cache import token_cache
from scripts.audit_claude_usage import audit_messages
from scripts.claude_cloud_list_boundary import SESSION_ID
from scripts.claude_cloud_structure import empty_structure, observe_structure


def session_key(sid: str) -> str:
    """The id with a ``cse_`` or ``session_`` prefix removed.

    The list API returns ``cse_...`` while claude.ai shows ``session_...`` for the same
    session. The primary client strips either prefix before comparing; this is observed
    behaviour, not an API guarantee, so it is used only to match an operator-supplied target.
    """
    for prefix in ("cse_", "session_"):
        if sid.startswith(prefix):
            return sid[len(prefix) :]
    return sid


BASE_URL = "https://api.anthropic.com"
TOTAL_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
OBSERVATION_LOCATIONS = {
    1: ("event", "data", "content"),
    2: ("event", "data", "content", "payload"),
}


def empty_report(report_version: int = 1) -> dict[str, Any]:
    report: dict[str, Any] = {
        "format_version": report_version,
        "outcome": "source_unavailable",
        "stage": "source",
        "requests": 0,
        "http_status": None,
        "account_verified": False,
        "organization_verified": False,
        "session_list_records": 0,
        "session_list_complete": None,
        "history_pages": 0,
        "history_events": 0,
        "history_complete": None,
        "sequence": dict.fromkeys(("numeric", "invalid", "duplicates", "gaps", "out_of_order"), 0),
        "observations": [],
        "cloud_contract_verified": False,
        "ready_for_ingestion": False,
    }

    if report_version == 2:
        report["structure"] = empty_structure()
    return report


async def load_source(
    account: str, source_id: str
) -> tuple[dict[str, str], str | None, tuple[Any, ...]]:
    if account == "default":
        raise ProbeError("source_ineligible")
    snapshot = await token_cache.peek_source("anthropic", account, source_id)
    if snapshot is None:
        raise ProbeError("source_unavailable")
    tokens, candidate = snapshot
    # Syntax only: the authority read separately requires Code provenance for "1".
    if tokens.get("oauth_read_only") not in (None, "", "0", "1"):
        raise ProbeError("unsupported_source")
    if candidate.get("identity_pending") or not tokens.get("oauth_token"):
        raise ProbeError("source_ineligible")
    bearer = tokens["oauth_token"]
    if (
        len(bearer) > 16384
        or any(not 33 <= ord(char) <= 126 for char in bearer)
        or bearer.startswith("sk-ant-sid")
    ):
        raise ProbeError("unsupported_source")
    expires = IdentityExtractor.exp_from_tokens(tokens)
    if expires is not None and expires <= time.time():
        raise ProbeError("expired")
    organization, authority = await asyncio.to_thread(
        support.authority_read, account, source_id, tokens, candidate
    )
    if await token_cache.peek_source("anthropic", account, source_id) != snapshot:
        raise ProbeError("source_changed")
    return tokens, organization, authority


def page(payload: object, limit: int) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProbeError("unsupported_contract")
    records = payload["data"]
    if len(records) > limit or any(not isinstance(record, dict) for record in records):
        raise ProbeError("unsupported_contract")
    if "next_cursor" not in payload:
        raise ProbeError("unsupported_contract")
    cursor = payload["next_cursor"]
    if cursor is not None and (
        not isinstance(cursor, str)
        or not 0 < len(cursor) <= 2048
        or any(ord(char) < 32 for char in cursor)
    ):
        raise ProbeError("unsupported_contract")
    return records, cursor


class Probe:
    def __init__(
        self,
        request: ClaudeCloudProbeRequest,
        client: httpx.AsyncClient,
        *,
        total_byte_limit: int = MAX_TOTAL_BYTES,
    ):
        self.request = request
        self.client = client
        self.account = canonical_account_id(request.account_id)
        self.report = empty_report(request.report_version)
        self.tokens: dict[str, str] = {}
        self.expected_org: str | None = None
        self.authority: tuple[Any, ...] = ()
        self.total_bytes = 0
        self.total_byte_limit = total_byte_limit

    async def check_source(self) -> None:
        try:
            current = await load_source(self.account, self.request.source_id)
        except ProbeError:
            raise ProbeError("source_changed") from None
        if current != (self.tokens, self.expected_org, self.authority):
            raise ProbeError("source_changed")

    async def get(
        self, path: str, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> Any:
        await self.check_source()
        self.report["requests"] += 1
        async with self.client.stream(
            "GET",
            BASE_URL + path,
            headers=headers,
            params=params,
            timeout=5,
            follow_redirects=False,
        ) as response:
            self.report["http_status"] = response.status_code
            if response.status_code in (401, 403):
                raise ProbeError("rejected")
            if response.status_code == 429:
                raise ProbeError("rate_limited")
            if response.status_code != 200:
                raise ProbeError("unavailable")
            if self._response_encoding(response) != "identity":
                raise ProbeError("unsupported_contract")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                self.total_bytes += len(chunk)
                if (
                    len(body) + len(chunk) > MAX_RESPONSE_BYTES
                    or self.total_bytes > self.total_byte_limit
                ):
                    raise ProbeError("response_limit")
                body.extend(chunk)
        await self.check_source()
        try:
            return self._decode(body)
        except (ValueError, RecursionError):
            raise ProbeError("unsupported_contract") from None

    def _response_encoding(self, response: httpx.Response) -> str:
        return response.headers.get("content-encoding", "identity").lower()

    def _decode(self, body: bytearray) -> Any:
        return json.loads(
            body,
            object_pairs_hook=support.strict_object,
            parse_constant=support.strict_constant,
        )

    async def identity(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.tokens['oauth_token']}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept-Encoding": "identity",
            "User-Agent": "runway-cloud-probe",
        }
        self.report["stage"] = "profile"
        profile = await self.get("/api/oauth/profile", headers)
        holder = profile.get("account") if isinstance(profile, dict) else None
        organization = profile.get("organization") if isinstance(profile, dict) else None
        if not isinstance(holder, dict) or not isinstance(organization, dict):
            raise ProbeError("identity_unavailable")
        email = holder.get("email")
        subject, org = (
            support.strict_uuid(holder.get("uuid")),
            support.strict_uuid(organization.get("uuid")),
        )
        if not isinstance(email, str) or "@" not in email or subject is None or (org is None):
            raise ProbeError("identity_unavailable")
        if canonical_account_id(email) != self.account:
            raise ProbeError("identity_mismatch")
        for expected, actual in (
            (self.tokens.get("oauth_account_uuid"), subject),
            (self.tokens.get("oauth_organization_uuid"), org),
            (self.tokens.get("organization_id"), org),
            (self.request.organization_id, org),
            (self.expected_org, org),
        ):
            if expected is not None and support.strict_uuid(expected) != actual:
                raise ProbeError("identity_mismatch")
        self.report["account_verified"] = self.report["organization_verified"] = True
        headers.update(
            {
                "anthropic-beta": "ccr-byoc-2025-07-29",
                "anthropic-client-feature": "ccr",
                "anthropic-version": "2023-06-01",
                "x-organization-uuid": org,
            }
        )
        return headers

    async def sample(self) -> None:
        headers = await self.identity()
        self.report["stage"] = "sessions"
        listed, cursor = page(await self.get("/v1/code/sessions", headers, {"limit": 20}), 20)
        self.report["session_list_records"] = len(listed)
        self.report["session_list_complete"] = cursor is None
        ids = [row.get("id") for row in listed]
        if any(not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) for sid in ids):
            raise ProbeError("unsupported_contract")
        if len(set(ids)) != len(ids):
            raise ProbeError("unsupported_contract")
        target = self.request.session_id
        if target is not None and session_key(target) not in {
            session_key(cast(str, v)) for v in ids
        }:
            self.report["outcome"] = "sample_not_found"
            return
        if not ids:
            self.report["outcome"] = "no_sessions" if cursor is None else "sample_limited"
            return
        # An exact listed id wins; the prefix-equivalent form is only a fallback.
        sid = next(
            (cast(str, v) for v in ids if v == target),
            next(
                (
                    cast(str, v)
                    for v in ids
                    if target is not None and session_key(cast(str, v)) == session_key(target)
                ),
                cast(str, ids[0]),
            ),
        )
        await self.history(sid, headers)

    async def history(self, sid: str, headers: dict[str, str]) -> None:
        if not SESSION_ID.fullmatch(sid):
            raise ProbeError("unsupported_contract")
        self.report["stage"] = "history"
        self.report["history_complete"] = False
        records_by_location: dict[str, list[dict[str, Any]]] = {
            key: [] for key in OBSERVATION_LOCATIONS[self.request.report_version]
        }
        seen_cursors: set[str] = set()
        sequences: set[int] = set()
        previous: int | None = None
        cursor = None
        for _ in range(self.request.max_history_pages):
            params: dict[str, Any] = {"limit": 50, "sort_order": "asc"}
            if cursor is not None:
                params["cursor"] = cursor
            records, cursor = page(
                await self.get(f"/v1/code/sessions/{quote(sid, safe='')}/events", headers, params),
                50,
            )
            self.report["history_pages"] += 1
            self.report["history_events"] += len(records)
            for event in records:
                if self.request.report_version == 2:
                    observe_structure(self.report["structure"], event)
                raw_sequence = event.get("sequence_num")
                sequence = None
                if type(raw_sequence) is int and 0 <= raw_sequence < 2**63:
                    sequence = raw_sequence
                elif isinstance(raw_sequence, str) and re.fullmatch("[0-9]{1,19}", raw_sequence):
                    number = int(raw_sequence)
                    sequence = number if number < 2**63 else None
                if sequence is None:
                    self.report["sequence"]["invalid"] += 1
                else:
                    self.report["sequence"]["numeric"] += 1
                    if sequence in sequences:
                        self.report["sequence"]["duplicates"] += 1
                    elif previous is not None:
                        self.report["sequence"]["gaps"] += sequence > previous + 1
                        self.report["sequence"]["out_of_order"] += sequence < previous
                    sequences.add(sequence)
                    previous = sequence
                for location, observations in records_by_location.items():
                    value = event if location == "event" else event.get(location)
                    if isinstance(value, dict) and value.get("type") in ("assistant", "result"):
                        observations.append(value)
            if cursor is None:
                self.report["history_complete"] = True
                break
            if cursor in seen_cursors:
                raise ProbeError("unsupported_contract")
            seen_cursors.add(cursor)
        self.report["observations"] = [
            {"location": location, "report": audit_messages(records)}
            for location, records in records_by_location.items()
            if records
        ]
        self.report["outcome"] = (
            "sample_complete" if self.report["history_complete"] else "sample_limited"
        )
        self.report["stage"] = "complete"


async def probe_cloud_history(
    request: ClaudeCloudProbeRequest, client: httpx.AsyncClient
) -> dict[str, Any]:
    """Bound the whole attempt and recheck authority before publication."""
    attempt = Probe(request, client)
    if support.gate.locked():
        attempt.report["outcome"] = "busy"
        return attempt.report
    async with support.gate:
        with probing():
            try:
                async with asyncio.timeout(TOTAL_TIMEOUT):
                    attempt.tokens, attempt.expected_org, attempt.authority = await load_source(
                        attempt.account, request.source_id
                    )
                    async with token_cache.using_source(
                        "anthropic", attempt.account, request.source_id
                    ):
                        try:
                            await attempt.sample()
                        except (
                            ProbeError,
                            httpx.HTTPError,
                            ValueError,
                            RecursionError,
                        ):
                            await attempt.check_source()
                            raise
                        await attempt.check_source()
            except ProbeError as exc:
                attempt.report["outcome"] = str(exc)
            except (TimeoutError, httpx.TimeoutException):
                attempt.report["outcome"] = "timeout"
            except SQLAlchemyError:
                attempt.report["outcome"] = "source_unavailable"
            except httpx.HTTPError:
                attempt.report["outcome"] = "unavailable"
            except (ValueError, RecursionError):
                attempt.report["outcome"] = "unsupported_contract"
            if attempt.report["outcome"] in ("source_changed", "timeout", "source_unavailable"):
                report = empty_report(request.report_version)
                report.update(
                    outcome=attempt.report["outcome"],
                    requests=attempt.report["requests"],
                    http_status=attempt.report["http_status"],
                )
                return report
    return attempt.report
