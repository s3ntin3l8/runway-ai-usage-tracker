"""
xAI (Grok) quota collector.

Collection Strategies:
- ``api`` (PRIMARY): bearer ``Authorization: Bearer <oauth>`` + the
  ``x-xai-token-auth: xai-grok-cli`` header against the Grok CLI-proxy
  billing REST API:
    - ``GET https://cli-chat-proxy.grok.com/v1/billing?format=credits``
      returns the user's current billing period (weekly or monthly),
      ``creditUsagePercent``, on-demand usage, and per-product
      percentages. This is the quota gauge.
    - ``GET https://cli-chat-proxy.grok.com/v1/settings`` returns
      ``subscription_tier_display`` (``SuperGrok`` vs ``SuperGrok Heavy``)
      and identity hints. Best-effort enrichment.

The ``oc_sk_…`` API key surface (https://api.x.ai/v1/…) is *not* the same
auth — it serves the developer API, not the consumer/Grok subscription.
Runway uses the OAuth access token that the opencode CLI stores in
``~/.local/share/opencode/auth.json["xai"]["access"]`` (auto-extracted
by the sidecar). Tokens expire after ~7 days; refresh is handled by
the CLI itself (``grok login``), not Runway — when the access JWT is
expired the collector surfaces an ``auth_required`` card pointing the
operator at the opencode CLI re-login flow.

The CodexBar docs (https://github.com/steipete/CodexBar/blob/main/docs/grok.md)
document a richer fallback chain (``grok agent stdio`` ACP JSON-RPC,
``grok.com`` gRPC-web with WKE keypair, browser cookies). Runway sticks
to the OAuth-bearer CLI-proxy path because the other surfaces require a
local ``grok`` CLI binary or a browser-held WKE keypair the sidecar can't
reasonably obtain. If xAI exposes an HTTP endpoint that proxies
``grok.com`` billing, this collector gains a cookie path the same way
the opencode collector gained one.
"""

import base64
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.utils import PaceCalculator, error_card, http_request_with_retry, scrub_log
from app.services.collectors.base import BaseCollector
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


