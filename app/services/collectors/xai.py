"""
xAI (Grok) quota collector.

xAI's public API does not expose a programmatic quota endpoint — the
management console hides balance and team usage behind a Cloudflare-protected
session that isn't reusable from a server. So this collector is a thin
"token-status" stub:

- Reads the xai OAuth credentials that the opencode CLI stores in
  `~/.local/share/opencode/auth.json["xai"]` (access JWT + refresh token +
  `expires` epoch ms).
- Decodes the JWT's `exp` claim and surfaces the expiry status.
- On expiry, emits an `auth_required` error card pointing the user at the
  opencode CLI re-login flow (xai tokens don't have a programmatic refresh
  endpoint that Runway can hit — the opencode CLI is the source of truth).
- When the token is fresh, returns no card (no quota API to surface) and
  logs that the credential is healthy.

The sidecar's `_OC_CANONICAL_MAP` already retags opencode events whose
providerID is `xai` (or any unknown backend) onto a derived
`opencode-xai` provider_id; token attribution continues to work without
this collector emitting quota data.

When xAI exposes a real quota API later, swap this stub for a bearer-fetch
strategy (the same shape as `app/services/collectors/opencode.py`'s `api`
strategy) — the rest of the wiring (token cache, registry, sidecar rule)
already supports the upgrade.
"""

import base64
import json
import logging
from typing import Any

import httpx

from app.core.utils import error_card, scrub_log
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class XaiCollector(BaseCollector):
    PROVIDER_ID = "xai"
    DEFAULT_WINDOW_TYPE = "monthly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        # No api strategy yet — xai doesn't expose programmatic quota.
        # Placeholder so the base collector's strategy dispatch works
        # once an endpoint is wired up.
        "stub": ("Token status (stub)", "_get_xai_token_status"),
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self._last_error_reason: str = "unknown"

    async def is_configured(self) -> bool:
        """xai is configured when the opencode-sidecar pushed any xai token.

        We deliberately accept any of the xai-related token slots — the
        sidecar rule reads the whole ``{"type":"oauth","refresh":...,
        "access":...,"expires":...}`` block from ``auth.json`` and
        stores each field under its own key.
        """
        acc = self.account_id or "default"
        for token_type in ("xai_access", "xai_refresh", "xai_oauth", "api_key"):
            if await token_cache.get_token("xai", token_type, account_id=acc):
                return True
        return False

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return await self._get_xai_token_status(client)

    def _fallback_strategies(self) -> list[Any]:
        return []

    def _is_error_result(self, results: list[dict[str, Any]]) -> bool:
        """An empty list is the healthy state for xai — no quota API to
        surface, just a token-status check. The base collector treats
        ``[]`` as an error and would otherwise emit a generic "Check
        State" card; we explicitly opt out so the dashboard stays clean
        when the credential is fresh and the only signal is "no quota
        to show" rather than "something is wrong"."""
        return any(r.get("remaining") == "ERR" for r in results)

    async def _get_xai_token_status(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Decode the access JWT and emit an auth_required card if expired.

        xAI doesn't expose a programmatic quota endpoint, so this strategy
        is intentionally minimal: it surfaces a single health signal
        ("your xai credential is expired") instead of a blank dashboard.
        """
        tokens = await token_cache.get("xai", account_id=self.account_id or "default") or {}
        access = tokens.get("xai_access") or tokens.get("access")
        if not access:
            return []  # No credential at all — base collector's _error_handler picks this up.
        exp_ms = self._extract_exp_ms(access, tokens.get("xai_expires") or tokens.get("expires"))
        if exp_ms is None:
            return []  # Unparseable JWT — don't surface false positives.
        # The JWT exp can be in seconds or milliseconds depending on the issuer
        # (the opencode CLI stores ms in `expires`, but some tokens carry s).
        exp_s = exp_ms / 1000 if exp_ms > 1e12 else exp_ms
        from datetime import UTC, datetime

        if exp_s <= datetime.now(UTC).timestamp():
            self._last_error_reason = "invalid_api_key"
            return [
                error_card(
                    "xAI",
                    "🤖",
                    # 40-char cap (truncate_string in LimitCardBuilder.error).
                    "xAI expired — re-login opencode",
                    error_type="auth_failed",
                    provider_id=self.PROVIDER_ID,
                )
            ]
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        reason = self._last_error_reason
        if reason == "invalid_api_key":
            message = "xAI expired — re-login opencode"
            error_type = "auth_failed"
        else:
            message = "xAI token status check failed."
            error_type = "unknown"
        return [error_card("xAI", "🤖", message, error_type=error_type)]

    @staticmethod
    def _extract_exp_ms(access: str, explicit_expires: Any) -> int | None:
        """Return the JWT `exp` claim in milliseconds, or fall back to the
        ``expires`` field the opencode CLI stores alongside the token.
        Returns ``None`` if the token isn't a JWT and no explicit expiry
        is available.
        """
        if explicit_expires is not None:
            try:
                return int(explicit_expires)
            except (TypeError, ValueError):
                pass
        parts = access.split(".")
        if len(parts) < 2:
            return None
        try:

            def pad(s: str) -> str:
                return s + "=" * (-len(s) % 4)

            payload = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
            exp = payload.get("exp")
            if exp is None:
                return None
            # Multiply seconds up to ms when the value is clearly seconds.
            return int(exp) * 1000 if int(exp) < 1e12 else int(exp)
        except Exception as exc:
            logger.debug("xAI: failed to decode JWT exp: %s", scrub_log(str(exc)))
            return None
