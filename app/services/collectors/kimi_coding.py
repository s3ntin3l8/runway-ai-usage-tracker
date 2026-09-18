"""
Kimi Coding (Kimi For Coding) quota collector.

Collection Strategies (both standalone-capable, UI-reorderable):
- api:  GET {base}/coding/v1/usages — Kimi Code API key (DB > env > CLI credential)
- web:  POST www.kimi.com/apiv2/.../GetUsages + GetSubscriptionStats +
        GetSubscription — kimi-auth cookie

Strategy merge semantics (see collect()): strategies run in their resolved
(user-reorderable) order; the FIRST successful strategy provides the base
cards and every later success enriches it — adding windows the base lacks
(the Code API omits the weekly window and the plan title), filling the tier
badge, and upgrading ratio-only cards to real counts when available.

Live response notes (verified 2026-09-18 against a Pro plan):
- Code API returns `limits[]` (5h counts), ratio pools under `usages`
  (`limit_5h`, `limit_7d`, `limit_month_total`, `limit_month_code`) and
  `booster_wallet`. `limit_5h.used_ratio` demonstrably lags the counts
  (0% vs a real 47%) — counts always win for the 5h window.
- Pro plan (GOODS_VERSION_V2): 5h session limit 100, weekly limit 100,
  monthly credit pool. NO `usage` (weekly counts), `user`, or `version`
  fields — tier only comes from the web GetSubscription goods.title.
- CLI credential (~/.kimi-code/credentials/kimi-code.json) is read-only;
  the refresh token is never used. A token is fresh when
  expires_at > now + 60s (CodexBar parity).

See Also:
- kimi_api.py for Moonshot Open Platform balance (different service)
- docs/collectors/kimi_coding.md

Error Handling:
- No auth: Returns error card
- API errors: Returns error card (401 with explicit API key = invalid key)
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.core.date_utils import parse_iso8601_utc
from app.core.utils import error_card, http_request_with_retry, human_delta
from app.services.collectors.base import BaseCollector
from app.services.credential_provider import credential_provider
from app.services.token_cache import token_cache

# GOODS_VERSION_V1 membership-level -> display name (CodexBar parity). V2 goods
# carry real titles and are taken verbatim from GetSubscription instead.
_MEMBERSHIP_LEVEL_NAMES_V1 = {
    "LEVEL_FREE": "Adagio",
    "LEVEL_TRIAL": "Andante",
    "LEVEL_BASIC": "Moderato",
    "LEVEL_INTERMEDIATE": "Allegretto",
    "LEVEL_ADVANCED": "Allegro",
}

_WEB_BASE = "https://www.kimi.com/apiv2"
_USAGE_URL = f"{_WEB_BASE}/kimi.gateway.billing.v1.BillingService/GetUsages"
_SUBSCRIPTION_STATS_URL = (
    f"{_WEB_BASE}/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats"
)
_SUBSCRIPTION_URL = f"{_WEB_BASE}/kimi.gateway.membership.v2.MembershipService/GetSubscription"


class KimiCodingCollector(BaseCollector):
    """Collector for Kimi Coding quotas (5h session / weekly / monthly)."""

    PROVIDER_ID = "kimi_coding"
    DEFAULT_WINDOW_TYPE = "weekly"

    STRATEGIES: dict[str, tuple[str, str] | tuple[str, str, dict]] = {
        "api": ("Code API (key/CLI)", "_strategy_code_api"),
        "web": ("Web (cookie)", "_strategy_web"),
    }

    USAGE_URL = "https://www.kimi.com/code/console"

    _WEB_HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.kimi.com/code/console",
        "Origin": "https://www.kimi.com",
        "connect-protocol-version": "1",
        "x-msh-platform": "web",
    }

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self._ephemeral_device_id: str | None = None
        self._api_key_auth_failed = False

    async def is_configured(self) -> bool:
        """True when any credential source is present (API key, CLI token, cookie)."""
        return (
            await self._resolve_code_bearer() is not None
            or await self._resolve_cookie() is not None
        )

    # ------------------------------------------------------------------
    # Strategy orchestration: first success = base, later = enrichment.
    # ------------------------------------------------------------------

    async def collect(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        strategies = self._resolve_strategies()
        if not strategies:
            return await super().collect(client)

        cards: dict[tuple[str | None, str | None], dict[str, Any]] = {}
        tier: str | None = None
        best_error: dict[str, Any] | None = None

        for method, s_id in strategies:
            try:
                results = await method(client)
            except Exception as e:
                logger.warning("Kimi Coding strategy %s failed: %s", s_id, e)
                self._record_strategy_error(e)
                continue

            # An explicit API key that gets rejected is authoritative — surface
            # the invalid-key error instead of silently falling back to web.
            if (
                s_id == "api"
                and results
                and results[0].get("error_type") == "auth_failed"
                and self._api_key_auth_failed
            ):
                return self._tag_results([results[0]])

            if self._is_error_result(results):
                if results:
                    err_card = results[0]
                    prio = self.ERROR_PRIORITY.get(err_card.get("error_type", "unknown"), 0)
                    best_prio = (
                        self.ERROR_PRIORITY.get(best_error.get("error_type", "unknown"), -1)
                        if best_error
                        else -1
                    )
                    if prio > best_prio:
                        best_error = err_card
                continue

            for card in results:
                if card.get("tier") and tier is None:
                    tier = card["tier"]
                key = (card.get("window_type"), card.get("variant"))
                existing = cards.get(key)
                if existing is None:
                    cards[key] = card
                elif existing.get("used_value") is None and card.get("used_value") is not None:
                    # Real request counts beat ratio-only percentages.
                    cards[key] = card

        results = list(cards.values())
        if tier:
            for card in results:
                card.setdefault("tier", tier)

        if not results:
            results = [best_error] if best_error else await self._error_handler()
        return self._tag_results(results)

    # ------------------------------------------------------------------
    # Legacy abstract hooks (unused — STRATEGIES path drives collect()).
    # ------------------------------------------------------------------

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        return []

    def _fallback_strategies(self) -> list[Any]:
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return fallback error when nothing could collect."""
        if not await self._resolve_code_bearer() and not await self._resolve_cookie():
            return [
                error_card(
                    "Kimi Coding",
                    "🌙",
                    "No Auth (set KIMI_CODE_API_KEY, sign in with Kimi Code CLI, or paste kimi-auth cookie)",
                    error_type="missing_config",
                )
            ]
        return [error_card("Kimi Coding", "🌙", "API Collection Failed", error_type="api_error")]

    # ------------------------------------------------------------------
    # Credential resolution
    # ------------------------------------------------------------------

    async def _resolve_code_bearer(self) -> tuple[str, str, bool] | None:
        """
        Resolve a Bearer token for the Code API, plus its input source.

        Priority: DB API key (config) > KIMI_CODE_API_KEY env > Kimi Code CLI
        access token (local file or sidecar-pushed), which must be fresh
        (expires_at > now + 60s). Returns (token, input_source, is_cli).
        """
        key = credential_provider.get_provider_api_key("kimi_coding")
        if self._is_valid_credential(key):
            return key, self.INPUT_SOURCE_CONFIG, False  # type: ignore[return-value]

        if self._is_valid_credential(settings.KIMI_CODE_API_KEY):
            return settings.KIMI_CODE_API_KEY, self.INPUT_SOURCE_SERVER, False

        cli = await self._resolve_cli_token()
        if cli:
            return cli[0], cli[1], True
        return None

    async def _resolve_cli_token(self) -> tuple[str, str] | None:
        """
        Read-only access token from the Kimi Code CLI credential file.

        Local topology: parsed server-side from ~/.kimi-code/credentials/kimi-code.json
        via the registry file rule. Multi-host: pushed by the sidecar into the
        token cache. The refresh token is never used (CodexBar parity — the
        official CLI owns the refresh flow; when the token lapses, re-login).
        """
        candidates: list[tuple[str, Any, str]] = []

        # Local file (registry rule) — access_token + expires_at together.
        try:
            creds = credential_provider.get_credentials("kimi_coding")
            if creds.get("cli_access_token"):
                candidates.append(
                    (
                        creds["cli_access_token"],
                        creds.get("cli_expires_at"),
                        creds.sources.get("cli_access_token", "server"),
                    )  # type: ignore[arg-type]
                )
        except Exception:
            logger.debug("Kimi Code CLI credential file unreadable", exc_info=True)

        # Sidecar-pushed token cache.
        try:
            cache_data = await token_cache.get_with_metadata(
                "kimi_coding", account_id=self.account_id or "default"
            )
            if cache_data:
                value, meta = cache_data
                token = (value or {}).get("cli_access_token")
                if token:
                    candidates.append((token, (value or {}).get("cli_expires_at"), "sidecar"))
        except Exception:
            logger.debug("Kimi Code CLI token cache unreadable", exc_info=True)

        for token, expires_at, source in candidates:
            if self._is_cli_token_fresh(expires_at):
                return token, source
        return None

    @staticmethod
    def _is_cli_token_fresh(expires_at: Any) -> bool:
        """expires_at (epoch seconds, int/float/str) must be > now + 60s."""
        try:
            exp = float(expires_at)
        except (TypeError, ValueError):
            return False
        return exp > datetime.now(UTC).timestamp() + 60

    async def _resolve_cookie(self) -> tuple[str, str] | None:
        """
        Resolve the kimi-auth web cookie.

        Priority: DB-stored session cookie (manual override) > KIMI_AUTH_TOKEN env
        > sidecar-pushed browser cookie.
        """
        db_token = credential_provider.get_provider_session_cookie("kimi_coding")
        if self._is_valid_credential(db_token):
            return db_token, self.INPUT_SOURCE_CONFIG  # type: ignore[return-value]

        if self._is_valid_credential(settings.KIMI_AUTH_TOKEN):
            return settings.KIMI_AUTH_TOKEN, self.INPUT_SOURCE_SERVER

        cache_data = await token_cache.get_with_metadata(
            "kimi_coding", account_id=self.account_id or "default"
        )
        if cache_data:
            value, _meta = cache_data
            token = (value or {}).get("cookie_kimi-auth") or (value or {}).get("session_cookie")
            if self._is_valid_credential(token):
                return token, self.INPUT_SOURCE_SIDECAR
        return None

    def _kimi_code_identity_headers(self) -> dict[str, str]:
        """X-Msh-* identity headers for CLI-credential calls (CodexBar parity)."""
        device_id = self._read_cli_device_id() or self._ephemeral_device_id
        if not device_id:
            device_id = uuid.uuid4().hex
            self._ephemeral_device_id = device_id
        return {
            "X-Msh-Platform": "kimi_code_cli",
            "X-Msh-Device-Id": device_id,
        }

    @staticmethod
    def _read_cli_device_id() -> str | None:
        """Read-only: use the official CLI's device_id if present, else None."""
        from pathlib import Path

        path = Path.home() / ".kimi-code" / "device_id"
        try:
            value = path.read_text(encoding="utf-8").strip()
            return value or None
        except OSError:
            return None

    # ------------------------------------------------------------------
    # api strategy — GET {base}/coding/v1/usages
    # ------------------------------------------------------------------

    async def _strategy_code_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect via the Kimi Code API (API key or CLI access token)."""
        self._api_key_auth_failed = False
        resolved = await self._resolve_code_bearer()
        if resolved is None:
            return []
        token, input_source, is_cli = resolved

        base = (settings.KIMI_CODE_BASE_URL or "https://api.kimi.com").rstrip("/")
        endpoint = f"{base}/coding/v1/usages"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if is_cli:
            headers.update(self._kimi_code_identity_headers())

        try:
            resp = await http_request_with_retry(
                client, "GET", endpoint, headers=headers, timeout=10.0
            )
        except (httpx.RequestError, ValueError):
            return []

        if resp.status_code == 401 and not is_cli:
            self._api_key_auth_failed = True
            return [
                error_card(
                    "Kimi Coding",
                    "🌙",
                    "Invalid API Key (401) — check the key from kimi.com/code/console",
                    error_type="auth_failed",
                )
            ]
        if resp.status_code != 200:
            return []

        try:
            data = resp.json()
        except ValueError:
            return []
        return self._parse_code_api_response(data, input_source)

    def _parse_code_api_response(
        self, data: dict[str, Any], input_source: str
    ) -> list[dict[str, Any]]:
        """
        Parse the Code API usage response into cards.

        Handles both shapes: legacy counts (`usage` weekly + `limits[]` 5h) and
        ratio pools (`usages.limit_5h/limit_7d/limit_month_total/limit_month_code`).
        Counts take precedence for the 5h window (the `limit_5h` ratio lags).
        """
        cards: list[dict[str, Any]] = []

        # Legacy weekly counts.
        weekly_detail = data.get("usage") or {}
        weekly_card = self._card_from_detail(
            weekly_detail, "weekly", input_source, label="Weekly quota"
        )
        if weekly_card:
            cards.append(weekly_card)

        # 5h window counts from `limits[]` (present in both old and new shapes).
        rate_limit = None
        for limit in data.get("limits") or []:
            window = limit.get("window") or {}
            if (window.get("duration"), window.get("timeUnit")) == (300, "TIME_UNIT_MINUTE"):
                rate_limit = limit.get("detail") or {}
                break
        if rate_limit is None and data.get("limits"):
            rate_limit = (data["limits"][0] or {}).get("detail") or {}
        session_card = self._card_from_detail(
            rate_limit, "session", input_source, icon="⏱️", label="5h rate limit"
        )
        if session_card:
            cards.append(session_card)

        # Ratio pools — monthly variants, weekly fallback, 5h only if no counts.
        pools = data.get("usages") or {}
        if isinstance(pools, dict):
            if weekly_card is None:
                weekly_card = self._card_from_ratio(
                    pools.get("limit_7d"), "weekly", input_source, label="Weekly quota"
                )
                if weekly_card:
                    cards.append(weekly_card)

            monthly_total = self._card_from_ratio(
                pools.get("limit_month_total"),
                "monthly",
                input_source,
                label="Monthly Total",
                variant="total",
            )
            if monthly_total:
                cards.append(monthly_total)

            monthly_code = self._card_from_ratio(
                pools.get("limit_month_code"),
                "monthly",
                input_source,
                label="Monthly Code",
                variant="code",
            )
            if monthly_code:
                cards.append(monthly_code)

            if session_card is None:
                session_card = self._card_from_ratio(
                    pools.get("limit_5h"),
                    "session",
                    input_source,
                    icon="⏱️",
                    label="5h rate limit",
                )
                if session_card:
                    cards.append(session_card)

        for card in cards:
            card["data_source"] = self.DATA_SOURCE_API

        tier = self._tier_from_membership(data)
        if tier:
            for card in cards:
                card["tier"] = tier

        return cards

    def _tier_from_membership(self, data: dict[str, Any]) -> str | None:
        """membership level -> tier name. V1 goods map to canonical names; a V2
        or unknown version means the enum is meaningless — return the raw level."""
        user = data.get("user") or {}
        membership = user.get("membership") or {}
        level = str(membership.get("level") or "").strip()
        if not level or level == "LEVEL_UNSPECIFIED":
            return None
        version = data.get("version")
        if version not in (None, "GOODS_VERSION_V1"):
            return level
        return _MEMBERSHIP_LEVEL_NAMES_V1.get(level, level)

    # ------------------------------------------------------------------
    # web strategy — cookie: GetUsages + GetSubscriptionStats + GetSubscription
    # ------------------------------------------------------------------

    async def _strategy_web(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect via the kimi web gateway (cookie auth). Standalone-capable."""
        resolved = await self._resolve_cookie()
        if resolved is None:
            return []
        token, input_source = resolved
        headers = {**self._WEB_HEADERS, "Authorization": f"Bearer {token}"}

        # Primary: GetUsages (5h counts + weekly counts).
        usage_data: dict[str, Any] = {}
        try:
            resp = await http_request_with_retry(
                client,
                "POST",
                _USAGE_URL,
                headers=headers,
                json={"scope": ["FEATURE_CODING"]},
                timeout=10.0,
            )
            if resp.status_code in (401, 403):
                return [
                    error_card(
                        "Kimi Coding",
                        "🌙",
                        "Kimi auth token invalid or expired — re-login at kimi.com/code",
                        error_type="auth_failed",
                    )
                ]
            if resp.status_code == 200:
                usage_data = resp.json()
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            pass

        # Enrichment: subscription stats (accurate 5h/weekly ratios, monthly pool).
        stats_data: dict[str, Any] = {}
        try:
            resp = await http_request_with_retry(
                client, "POST", _SUBSCRIPTION_STATS_URL, headers=headers, json={}, timeout=10.0
            )
            if resp.status_code == 200:
                stats_data = resp.json()
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            pass

        # Enrichment: subscription (plan title -> tier). Never fatal.
        plan_title: str | None = None
        try:
            resp = await http_request_with_retry(
                client, "POST", _SUBSCRIPTION_URL, headers=headers, json={}, timeout=5.0
            )
            if resp.status_code == 200:
                plan_title = self._plan_title_from_subscription(resp.json())
        except (httpx.RequestError, ValueError, KeyError, TypeError):
            pass

        cards = self._parse_web_response(usage_data, stats_data, input_source)
        if plan_title:
            for card in cards:
                card["tier"] = plan_title
        return cards

    @staticmethod
    def _plan_title_from_subscription(data: dict[str, Any]) -> str | None:
        subscription = data.get("subscription") or {}
        if subscription.get("active") is not True:
            return None
        if subscription.get("status") != "SUBSCRIPTION_STATUS_ACTIVE":
            return None
        title = (subscription.get("goods") or {}).get("title")
        title = str(title or "").strip()
        return title or None

    def _parse_web_response(
        self,
        usage_data: dict[str, Any],
        stats_data: dict[str, Any],
        input_source: str,
    ) -> list[dict[str, Any]]:
        """Merge GetUsages counts with GetSubscriptionStats ratios into cards."""
        cards: list[dict[str, Any]] = []

        usages = usage_data.get("usages") or []
        coding = None
        for u in usages:
            if u.get("scope") == "FEATURE_CODING":
                coding = u
                break
        if coding is None and usages:
            coding = usages[0]

        if not usage_data.get("usages") and not stats_data:
            return [
                {
                    "service_name": "Kimi Coding",
                    "icon": "🌙",
                    "remaining": "No active plan",
                    "unit": "quota",
                    "reset": "—",
                    "health": "good",
                    "pace": "N/A",
                    "detail": "No active plan",
                    "data_source": self.DATA_SOURCE_WEB,
                    "input_source": input_source,
                    "is_unlimited": False,
                    "unit_type": "unknown",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            ]

        # 5h session: real counts from GetUsages (the stats ratio is a cross-check).
        rate_detail = None
        if coding:
            limits = coding.get("limits") or []
            if limits:
                rate_detail = (limits[0] or {}).get("detail") or {}
        session_card = self._card_from_detail(
            rate_detail, "session", input_source, icon="⏱️", label="5h rate limit"
        )
        if session_card:
            cards.append(session_card)

        # Weekly: prefer the dedicated ratelimitCode7d ratio (the GetUsages
        # weekly detail's `used` is often absent, showing a misleading 0%).
        weekly_card = None
        rl7d = stats_data.get("ratelimitCode7d") or {}
        if rl7d.get("enabled", True) and rl7d.get("ratio") is not None:
            weekly_card = self._card_from_ratio(
                {"used_ratio": rl7d.get("ratio"), "reset_time": rl7d.get("resetTime")},
                "weekly",
                input_source,
                label="Weekly quota",
            )
        if weekly_card is None and coding:
            weekly_card = self._card_from_detail(
                coding.get("detail") or {}, "weekly", input_source, label="Weekly quota"
            )
        if weekly_card:
            cards.append(weekly_card)

        # Monthly: subscription credit pool, total + coding-specific variants.
        balance = stats_data.get("subscriptionBalance") or {}
        monthly_total = self._card_from_ratio(
            {
                "used_ratio": balance.get("amountUsedRatio"),
                "reset_time": balance.get("expireTime"),
            },
            "monthly",
            input_source,
            label="Monthly Total",
            variant="total",
        )
        if monthly_total:
            cards.append(monthly_total)

        monthly_code = self._card_from_ratio(
            {
                "used_ratio": balance.get("kimiCodeUsedRatio"),
                "reset_time": balance.get("expireTime"),
            },
            "monthly",
            input_source,
            label="Monthly Code",
            variant="code",
        )
        if monthly_code:
            cards.append(monthly_code)

        return (
            cards
            if cards
            else [error_card("Kimi Coding", "🌙", "No Quota Data", error_type="parse_error")]
        )

    # ------------------------------------------------------------------
    # Card builders
    # ------------------------------------------------------------------

    @staticmethod
    def _detail_value(detail: dict[str, Any], *keys: str) -> Any:
        """First present value among resetTime/resetAt/reset_time/reset_at-style aliases."""
        for key in keys:
            if detail.get(key) is not None:
                return detail[key]
        return None

    def _card_from_detail(
        self,
        detail: dict[str, Any] | None,
        window_type: str,
        input_source: str,
        icon: str = "🌙",
        label: str = "Quota",
        variant: str | None = None,
        data_source: str = "web",
    ) -> dict[str, Any] | None:
        """Count-based card from a {limit, used, remaining, resetTime} detail."""
        if not detail:
            return None
        try:
            limit = int(float(detail.get("limit") or 0))
            if limit <= 0:
                return None
            remaining_raw = detail.get("remaining")
            used_raw = detail.get("used")
            if used_raw is not None:
                used = int(float(used_raw))
            elif remaining_raw is not None:
                used = limit - int(float(remaining_raw))
            else:
                used = 0
            remaining = int(float(remaining_raw)) if remaining_raw is not None else limit - used
            pct_used = used / limit * 100

            reset_dt, reset_delta = self._parse_reset(
                self._detail_value(detail, "resetTime", "resetAt", "reset_time", "reset_at")
            )
            warn = 70 if window_type == "session" else 80

            card = {
                "service_name": "Kimi Coding",
                "window_type": window_type,
                "icon": icon,
                "remaining": f"{remaining}",
                "unit": f"{limit} req",
                "reset": reset_delta,
                "health": (
                    "good" if pct_used < 50 else "warning" if pct_used < warn else "critical"
                ),
                "pace": "Stable" if pct_used < 50 else "High" if pct_used < 80 else "Critical",
                "detail": f"{used} used · {label}",
                "used_value": float(used),
                "limit_value": float(limit),
                "pct_used": pct_used,
                "is_unlimited": False,
                "unit_type": "requests",
                "reset_at": reset_dt.isoformat() if reset_dt else None,
                "data_source": data_source,
                "input_source": input_source,
                "usage_url": self.USAGE_URL,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            if variant:
                card["variant"] = variant
            return card
        except (ValueError, TypeError):
            return None

    def _card_from_ratio(
        self,
        pool: dict[str, Any] | None,
        window_type: str,
        input_source: str,
        icon: str = "🌙",
        label: str = "Quota",
        variant: str | None = None,
        data_source: str = "web",
    ) -> dict[str, Any] | None:
        """Ratio-based card ({used_ratio, reset_time}) — percentages only."""
        if not pool:
            return None
        if pool.get("used_ratio") is None:
            return None
        try:
            ratio = float(pool.get("used_ratio") or 0)
        except (ValueError, TypeError):
            return None
        if ratio < 0:
            return None

        pct_used = min(1.0, ratio) * 100
        reset_dt, reset_delta = self._parse_reset(
            self._detail_value(pool, "reset_time", "resetTime", "reset_at", "resetAt")
        )
        warn = 70 if window_type == "session" else 80

        card: dict[str, Any] = {
            "service_name": "Kimi Coding",
            "window_type": window_type,
            "icon": icon,
            "remaining": f"{100 - pct_used:.1f}%",
            "unit": "%",
            "reset": reset_delta,
            "health": "good" if pct_used < 50 else "warning" if pct_used < warn else "critical",
            "pace": "Stable" if pct_used < 50 else "High" if pct_used < 80 else "Critical",
            "detail": f"{pct_used:.1f}% used · {label}",
            "pct_used": pct_used,
            "used_value": None,
            "limit_value": None,
            "is_unlimited": False,
            "unit_type": "percent",
            "reset_at": reset_dt.isoformat() if reset_dt else None,
            "data_source": data_source,
            "input_source": input_source,
            "usage_url": self.USAGE_URL,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if variant:
            card["variant"] = variant
        return card

    def _parse_reset(self, reset_raw: Any) -> tuple[datetime | None, str]:
        """Tolerant reset parsing: ISO string or epoch seconds -> (dt, human delta)."""
        if reset_raw is None:
            return None, "Unknown"
        if isinstance(reset_raw, int | float):
            try:
                dt = datetime.fromtimestamp(float(reset_raw), tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None, "Unknown"
            return dt, human_delta(dt)
        try:
            dt = parse_iso8601_utc(str(reset_raw))
            return dt, human_delta(dt)
        except (ValueError, TypeError):
            logger.debug("Failed to parse Kimi reset time %r", reset_raw, exc_info=True)
            return None, "Unknown"