class XaiCollector(BaseCollector):
    PROVIDER_ID = "xai"
    DEFAULT_WINDOW_TYPE = "monthly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("OAuth bearer (cli-chat-proxy)", "_get_xai_api"),
    }

    CLI_CHAT_PROXY_BASE = "https://cli-chat-proxy.grok.com"
    BILLING_URL = f"{CLI_CHAT_PROXY_BASE}/v1/billing?format=credits"
    SETTINGS_URL = f"{CLI_CHAT_PROXY_BASE}/v1/settings"

    # CodexBar-parity mapping of `currentPeriod.type` -> Runway canonical
    # ``window_type``. ``WEEKLY`` -> ``weekly``, ``MONTHLY`` -> ``monthly``,
    # anything else falls back to ``monthly`` (xAI doesn't publish a 5h
    # rolling window).
    _PERIOD_WINDOW_TYPE: dict[str, str] = {
        "USAGE_PERIOD_TYPE_WEEKLY": "weekly",
        "USAGE_PERIOD_TYPE_MONTHLY": "monthly",
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self._last_error_reason: str = "unknown"
        self._plan_tier: str | None = None  # from /v1/settings (best-effort enrichment)

    async def is_configured(self) -> bool:
        """xAI needs an access bearer; refresh-only credentials aren't consumed here."""
        acc = self.account_id or "default"
        return bool(await token_cache.get_token("xai", "xai_access", account_id=acc))

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return await self._get_xai_api(client)

    def _fallback_strategies(self) -> list[Any]:
        return []

    async def _get_xai_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Bearer OAuth path: ``/v1/billing`` + optional ``/v1/settings``."""
        access = await token_cache.get_token(
            "xai", "xai_access", account_id=self.account_id or "default"
        )
        if not access:
            return []
        access = access.strip() if isinstance(access, str) else access

        # Token-status gate: emit an empty list (no quota cards) when the
        # access JWT is expired; ``BaseCollector.collect`` then runs the
        # ``_error_handler`` which emits the auth_required card. We return
        # ``[]`` here so the dashboard isn't blank — the error message
        # carries the actionable fix.
        if self._is_expired(access):
            self._last_error_reason = "invalid_api_key"
            return []

        headers = {
            "Authorization": f"Bearer {access}",
            "x-xai-token-auth": "xai-grok-cli",
            "Accept": "application/json",
            "User-Agent": "runway-ai-usage-tracker/1.0",
        }

        # Best-effort: plan tier from /v1/settings. Failure here doesn't
        # block quota — we just render the card without a tier badge.
        self._plan_tier = await self._fetch_plan_tier(client, headers)

        try:
            resp = await http_request_with_retry(
                client,
                "GET",
                self.BILLING_URL,
                headers=headers,
                timeout=15.0,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            logger.warning("xAI billing fetch timed out: %s", scrub_log(str(exc)))
            return []
        except Exception as exc:
            logger.warning("xAI billing fetch failed: %s", scrub_log(str(exc)))
            return []

        if resp.status_code in (401, 403):
            self._last_error_reason = "invalid_api_key"
            return []
        if resp.status_code != 200:
            return []
        try:
            body = resp.json()
        except Exception:
            self._last_error_reason = "parse_error"
            return []

        cards = self._build_cards_from_billing(body)
        if not cards:
            self._last_error_reason = "parse_error"
        return cards

    async def _fetch_plan_tier(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> str | None:
        """Best-effort enrichment: ``GET /v1/settings`` -> ``subscription_tier_display``.

        Never blocks quota — a timeout or 5xx silently degrades the card
        to no tier badge, which is the same as the pre-existing behavior
        for the other collectors' enrichment paths.
        """
        try:
            resp = await http_request_with_retry(
                client,
                "GET",
                self.SETTINGS_URL,
                headers=headers,
                timeout=5.0,
                follow_redirects=True,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            if not isinstance(body, dict):
                return None
            tier = body.get("subscription_tier_display")
            return tier.strip() if isinstance(tier, str) and tier.strip() else None
        except (httpx.TimeoutException, Exception):
            return None

    def _build_cards_from_billing(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the quota card(s) from ``/v1/billing`` ``config`` block.

        Shape (live):

            {
              "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY",
                                   "start": "...",
                                   "end":   "..."},
                "creditUsagePercent": 2.0,
                "onDemandCap":  {"val": 0},
                "onDemandUsed": {"val": 0},
                "productUsage": [{"product": "GrokBuild", "usagePercent": 2.0}, ...],
                "isUnifiedBillingUser": true,
                "prepaidBalance": {"val": 0},
                "billingPeriodStart": "...",
                "billingPeriodEnd":   "..."
              }
            }

        ``creditUsagePercent`` is the included-credit gauge. On-demand usage
        is shown as a separate "OnDemand" card when the user has a non-zero
        cap (xAI's "SuperGrok Heavy" plan allows pay-as-you-go overage;
        the proxy surfaces ``onDemandCap.val`` and ``onDemandUsed.val`` as
        microcents).
        """
        config = (body or {}).get("config") or {}
        if not isinstance(config, dict):
            return []
        period = config.get("currentPeriod") or {}
        period_type = (period.get("type") or "").strip()
        window_type = self._PERIOD_WINDOW_TYPE.get(period_type, "monthly")
        reset_at = self._parse_iso(period.get("end"))
        now = datetime.now(UTC)
        now_iso = now.isoformat()

        cards: list[dict[str, Any]] = []
        pct = self._percent_or_none(config.get("creditUsagePercent"))
        if pct is not None:
            cards.append(
                self._build_credits_card(
                    pct=pct,
                    used_label="Credits used",
                    window_type=window_type,
                    reset_at=reset_at,
                    now_iso=now_iso,
                )
            )

        # On-demand card — only when a non-zero cap exists and we have both
        # used and cap values. microcents -> USD via /100_000_000 (parity
        # with the opencode collector).
        on_demand = self._build_on_demand_card(config, window_type, reset_at, now_iso)
        if on_demand:
            cards.append(on_demand)

        if not cards:
            self._last_error_reason = "parse_error"
        return cards

    def _build_on_demand_card(
        self,
        config: dict[str, Any],
        window_type: str,
        reset_at: datetime | None,
        now_iso: str,
    ) -> dict[str, Any] | None:
        cap_raw = (
            (config.get("onDemandCap") or {}).get("val")
            if isinstance(config.get("onDemandCap"), dict)
            else None
        )
        used_raw = (
            (config.get("onDemandUsed") or {}).get("val")
            if isinstance(config.get("onDemandUsed"), dict)
            else None
        )
        if cap_raw is None or used_raw is None:
            return None
        try:
            cap = int(cap_raw)
            used = int(used_raw)
        except (TypeError, ValueError):
            return None
        if cap <= 0:
            return None  # plan has no on-demand; skip the card entirely
        pct = min(100.0, used / cap * 100.0) if cap else 0.0
        limit_usd = cap / 100_000_000
        used_usd = used / 100_000_000
        return self._build_currency_card(
            pct=pct,
            used_usd=used_usd,
            limit_usd=limit_usd,
            window_type=window_type,
            reset_at=reset_at,
            now_iso=now_iso,
            detail=f"${used_usd:.2f} of ${limit_usd:.2f} on-demand used",
        )

    def _build_credits_card(
        self,
        pct: float,
        used_label: str,
        window_type: str,
        reset_at: datetime | None,
        now_iso: str,
    ) -> dict[str, Any]:
        return self._build_card_dict(
            {
                "remaining": f"{max(0.0, 100 - pct):.1f}%",
                "unit": "remaining",
                "pct": pct,
                "used_value": pct,
                "limit_value": 100.0,
                "unit_type": "percent",
                "currency": None,
                "detail": used_label,
                "window_type": window_type,
                "reset_at": reset_at,
                "now_iso": now_iso,
            }
        )

    def _build_currency_card(
        self,
        pct: float,
        used_usd: float,
        limit_usd: float,
        window_type: str,
        reset_at: datetime | None,
        now_iso: str,
        detail: str,
    ) -> dict[str, Any]:
        return self._build_card_dict(
            {
                "remaining": f"${max(0.0, limit_usd - used_usd):.2f}",
                "unit": "on-demand",
                "pct": pct,
                "used_value": used_usd,
                "limit_value": limit_usd,
                "unit_type": "currency",
                "currency": "USD",
                "detail": detail,
                "window_type": window_type,
                "reset_at": reset_at,
                "now_iso": now_iso,
            }
        )

    def _build_card_dict(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Build the card dict from a context mapping.

        Accepting a dict (rather than keyword args) keeps the function
        signature below the PLR0913 10-arg cap and lets the callers
        express the card shape in one literal — useful when adding new
        window types or metrics in the future.
        """
        pct: float = ctx["pct"]
        reset_at: datetime | None = ctx["reset_at"]
        if pct >= 90:
            health = "critical"
        elif pct >= 70:
            health = "warning"
        else:
            health = "good"
        return {
            "service_name": "xAI",
            "icon": "🤖",
            "remaining": ctx["remaining"],
            "unit": ctx["unit"],
            "reset": PaceCalculator.estimate_longevity(pct, reset_at) if reset_at else "—",
            "health": health,
            "pace": PaceCalculator.estimate_longevity(pct, reset_at) if reset_at else "—",
            "detail": ctx["detail"],
            "used_value": ctx["used_value"],
            "limit_value": ctx["limit_value"],
            "pct_used": pct,
            "is_unlimited": False,
            "unit_type": ctx["unit_type"],
            "currency": ctx["currency"],
            "reset_at": reset_at.isoformat() if reset_at else None,
            "account_label": self.account_label or "",
            "window_type": ctx["window_type"],
            "provider_id": "xai",
            "tier": self._plan_tier,
            "data_source": self.DATA_SOURCE_API,
            "input_source": "sidecar",
            "usage_url": "https://console.x.ai",
            "updated_at": ctx["now_iso"],
        }

    async def _error_handler(self) -> list[dict[str, Any]]:
        reason = self._last_error_reason
        if reason == "invalid_api_key":
            message = "xAI session expired — re-login opencode CLI"
            error_type = "auth_failed"
        else:
            message = "xAI quota collection failed."
            error_type = "unknown"
        return [error_card("xAI", "🤖", message, error_type=error_type)]

    def _is_expired(self, access: str) -> bool:
        """JWT ``exp`` claim check. Same format as before — seconds since
        epoch or milliseconds for tokens carrying the opencode-CLI flavor.
        Returns False when we can't decode (no false positives on malformed
        tokens; we let the real call's 401 surface the failure)."""
        exp_ms = self._extract_exp_ms(access, None)
        if exp_ms is None:
            return False
        exp_s = exp_ms / 1000 if exp_ms > 1e12 else exp_ms
        return exp_s <= datetime.now(UTC).timestamp()

    @staticmethod
    def _percent_or_none(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_exp_ms(access: str, explicit_expires: Any) -> int | None:
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
            return int(exp) * 1000 if int(exp) < 1e12 else int(exp)
        except Exception as exc:
            logger.debug("xAI: failed to decode JWT exp: %s", scrub_log(str(exc)))
            return None

    @staticmethod
    def _parse_iso(s: str | None):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)
        except Exception:
            return None
