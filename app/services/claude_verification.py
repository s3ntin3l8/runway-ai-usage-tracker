"""Verify one pinned Claude cookie against independently usable organizations."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from app.services.collectors.anthropic_console import (
    CONSOLE_URL,
    ConsoleAPIError,
    console_headers,
    parse_console_balance,
)
from app.services.token_cache import token_cache


def subscription_headers(cookie: str) -> dict[str, str]:
    if not cookie.strip() or "\r" in cookie or "\n" in cookie:
        raise ConsoleAPIError("Invalid Claude cookie", "missing_config", 422)
    # If the cookie already looks like a multi-cookie string (contains ; or =),
    # use it as-is. Otherwise wrap it in sessionKey=.
    cookie_header = cookie if ";" in cookie or "=" in cookie else f"sessionKey={cookie}"

    # Extract activitySessionId from cookies if present for x-activity-session-id header
    activity_sid_match = re.search(r"activitySessionId=([^;]+)", cookie_header)
    activity_sid = activity_sid_match.group(1) if activity_sid_match else None

    headers = {
        "Cookie": cookie_header,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer": "https://claude.ai/settings/usage",
        "origin": "https://claude.ai",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "x-requested-with": "XMLHttpRequest",
    }
    if activity_sid:
        headers["x-activity-session-id"] = activity_sid
    return headers


async def claude_get(client: httpx.AsyncClient, provider: str, path: str, cookie: str) -> Any:
    console = provider == "anthropic_console"
    headers = console_headers(cookie) if console else subscription_headers(cookie)
    try:
        response = await client.get(
            (CONSOLE_URL if console else "https://claude.ai") + path,
            headers=headers,
            timeout=10,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise ConsoleAPIError("Claude connection unavailable", "timeout") from exc
    if (
        "text/html" in response.headers.get("content-type", "").lower()
        or response.headers.get("cf-mitigated") == "challenge"
    ):
        raise ConsoleAPIError("Claude browser verification required; retry later", "challenge")
    if response.status_code == 401:
        raise ConsoleAPIError("Claude session expired", "auth_failed", 401)
    if response.status_code == 403:
        raise ConsoleAPIError("Claude organization access denied", "permission_denied", 403)
    if response.status_code == 429:
        raise ConsoleAPIError("Claude rate limited; retry later", "rate_limited", 429)
    if response.status_code != 200:
        raise ConsoleAPIError("Claude request unavailable", "api_error")
    try:
        return response.json()
    except ValueError as exc:
        raise ConsoleAPIError("Invalid Claude response", "parse_error") from exc


@dataclass
class ClaudeVerification:
    account_id: str | None = None
    organizations: list[dict[str, str]] = field(default_factory=list)
    complete: bool = True
    organization_id: str | None = None
    account: dict[str, Any] | None = None
    org_data: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None

    @property
    def selection_required(self) -> bool:
        return len(self.organizations) > 1 and self.organization_id is None


def organization_uuid(value: Any) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConsoleAPIError("Invalid Claude organization identity", "parse_error", 422) from exc


async def verify_claude_cookie(
    client: httpx.AsyncClient, provider: str, cookie: str, selected: str | None = None
) -> ClaudeVerification:
    if provider not in ("anthropic", "anthropic_console"):
        raise ConsoleAPIError("Unsupported Claude provider", "missing_config", 422)
    if selected:
        selected = organization_uuid(selected)
    result = ClaudeVerification()
    if provider == "anthropic":
        account = await claude_get(client, provider, "/api/account", cookie)
        if isinstance(account, list) and len(account) == 1:
            account = account[0]
        email = account.get("email_address") if isinstance(account, dict) else None
        if not isinstance(email, str) or "@" not in email:
            raise ConsoleAPIError("Claude account identity unavailable", "parse_error")
        result.account_id = email.strip().lower()
        result.account = account
    orgs = await claude_get(client, provider, "/api/organizations", cookie)
    if not isinstance(orgs, list):
        raise ConsoleAPIError("Invalid Claude organizations", "parse_error")
    usable: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for org in orgs:
        if not isinstance(org, dict):
            result.complete = False
            continue
        if provider == "anthropic_console" and "api" not in (org.get("capabilities") or []):
            continue
        try:
            org_id = organization_uuid(org.get("uuid") or org.get("id"))
            path = f"/api/organizations/{org_id}/" + (
                "prepaid/credits" if provider == "anthropic_console" else "usage"
            )
            usage = await claude_get(client, provider, path, cookie)
            if provider == "anthropic_console":
                parse_console_balance(usage)
            else:
                if not isinstance(usage, dict):
                    raise ValueError("Missing quota")
                quota_values = [
                    v.get("utilization", v.get("percentUsed"))
                    for v in usage.values()
                    if isinstance(v, dict)
                    and (v.get("utilization") is not None or v.get("percentUsed") is not None)
                ]
                if not quota_values or any(
                    value is None or isinstance(value, bool) or not math.isfinite(float(value))
                    for value in quota_values
                ):
                    raise ValueError("Invalid quota")
        except ConsoleAPIError as exc:
            if exc.error_type == "auth_failed":
                raise
            if exc.error_type != "permission_denied":
                result.complete = False
            continue
        except (ValueError, TypeError, KeyError):
            result.complete = False
            continue
        usable[org_id] = (org, usage)
        result.organizations.append(
            {"id": org_id, "name": str(org.get("name") or "Claude organization")}
        )
    choice = selected if selected in usable else None
    if selected is None and result.complete and len(usable) == 1:
        choice = next(iter(usable))
    if choice:
        result.organization_id = choice
        result.org_data, result.usage = usable[choice]
        if provider == "anthropic_console":
            result.account_id = choice
    return result


def is_native_claude_source(provider: str) -> bool:
    metadata = token_cache.current_source_metadata(provider, "default") or {}
    return str(metadata.get("credential_origin", "")).startswith(f"cookie:{provider}/")


def selected_organization(provider: str, account_id: str | None) -> str | None:
    from sqlmodel import Session, select

    from app.core.db import engine
    from app.models.db import ProviderConfig

    if not account_id or account_id == "default":
        return None
    with Session(engine) as session:
        row = session.exec(
            select(ProviderConfig).where(
                ProviderConfig.provider_id == provider, ProviderConfig.account_id == account_id
            )
        ).first()
        return row.claude_organization_id if row else None


def persist_pending_organizations(
    provider: str, source_id: str, result: ClaudeVerification
) -> None:
    import json

    from sqlmodel import Session, select

    from app.core.db import engine
    from app.models.db import PendingCredentialTag
    from app.services.credential_sources import sidecar_source_id

    with Session(engine) as session:
        for row in session.exec(
            select(PendingCredentialTag).where(PendingCredentialTag.provider_id == provider)
        ).all():
            if sidecar_source_id(row.sidecar_id, row.credential_origin) == source_id:
                row.claude_organizations_json = json.dumps(result.organizations)
                row.claude_organizations_complete = result.complete
                session.add(row)
        session.commit()


async def verify_collector_cookie(
    collector: Any, client: httpx.AsyncClient, provider: str, cookie: str
) -> ClaudeVerification:
    selection = token_cache.selected_source(provider)
    tokens_before = token_cache.current_source_tokens(provider, "default")
    selected = selected_organization(provider, collector.account_id)
    result = await verify_claude_cookie(client, provider, cookie, selected)
    # A pending source has no account slot yet. Only after the account endpoint proves
    # its email may we consult that account's saved choice; never use a claimed identity.
    if selected is None and result.account_id:
        bound = selected_organization(provider, result.account_id)
        if bound and bound != result.organization_id:
            result = await verify_claude_cookie(client, provider, cookie, bound)
    if selection and token_cache.current_source_tokens(provider, "default") != tokens_before:
        # No result obtained with the old secret proves the newly arrived bundle.
        return ClaudeVerification(complete=False)
    if selection:
        persist_pending_organizations(provider, selection[1], result)
    if result.organization_id and result.account_id:
        collector.verified_source_generation = token_cache.credential_generation(
            tokens_before or {}
        )
        collector.verified_identity = result.account_id
        collector.verified_organization_id = result.organization_id
        # Every attempt starts at default; only the exact pinned source may establish it.
        if not collector.account_id or collector.account_id == "default":
            collector.account_id = result.account_id
    return result


@dataclass(frozen=True)
class ClaudeSourceProof:
    provider_id: str
    account_id: str
    source_id: str
    generation: str
    organization_id: str
    credential_origin: str | None
    sidecar_id: str | None


class ClaudeVerifiedCard(dict[str, Any]):
    """A normal JSON card with private nonsecret proof carried only to the poller."""

    def __init__(self, card: dict[str, Any], proof: ClaudeSourceProof):
        super().__init__(card)
        self.claude_source_proof = proof
