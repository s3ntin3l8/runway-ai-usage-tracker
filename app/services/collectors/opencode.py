"""
OpenCode quota collector.

Collection Strategy (priority order):

1. ``api`` (PRIMARY): OpenCode Go usage API.
   - Auth: ``Authorization: Bearer <oc_sk_…>`` (or whatever the opencode CLI
     stores as ``opencode-go.key``).
   - Endpoint: ``GET https://opencode.ai/zen/go/v1/usage``.

2. ``web`` (FALLBACK): OpenCode Console session cookies.
   - Auth: ``auth`` + ``__Host-console_session`` cookies (browser-imported
     or pasted).
   - Endpoint A: ``GET https://opencode.ai/console/api/orgs`` lists
     workspaces (cookie auth).
   - Endpoint B: ``GET https://opencode.ai/console/api/go/status`` with
     ``x-org-id`` header returns subscription meters.

The legacy ``/_server?id=def3997…`` server-function path is no longer used
— opencode migrated its workspaces to the console and that fn id either
returns a 302-encoded login redirect or a client-rendered SPA shell.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.utils import PaceCalculator, error_card, http_request_with_retry, scrub_log
from app.services.collectors.base import BaseCollector, normalize_account_id
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

_BASE_URL = "https://opencode.ai"

# Window-key -> canonical Runway window_type mapping for the Go tier.
_WINDOW_TYPE_MAP: dict[str, str] = {
    "fiveHour": "session",
    "week": "weekly",
    "month": "monthly",
    "rolling": "session",
    "weekly": "weekly",
    "monthly": "monthly",
}

# Default limits per window (USD), used when the API returns a missing /
# null ``limitMicroCents``. Matches the documented OpenCode Go tier.
_DEFAULT_LIMIT_USD: dict[str, float] = {
    "fiveHour": 12.0,
    "week": 30.0,
    "month": 60.0,
    "rolling": 12.0,
    "weekly": 30.0,
    "monthly": 60.0,
}


class OpenCodeCollector(BaseCollector):
    PROVIDER_ID = "opencode"
    DEFAULT_WINDOW_TYPE = "weekly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("OpenCode API key", "_get_opencode_api"),
        "web": ("Console session cookies", "_get_opencode_web"),
    }

    def __init__(
        self,
        account_id: str | None = None,
        account_label: str | None = None,
        credential_account_id: str | None = None,
    ):
        super().__init__(account_id=account_id, account_label=account_label)
        self.credential_account_id = credential_account_id or account_id or "default"
        self._cookie_owner: str | None = None
        self._last_error_reason: str = "unknown"
        self._last_error_warned: bool = False

    async def is_configured(self) -> bool:
        """OpenCode is configured when either an API key or session cookies are cached."""
        acc = self.account_id or "default"
        if credential_provider.get_provider_api_key(
            "opencode", account_id=self.credential_account_id
        ):
            return True
        if credential_provider.get_provider_session_cookie(
            "opencode", account_id=self.credential_account_id
        ):
            return True
        if self.credential_account_id == "default" and (
            settings.OPENCODE_API_KEY or settings.OPENCODE_GO_API_KEY
        ):
            return True
        api_key = await token_cache.get_token("opencode", "api_key", account_id=acc)
        if api_key:
            return True
        for token_type in ("cookie_session", "console_session"):
            val = await token_cache.get_token("opencode", token_type, account_id=acc)
            if val:
                return True
        return False

    async def reset(self):
        """Reset per-cycle error diagnosis."""
        self._last_error_reason = "unknown"
        self._last_error_warned = False

    async def collect(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Start each strategy chain with a fresh diagnostic state."""
        await self.reset()
        return await super().collect(client)

    # Both methods satisfy the abstract contract on BaseCollector. They are
    # unused at runtime: STRATEGIES declares the api/web order, and base
    # only invokes this legacy pair when STRATEGIES is empty.
    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return await self._get_opencode_api(client)

    def _fallback_strategies(self) -> list[Any]:
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Emit an auth_failed / api_error card instead of returning []. The
        pre-existing collector silently returned ``[]`` on every failure, which
        meant the dashboard went blank with zero diagnostic signal."""
        reason = self._last_error_reason
        if reason in (
            "invalid_api_key",
            "session_invalid",
            "missing_cookies",
            "missing_api_key",
        ):
            message = (
                "OpenCode session expired — paste a fresh `oc_sk_…` API key "
                "from the opencode console into Providers → opencode, or "
                "ensure the sidecar can read "
                "`~/.local/share/opencode/auth.json`."
            )
            error_type = "auth_failed"
        elif reason == "no_workspace":
            message = "OpenCode: no workspace found for the configured account."
            error_type = "parse_error"
        elif reason == "api_unavailable":
            message = "OpenCode: usage API unreachable. Will retry on next cycle."
            error_type = "api_error"
        elif reason == "invalid_config":
            message = "Set the OpenCode workspace ID in settings."
            error_type = "invalid_config"
        else:
            message = "OpenCode quota collection failed."
            error_type = "unknown"
        return [error_card("OpenCode", "⚡", message, error_type=error_type)]

    def _pin_identity(self, scraped_email: str | None) -> None:
        """Pin account_id / account_label so cards and events land under a
        stable identity rather than "default" (#276, #315).

        The cookie / key owner is authoritative; the API-scraped email is
        only a fallback — with several opencode accounts the API can show
        a different identity than the credential's. A disagreement is
        logged so the split is visible instead of silent.
        """
        owner = self._cookie_owner
        scraped = scraped_email if (scraped_email and "@" in scraped_email) else None
        if owner and scraped and owner.lower() != scraped.lower():
            logger.warning(
                "OpenCode: API response shows %s but the credential belongs to %s; "
                "using the credential owner",
                scrub_log(scraped),
                scrub_log(owner),
            )
        identity = owner or scraped
        if identity:
            self.account_label = identity
            if not self.account_id or self.account_id == "default":
                self.account_id = normalize_account_id(identity)

    async def _get_credentials(self) -> tuple[dict[str, Any], str]:
        """Return cached credentials and provenance for this collector account.

        Also stamps ``_cookie_owner`` from the metadata's ``account_label``
        so ``_pin_identity`` can prefer the credential's identity over the
        API-scraped email when the two disagree (#315).
        """
        res = await token_cache.get_with_metadata(
            "opencode", account_id=self.account_id or "default"
        )
        if not res:
            return {}, "unknown"
        tokens, metadata = res
        # Identity lives on the credential side, not the API response. The
        # token-cache metadata's account_label carries it through from the
        # sidecar push / UI paste — if it looks like an email, it's the
        # credential owner. Otherwise fall back to the constructor's label.
        meta_label = str(metadata.get("account_label") or "")
        self._cookie_owner = (
            meta_label
            if "@" in meta_label
            else (self.account_label if self.account_label and "@" in self.account_label else None)
        )
        source = metadata.get("source")
        return dict(tokens), "sidecar" if source else "config"

    def _set_error(self, reason: str) -> None:
        priority = {
            "unknown": 0,
            "missing_api_key": 1,
            "missing_cookies": 1,
            "no_workspace": 2,
            "invalid_api_key": 3,
            "session_invalid": 3,
            "api_error": 4,
            "api_unavailable": 4,
            "parse_error": 4,
            "invalid_config": 5,
        }
        if priority.get(reason, 0) < priority.get(self._last_error_reason, 0):
            return
        if reason != self._last_error_reason:
            self._last_error_reason = reason
            self._last_error_warned = False
        if not self._last_error_warned:
            logger.warning("OpenCode collector: %s", reason)
            self._last_error_warned = True

    # --- api strategy -----------------------------------------------------

    async def _get_opencode_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Bearer-authenticated OpenCode Go usage API."""
        tokens, input_source = await self._get_credentials()
        api_key = credential_provider.get_provider_api_key(
            "opencode", account_id=self.credential_account_id
        )
        if api_key:
            input_source = "config"
        else:
            api_key = tokens.get("api_key") or tokens.get("OPENCODE_API_KEY")
        if not api_key and self.credential_account_id == "default":
            api_key = settings.OPENCODE_API_KEY or settings.OPENCODE_GO_API_KEY or None
            if api_key:
                input_source = "server"
        if not api_key:
            self._set_error("missing_api_key")
            return []
        try:
            return await self._fetch_api_meters(client, api_key, input_source)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                self._set_error("invalid_api_key")
            else:
                self._set_error("api_error")
            logger.warning(
                "OpenCode API auth fetch failed (status=%s): %s",
                exc.response.status_code,
                scrub_log(str(exc)),
            )
            return []
        except httpx.TimeoutException as exc:
            self._set_error("api_unavailable")
            logger.warning("OpenCode API fetch timed out: %s", scrub_log(str(exc)))
            return []
        except Exception as exc:
            self._set_error("api_error")
            logger.warning("OpenCode API fetch failed: %s", scrub_log(str(exc)))
            return []

    async def _fetch_api_meters(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        input_source: str,
    ) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        }
        if api_key.lower().startswith("bearer "):
            headers["Authorization"] = api_key
        body, _source = await self._get_first_2xx(
            client, [(f"{_BASE_URL}/zen/go/v1/usage", headers)]
        )
        if body is None:
            self._set_error("invalid_api_key")
            return []
        cards = self._build_cards_from_zen_usage(body, input_source)
        if not cards:
            self._set_error("parse_error")
        else:
            self._last_error_reason = "unknown"
        return cards

    async def _get_first_2xx(
        self, client: httpx.AsyncClient, attempts: list[tuple[str, dict[str, str]]]
    ) -> tuple[dict[str, Any] | None, str | None]:
        for url, headers in attempts:
            try:
                resp = await http_request_with_retry(
                    client, "GET", url, headers=headers, timeout=15.0, follow_redirects=True
                )
            except httpx.TimeoutException:
                self._set_error("api_unavailable")
                continue
            except Exception as exc:
                self._set_error("api_unavailable")
                logger.debug("OpenCode: GET %s failed: %s", url, scrub_log(str(exc)))
                continue
            if resp.status_code != 200:
                if resp.status_code in (401, 403):
                    self._set_error("invalid_api_key")
                elif resp.status_code >= 500:
                    self._set_error("api_error")
                continue
            label = "console_go_status" if "/console/api/go/status" in url else "zen_go_v1_usage"
            try:
                return resp.json(), label
            except Exception:
                self._set_error("parse_error")
                logger.debug("OpenCode: non-JSON response from %s", url)
                continue
        return None, None

    def _build_cards_from_go_status(
        self, body: dict[str, Any], input_source: str
    ) -> list[dict[str, Any]]:
        access = (body or {}).get("access") or {}
        meters = access.get("meters") or {}
        if not meters:
            return []
        account = body.get("subscriberUserId") or ""
        if account and "@" in account:
            self._pin_identity(account)
        period_end_iso = access.get("endsAt")
        period_end = self._parse_iso(period_end_iso) if period_end_iso else None
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        cards: list[dict[str, Any]] = []
        for meter_key, window_type in _WINDOW_TYPE_MAP.items():
            meter = meters.get(meter_key)
            if not isinstance(meter, dict):
                continue
            used = self._microcents_to_usd(meter.get("usedMicroCents"))
            # A meter without usage is incomplete; do not report a fabricated
            # zero or let one malformed meter discard the other valid meters.
            if used is None:
                continue
            limit = self._microcents_to_usd(meter.get("limitMicroCents"))
            if limit is None or limit <= 0:
                limit = _DEFAULT_LIMIT_USD.get(meter_key, 0.0)
            pct = (used / limit * 100) if limit > 0 else 0
            reset_iso = meter.get("resetsAt")
            reset_at = self._parse_iso(reset_iso) if reset_iso else period_end
            cards.append(
                self._build_api_card(
                    used=used,
                    limit=limit,
                    pct=pct,
                    reset_at=reset_at,
                    window_type=window_type,
                    input_source=input_source,
                    now_iso=now_iso,
                    data_source=self.DATA_SOURCE_WEB,
                )
            )
        return cards

    def _build_cards_from_zen_usage(
        self, body: dict[str, Any], input_source: str
    ) -> list[dict[str, Any]]:
        usage = (body or {}).get("usage") or {}
        if not usage:
            return []
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        cards: list[dict[str, Any]] = []
        for key in ("rolling", "weekly", "monthly"):
            window = usage.get(key)
            if not isinstance(window, dict):
                continue
            pct_raw = window.get("percent")
            if pct_raw is None:
                continue
            pct = float(pct_raw)
            limit = _DEFAULT_LIMIT_USD.get(key, 0.0)
            used = (pct / 100.0) * limit if limit > 0 else 0
            reset_at = self._parse_iso(window.get("resetsAt"))
            status = window.get("status")
            cards.append(
                self._build_api_card(
                    used=used,
                    limit=limit,
                    pct=pct,
                    reset_at=reset_at,
                    window_type=_WINDOW_TYPE_MAP.get(key, key),
                    input_source=input_source,
                    now_iso=now_iso,
                    status=status,
                    data_source=self.DATA_SOURCE_API,
                )
            )
        return cards

    def _build_api_card(
        self,
        *,
        used: float,
        limit: float,
        pct: float,
        reset_at: datetime | None,
        window_type: str,
        input_source: str,
        now_iso: str,
        data_source: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        remaining = max(0.0, limit - used)
        reset_label = self._reset_label(window_type)
        # rate-limited windows can still be below 90% pct; mark them critical
        # explicitly so the dashboard reflects "you've been throttled".
        if status == "rate-limited" or pct >= 90:
            health = "critical"
        elif pct >= 70:
            health = "warning"
        else:
            health = "good"
        return {
            "service_name": "OpenCode",
            "icon": "⚡",
            "remaining": f"${remaining:.2f}",
            "unit": f"${limit:.0f} limit",
            "reset": reset_label,
            "health": health,
            "pace": PaceCalculator.estimate_longevity(pct, reset_at) if reset_at else "—",
            "detail": f"${used:.2f} used ({pct:.1f}%) · OpenCode Go API",
            "used_value": used,
            "limit_value": limit,
            "pct_used": pct,
            "is_unlimited": False,
            "unit_type": "currency",
            "currency": "USD",
            "account_label": self.account_label or "",
            "reset_at": reset_at.isoformat() if reset_at else None,
            "window_type": window_type,
            "provider_id": "opencode",
            "tier": "Go",
            "data_source": data_source,
            "input_source": input_source,
            "usage_url": "https://opencode.ai/console/usage",
            "updated_at": now_iso,
        }

    # --- web strategy -----------------------------------------------------

    async def _get_opencode_web(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Cookie-authenticated console handshake (2 steps)."""
        tokens, input_source = await self._get_credentials()
        cookie_session = credential_provider.get_provider_session_cookie(
            "opencode", account_id=self.credential_account_id
        )
        if cookie_session:
            input_source = "config"
        else:
            cookie_session = tokens.get("cookie_session") or tokens.get("session_cookie")
        console_session = tokens.get("console_session")
        if cookie_session and "__Host-console_session=" in cookie_session:
            for part in cookie_session.split(";"):
                part = part.strip()
                if part.startswith("__Host-console_session="):
                    console_session = part[len("__Host-console_session=") :].strip()
                elif part.startswith("auth="):
                    cookie_session = part[5:].strip()
        if not cookie_session or not console_session:
            self._set_error("missing_cookies")
            return []
        headers = self._build_cookie_headers(cookie_session, console_session)
        workspaces = await self._fetch_workspaces(client, headers)
        if not workspaces:
            if self._last_error_reason == "unknown":
                self._set_error("no_workspace")
            return []
        selected = credential_provider.get_opencode_workspace_id(
            account_id=self.credential_account_id
        )
        if selected:
            if selected not in {wid for wid, _ in workspaces}:
                self._set_error("invalid_config")
                return []
            candidates = [(selected, next((n for wid, n in workspaces if wid == selected), None))]
        else:
            candidates = workspaces
        usable: list[tuple[str, dict[str, Any]]] = []
        for workspace_id, _ in candidates:
            body = await self._fetch_go_status(client, headers, workspace_id)
            if body is not None:
                usable.append((workspace_id, body))
                if not selected and len(usable) > 1:
                    break
        if selected and not usable:
            if self._last_error_reason == "unknown":
                self._set_error("no_workspace")
            return []
        if not selected and len(usable) > 1:
            self._set_error("invalid_config")
            return []
        if not usable:
            if self._last_error_reason == "unknown":
                self._set_error("no_workspace")
            return []
        cards = self._build_cards_from_go_status(usable[0][1], input_source)
        if not cards:
            self._set_error("parse_error")
        else:
            self._last_error_reason = "unknown"
        return cards

    def _build_cookie_headers(
        self, cookie_session: str | None, console_session: str | None
    ) -> dict[str, str]:
        cookies: list[str] = []
        if cookie_session:
            cookies.append(f"auth={cookie_session}")
        if console_session:
            cookies.append(f"__Host-console_session={console_session}")
        return {
            "Cookie": "; ".join(cookies),
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Referer": "https://opencode.ai/console/",
            "Origin": "https://opencode.ai",
        }

    async def _fetch_workspaces(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> list[tuple[str, str | None]]:
        try:
            resp = await http_request_with_retry(
                client,
                "GET",
                f"{_BASE_URL}/console/api/orgs",
                headers=headers,
                timeout=15.0,
                follow_redirects=True,
            )
        except Exception as exc:
            self._set_error("api_unavailable")
            logger.warning("OpenCode: /console/api/orgs failed: %s", scrub_log(str(exc)))
            return []
        if resp.status_code in (401, 403):
            self._set_error("session_invalid")
            return []
        if resp.status_code != 200:
            if resp.status_code >= 500:
                self._set_error("api_error")
            return []
        try:
            orgs = resp.json()
        except Exception:
            self._set_error("parse_error")
            return []
        if not isinstance(orgs, list):
            self._set_error("parse_error")
            return []
        return [
            (org["id"], org.get("name"))
            for org in orgs
            if isinstance(org, dict) and isinstance(org.get("id"), str) and org["id"]
        ]

    async def _fetch_go_status(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        workspace_id: str,
    ) -> dict[str, Any] | None:
        # workspace_id header is required (CodexBar docs); without it the
        # server answers HTTP 400 {"_tag":"BadRequest"}.
        org_headers = dict(headers)
        org_headers["x-org-id"] = workspace_id
        try:
            resp = await http_request_with_retry(
                client,
                "GET",
                f"{_BASE_URL}/console/api/go/status",
                headers=org_headers,
                timeout=15.0,
                follow_redirects=True,
            )
        except Exception as exc:
            self._set_error("api_unavailable")
            logger.warning("OpenCode: /console/api/go/status failed: %s", scrub_log(str(exc)))
            return None
        if resp.status_code in (401, 403):
            self._set_error("session_invalid")
            return None
        if resp.status_code != 200:
            if resp.status_code >= 500:
                self._set_error("api_error")
            return None
        try:
            body = resp.json()
            if not isinstance(body, dict) or not (body.get("access") or {}).get("meters"):
                return None
            return body
        except Exception:
            self._set_error("parse_error")
            return None

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _microcents_to_usd(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            return int(raw) / 100_000_000
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_iso(s: str | None) -> datetime | None:
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            return None

    @staticmethod
    def _reset_label(window_type: str) -> str:
        return {"session": "5h", "weekly": "7d", "monthly": "30d"}.get(window_type, "—")
