"""One source-pinned manual-cookie Chat observation; no discovery, refresh or writes."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from sqlalchemy.exc import SQLAlchemyError

from app.core.utils import IdentityExtractor
from app.models.claude_chat_fixture import ChatFixtureRequest

# Deliberately share the cloud diagnostics' worker gate and read-only source authority.
# They live in the public support module, so both diagnostics contend for one gate
# without reaching into each other's privates. Chat owns its request/response/time
# budgets and public vocabulary; the Code protocol and its evidence formats remain
# unchanged.
from app.services import claude_cloud_support as support
from app.services.account_identity import canonical_account_id
from app.services.claude_cloud_support import ProbeError
from app.services.claude_verification import subscription_headers
from app.services.collectors.anthropic_console import ConsoleAPIError
from app.services.probe_mode import probing
from app.services.token_cache import SourceCacheInstance, token_cache
from scripts.claude_chat_evidence import empty_report, observe_conversation

TOTAL_TIMEOUT = 20.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 3 * MAX_RESPONSE_BYTES
MAX_REQUESTS = 3


async def _load_source(
    account: str, source_id: str
) -> tuple[dict[str, str], str | None, tuple[Any, ...], SourceCacheInstance]:
    if account == "default":
        raise ProbeError("source_ineligible")
    snapshot = await token_cache.peek_source_instance("anthropic", account, source_id)
    if snapshot is None:
        raise ProbeError("source_unavailable")
    tokens, candidate, instance = snapshot
    if candidate.get("identity_pending") or candidate.get("durable_source") is True:
        raise ProbeError("source_ineligible")
    cookies = [tokens[key] for key in ("cookie_sessionKey", "session_cookie") if tokens.get(key)]
    if not cookies or any(not isinstance(value, str) or len(value) > 16384 for value in cookies):
        raise ProbeError("source_ineligible")
    if len(set(cookies)) != 1:
        raise ProbeError("source_ineligible")
    try:
        subscription_headers(cookies[0])
    except ConsoleAPIError:
        raise ProbeError("source_ineligible") from None
    organization, proof = await asyncio.to_thread(
        support.authority_read, account, source_id, tokens, candidate
    )
    # Config cookies only. A native/enrolled/legacy sidecar must not borrow this consent.
    if proof[0] != "config" or (proof[1] and not proof[1].startswith("config:")):
        raise ProbeError("unsupported_source")
    expires = IdentityExtractor.exp_from_tokens(tokens)
    if expires is not None and expires <= time.time():
        raise ProbeError("expired")
    if await token_cache.peek_source_instance("anthropic", account, source_id) != snapshot:
        raise ProbeError("source_changed")
    return tokens, organization, proof, instance


class _Attempt:
    def __init__(self, request: ChatFixtureRequest, client: httpx.AsyncClient):
        self.request = request
        self.account = canonical_account_id(request.account_id)
        self.client = client
        self.report = empty_report()
        self.snapshot: (
            tuple[dict[str, str], str | None, tuple[Any, ...], SourceCacheInstance] | None
        ) = None
        self.total_bytes = 0

    async def check_source(self) -> None:
        try:
            now = await _load_source(self.account, self.request.source_id)
        except ProbeError:
            raise ProbeError("source_changed") from None
        if now != self.snapshot:
            raise ProbeError("source_changed")

    async def get(self, path: str, headers: dict[str, str]) -> Any:
        await self.check_source()
        if self.report["requests"] >= MAX_REQUESTS:
            raise ProbeError("response_limit")
        self.report["requests"] += 1
        async with self.client.stream(
            "GET", "https://claude.ai" + path, headers=headers, timeout=5, follow_redirects=False
        ) as response:
            self.report["http_status"] = response.status_code
            if (
                "text/html" in response.headers.get("content-type", "").lower()
                or response.headers.get("cf-mitigated") == "challenge"
            ):
                raise ProbeError("challenge")
            if response.status_code in (401, 403):
                raise ProbeError("rejected")
            if response.status_code == 429:
                raise ProbeError("rate_limited")
            if response.status_code != 200:
                raise ProbeError("unavailable")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ProbeError("unsupported_contract")
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                self.total_bytes += len(chunk)
                if (
                    len(body) + len(chunk) > MAX_RESPONSE_BYTES
                    or self.total_bytes > MAX_TOTAL_BYTES
                ):
                    raise ProbeError("response_limit")
                body.extend(chunk)
        # The next dispatch rechecks authority before any new credential use.
        # The outer guard also rechecks on errors and before publishing results;
        # another engine here would repeat the same boundary check.
        return json.loads(
            body, object_pairs_hook=support.strict_object, parse_constant=support.strict_constant
        )

    async def inspect(self) -> None:
        assert self.snapshot is not None
        tokens, bound_org, _, _ = self.snapshot
        organization = support.strict_uuid(self.request.organization_id)
        target = support.strict_uuid(self.request.conversation_id)
        if organization is None or target is None or (bound_org and bound_org != organization):
            raise ProbeError("source_ineligible")
        cookie = tokens.get("cookie_sessionKey") or tokens["session_cookie"]
        headers = {**subscription_headers(cookie), "Accept-Encoding": "identity"}
        self.report["stage"] = "account"
        account = await self.get("/api/account", headers)
        if isinstance(account, list) and len(account) == 1:
            account = account[0]
        if not isinstance(account, dict) or support.strict_uuid(account.get("uuid")) is None:
            raise ProbeError("identity_unavailable")
        email = account.get("email_address")
        if not isinstance(email, str) or "@" not in email:
            raise ProbeError("identity_unavailable")
        if canonical_account_id(email) != self.account:
            raise ProbeError("identity_mismatch")
        self.report["account_verified"] = True
        self.report["stage"] = "organizations"
        organizations = await self.get("/api/organizations", headers)
        if not isinstance(organizations, list) or len(organizations) > 100:
            raise ProbeError("unsupported_contract")
        ids = [
            support.strict_uuid(row.get("uuid")) if isinstance(row, dict) else None
            for row in organizations
        ]
        if None in ids or len(set(ids)) != len(ids):
            raise ProbeError("unsupported_contract")
        if organization not in ids:
            raise ProbeError("organization_mismatch")
        self.report["organization_verified"] = True
        self.report["stage"] = "conversation"
        payload = await self.get(
            f"/api/organizations/{organization}/chat_conversations/{target}", headers
        )
        if not isinstance(payload, dict) or support.strict_uuid(payload.get("uuid")) != target:
            raise ProbeError("target_mismatch")
        observed = observe_conversation(payload)
        for field in ("messages", "roles", "identities", "usage"):
            self.report[field] = observed[field]
        self.report.update(target_verified=True, stage="complete", outcome="observed")


async def inspect_chat_fixture(
    request: ChatFixtureRequest, client: httpx.AsyncClient
) -> dict[str, Any]:
    """Observe a private exact target; all late source uncertainty clears observations."""
    attempt = _Attempt(request, client)
    if support.gate.locked():
        attempt.report["outcome"] = "busy"
        return attempt.report
    async with support.gate:
        with probing():
            try:
                async with asyncio.timeout(TOTAL_TIMEOUT):
                    attempt.snapshot = await _load_source(attempt.account, request.source_id)
                    async with token_cache.using_source(
                        "anthropic", attempt.account, request.source_id
                    ):
                        try:
                            await attempt.inspect()
                        except (ProbeError, httpx.HTTPError, ValueError, RecursionError):
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
    if attempt.report["outcome"] in ("source_changed", "source_unavailable", "timeout"):
        report = empty_report()
        report.update(
            outcome=attempt.report["outcome"],
            requests=attempt.report["requests"],
            http_status=attempt.report["http_status"],
        )
        return report
    return attempt.report
