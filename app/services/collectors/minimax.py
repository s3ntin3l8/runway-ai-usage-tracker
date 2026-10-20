import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import settings
from app.core.utils import HealthCalculator, PaceCalculator, error_card, human_delta
from app.services.collectors.base import BaseCollector, ErrorType
from app.services.credential_provider import credential_provider

logger = logging.getLogger(__name__)

# `current_interval_status` / `current_weekly_status` values that mean "this
# quota bucket is yours" — 1 = available, 2 = exhausted (0% remaining). Status
# 3 (observed on the "video" bucket) means "not part of your plan" and is
# intentionally excluded regardless of its remaining_percent. Undocumented by
# MiniMax; see _parse_api_response's docstring for how this was derived.
ENTITLED_STATUSES = frozenset({1, 2})

# The MiniMax Code app's OAuth access token. The platform API secret is an
# ``sk-...``. The two are not interchangeable, and the prefix is the only
# marker of which kind a credential is once it arrives from Settings or the
# database — where it comes without any other hint of its kind.
OAUTH_TOKEN_PREFIX = "mmoat_"

# The China domain, and the open-platform host the MiniMax Code app's quota
# client uses there. The global region uses ``platform.minimax.io`` instead.
CHINA_DOMAIN = "minimaxi.com"
CHINA_OPEN_PLATFORM_HOST = "www.minimaxi.com"

# MiniMax's body-level "login fail" code: the token was rejected by the host
# it was sent to (an OAuth token on the API host, or an API key on the
# open-platform host). HTTP 200, so only the body reveals it.
LOGIN_FAIL_STATUS = 1004


def _credential_kind_of(token: str | None) -> str:
    """Which kind of credential *token* is: ``"oauth"`` or ``"api_key"``.

    The API host answers ``status_code 1004 login fail`` for an OAuth token
    and the open-platform host answers it for an API key, so sending the
    wrong kind silently reports "No active plan" instead of the real quota.
    The app's token carries the ``mmoat_`` prefix; the platform secret is an
    ``sk-...``. Anything else is treated as an API key.
    """
    if token and token.startswith(OAUTH_TOKEN_PREFIX):
        return "oauth"
    return "api_key"


def _hostname(host: str) -> str:
    """Hostname of *host*, which may be a bare domain or carry a scheme."""
    candidate = (host or "").strip()
    if "://" not in candidate:
        candidate = f"//{candidate}"
    try:
        return (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return ""


class MiniMaxCollector(BaseCollector):
    """
    Collector for MiniMax (Coding Plan) usage.

    Supports:
    - API token: https://api.minimax.io/v1/coding_plan/remains
    - MiniMax Code app OAuth token: https://platform.minimax.io/v1/api/openplatform/coding_plan/remains

    Config:
    - MINIMAX_API_KEY: API token (env var or DB)
    - MINIMAX_HOST: Override host (default: minimax.io, China: minimaxi.com) — a bare
      domain; both the API and dashboard URLs prefix it themselves. The
      open-platform host is derived from it: ``minimaxi.com`` (or any of its
      subdomains) maps to ``www.minimaxi.com``, anything else to
      ``platform.<host>``.
    """

    PROVIDER_ID = "minimax"
    COMPLETE_SNAPSHOT = True
    # No single canonical cadence — the plan has a 5h rolling window and a
    # weekly window, both stamped explicitly per card. Never "monthly".
    DEFAULT_WINDOW_TYPE = "unknown"
    USAGE_URL = "https://platform.minimax.io/user-center/payment/coding-plan"

    def __init__(self, account_id: str | None = None, account_label: str | None = None):
        super().__init__(account_id=account_id, account_label=account_label)
        self.api_key: str | None = None
        self.host: str = ""
        # Which kind of credential the last _get_current_creds() returned:
        # "api_key" (platform API secret) or "oauth" (the MiniMax Code app's
        # login). They are not interchangeable — each host accepts only its own.
        self._credential_kind: str = "api_key"

    async def _get_current_creds(self) -> str | None:
        """Async credential retrieval with metadata support.

        Two credential kinds reach the same provider: the platform API secret
        (``sk-...``) and the MiniMax Code app's OAuth access token
        (``mmoat_...``). The API host answers ``status_code 1004 login fail``
        for an OAuth token and the open-platform host answers it for an API
        key, so the kind is remembered here and decides the request.
        """
        account_id = getattr(self, "credential_account_id", None) or self.account_id or "default"
        db_key = credential_provider.get_provider_api_key("minimax", account_id=account_id)
        if db_key:
            self._current_input_source = "config"
            self._credential_kind = _credential_kind_of(db_key)
            return db_key

        from app.services.token_cache import token_cache

        cache_data = await token_cache.get_with_metadata("minimax", account_id=account_id)
        if cache_data:
            tokens, metadata = cache_data
            source = metadata.get("source") or "sidecar"
            self._current_input_source = (
                "config" if source in ("config", "manual_config") else "sidecar"
            )
            api_key = tokens.get("api_key")
            oauth_token = tokens.get("oauth_token")
            if api_key:
                # A credential pasted into the UI lands in the api_key slot
                # whatever it really is, so let the prefix decide.
                self._credential_kind = _credential_kind_of(api_key)
                return api_key
            if oauth_token:
                self._credential_kind = "oauth"
                return oauth_token
        key = (settings.MINIMAX_API_KEY or None) if account_id == "default" else None
        if key:
            self._current_input_source = "server"
            self._credential_kind = _credential_kind_of(key)
        return key

    async def is_configured(self) -> bool:
        """Check if a MiniMax API key is present."""
        key = await self._get_current_creds()
        return self._is_valid_credential(key)

    def _get_host(self) -> str:
        """Get host from config or default."""
        if settings.MINIMAX_HOST:
            return settings.MINIMAX_HOST
        return "minimax.io"

    async def _primary_strategy(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Collect usage data from MiniMax via API."""
        self.api_key = await self._get_current_creds()
        self.host = self._get_host()

        if not self.api_key:
            return []

        if self._credential_kind == "oauth":
            return await self._fetch_via_openplatform(client)
        return await self._fetch_via_api(client)

    def _open_platform_base_url(self) -> str:
        """Base URL of the open-platform host that serves the quota endpoints.

        The MiniMax Code app's own quota client uses ``platform.minimax.io``
        for the global region and ``www.minimaxi.com`` for China — a split the
        bare-domain ``MINIMAX_HOST`` override can't express on its own, so the
        China host is listed explicitly and everything else follows the
        ``platform.<host>`` pattern.

        The China check compares the *parsed hostname* against the registrable
        domain instead of testing a raw suffix: a suffix test would also
        accept ``evilminimaxi.com``.
        """
        name = _hostname(self.host)
        labels = name.split(".") if name else []
        if len(labels) >= 2 and ".".join(labels[-2:]) == CHINA_DOMAIN:
            return f"https://{CHINA_OPEN_PLATFORM_HOST}"
        return f"https://platform.{self.host.lower()}"

    async def _fetch_via_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch usage via the API endpoint.

        `/v1/coding_plan/remains` is the endpoint this collector has always used and
        it is confirmed working; MiniMax also documents a newer `/v1/token_plan/remains`
        path (same host) that we haven't migrated to.
        """
        base_url = f"https://api.{self.host}"

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            resp = await client.get(
                f"{base_url}/v1/coding_plan/remains",
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                return self._parse_api_response(data)

            logger.error(f"MiniMax API error (HTTP {resp.status_code}): {resp.text[:200]}")

        except Exception as e:
            logger.error(f"Failed to collect MiniMax usage: {e}")

        return []

    async def _fetch_via_openplatform(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch usage with the MiniMax Code app's OAuth token.

        The app's own quota client calls
        ``/v1/api/openplatform/coding_plan/remains`` on the open-platform host
        with the OAuth access token as a bearer. The payload is the same
        ``model_remains`` shape the API-key path returns, so the parser is
        shared — only the host and path differ.
        """
        base_url = self._open_platform_base_url()

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            }

            resp = await client.get(
                f"{base_url}/v1/api/openplatform/coding_plan/remains",
                headers=headers,
                timeout=10,
            )

            if resp.status_code == 200:
                return self._parse_api_response(resp.json())

            logger.error(
                f"MiniMax open-platform API error (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        except Exception as e:
            logger.error(f"Failed to collect MiniMax usage via open platform: {e}")

        return []

    def _parse_api_response(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse `/v1/coding_plan/remains` and build one card per active window.

        Real response shape (nothing like the field names this parser used to
        assume — `model_remains[]` never had a `remains` field):

            {"model_remains": [{
                "model_name": "general",
                "start_time": <ms>, "end_time": <ms>, "remains_time": <ms>,
                "current_interval_total_count": 0, "current_interval_usage_count": 0,
                "current_interval_status": 1, "current_interval_remaining_percent": 100,
                "weekly_start_time": <ms>, "weekly_end_time": <ms>,
                "current_weekly_total_count": 0, "current_weekly_usage_count": 0,
                "current_weekly_status": 1, "current_weekly_remaining_percent": 100
            }, {"model_name": "video", ..., "current_interval_status": 3}],
             "base_resp": {"status_code": 0, "status_msg": "success"}}

        `model_remains` holds one entry per quota bucket, not per billable model —
        "general" covers the text/coding models, "video" is a separate entitlement
        this collector doesn't surface. MiniMax doesn't document the status enum;
        observed against a live account: "video" persists at status=3 with 100%
        remaining regardless of actual usage (a fixed "not part of your plan"
        marker), while "general" oscillates between status=1 (100% remaining,
        available) and status=2 (0% remaining, exhausted) as real traffic depletes
        it. So status is two different things bolted together — {1, 2} means "you
        own this bucket" (available vs. exhausted are its two operational states),
        while 3 (or anything else) means "not your bucket, don't show it". Gating
        on status == 1 alone — the original assumption, made before "general" was
        ever seen exhausted — silently hid the card at exactly the moment (0%
        remaining) a user most needs to see it. The weekly block is identical
        across every entry (one account-level weekly quota), so it's emitted
        once, from the first ENTITLED_STATUSES entry, not per entry.
        """
        now_str = datetime.now(UTC).isoformat()

        # MiniMax answers HTTP 200 and puts the failure in the body: an OAuth
        # token sent to the API host (or an API key sent to the open-platform
        # host) comes back as ``base_resp.status_code`` 1004 "login fail".
        # Parsing that as a quota snapshot would report a healthy "No active
        # plan" card — and because this collector reports complete snapshots,
        # the reconciler would then retire the user's real session and weekly
        # rows. A rejected credential is a failure, not an empty plan, so it
        # has to become an error card.
        failure = self._api_failure(data)
        if failure is not None:
            code, message = failure
            detail = f"MiniMax error {code}" + (f": {message}" if message else "")
            error_type = ErrorType.AUTH_FAILED if code == LOGIN_FAIL_STATUS else ErrorType.API_ERROR
            logger.error("MiniMax API returned a failure status: %s", detail)
            return [
                error_card(
                    "MiniMax",
                    "🤖",
                    detail,
                    error_type=error_type,
                    provider_id=self.PROVIDER_ID,
                )
            ]

        model_remains = data.get("model_remains", [])
        entitled = [
            item
            for item in model_remains
            if item.get("current_interval_status") in ENTITLED_STATUSES
        ]
        # Session and weekly entitlement are gated independently — MiniMax can
        # report the 5h window owned while the weekly bucket is meanwhile not
        # part of the plan (or vice versa), so don't assume entitled[0]'s weekly
        # block is safe to use just because the session is owned.
        weekly_entitled = [
            item for item in model_remains if item.get("current_weekly_status") in ENTITLED_STATUSES
        ]

        if not entitled and not weekly_entitled:
            return [
                {
                    "service_name": "MiniMax",
                    "icon": "🤖",
                    "remaining": "No active plan",
                    "unit": "quota",
                    "reset": "—",
                    "health": "good",
                    "pace": "N/A",
                    "detail": "No active plan",
                    "data_source": self.DATA_SOURCE_API,
                    "input_source": getattr(self, "_current_input_source", "unknown"),
                    "is_unlimited": False,
                    "unit_type": "unknown",
                    "updated_at": now_str,
                }
            ]

        results = [self._build_session_card(item, now_str) for item in entitled]
        if weekly_entitled:
            results.append(self._build_weekly_card(weekly_entitled[0], now_str))
        return results

    @staticmethod
    def _api_failure(data: dict[str, Any]) -> tuple[int, str] | None:
        """The ``base_resp`` failure of a MiniMax payload, or None when it is OK.

        MiniMax reports success as ``status_code`` 0; anything else is a
        failure the HTTP status does not show (the response is a 200).
        """
        base_resp = data.get("base_resp")
        if not isinstance(base_resp, dict):
            return None
        try:
            code = int(base_resp.get("status_code"))
        except (TypeError, ValueError):
            return None
        if code == 0:
            return None
        return code, str(base_resp.get("status_msg") or "").strip()

    def _window_pct_used(
        self, total_count: int, usage_count: int, remaining_percent: float
    ) -> float:
        """`*_remaining_percent` is remaining, not used — invert it. Count fields win
        when the plan has started reporting them (`total_count > 0`); until then fall
        back to the percent field, which MiniMax appears to hold at 100 pre-use."""
        if total_count > 0:
            return (usage_count / total_count) * 100
        return 100.0 - remaining_percent

    def _build_session_card(self, item: dict[str, Any], now_str: str) -> dict[str, Any]:
        total_count = item.get("current_interval_total_count", 0)
        usage_count = item.get("current_interval_usage_count", 0)
        remaining_percent = item.get("current_interval_remaining_percent", 100.0)
        pct_used = self._window_pct_used(total_count, usage_count, remaining_percent)

        reset_at = None
        end_time = item.get("end_time")
        if end_time:
            reset_at = datetime.fromtimestamp(end_time / 1000, tz=UTC)

        return self._build_card(
            variant_label="5h",
            window_type="session",
            total_count=total_count,
            usage_count=usage_count,
            pct_used=pct_used,
            reset_at=reset_at,
            now_str=now_str,
        )

    def _build_weekly_card(self, item: dict[str, Any], now_str: str) -> dict[str, Any]:
        total_count = item.get("current_weekly_total_count", 0)
        usage_count = item.get("current_weekly_usage_count", 0)
        remaining_percent = item.get("current_weekly_remaining_percent", 100.0)
        pct_used = self._window_pct_used(total_count, usage_count, remaining_percent)

        reset_at = None
        weekly_end_time = item.get("weekly_end_time")
        if weekly_end_time:
            reset_at = datetime.fromtimestamp(weekly_end_time / 1000, tz=UTC)

        return self._build_card(
            variant_label="weekly",
            window_type="weekly",
            total_count=total_count,
            usage_count=usage_count,
            pct_used=pct_used,
            reset_at=reset_at,
            now_str=now_str,
        )

    def _build_card(
        self,
        *,
        variant_label: str,
        window_type: str,
        total_count: int,
        usage_count: int,
        pct_used: float,
        reset_at: datetime | None,
        now_str: str,
    ) -> dict[str, Any]:
        health = HealthCalculator.from_percentage(pct_used)
        pace = PaceCalculator.estimate_longevity(pct_used, reset_at)

        # Parenthesize the window label rather than trailing it after "· " — the
        # base collector's account-label auto-discovery (base.py's `user_match`
        # regex) treats a bare token after "·"/"|" at the end of `detail` as a
        # discovered account label, and would otherwise mislabel the account
        # "5h"/"weekly" instead of leaving it "Default".
        if total_count > 0:
            used_value = float(usage_count)
            limit_value = float(total_count)
            unit_type = "requests"
            allowance = f"{usage_count:,}/{total_count:,} prompts ({variant_label})"
        else:
            # No count data yet (fresh subscription, nothing consumed) — fall back
            # to the percent shape so the card still carries a real pct_used.
            used_value = pct_used
            limit_value = 100.0
            unit_type = "percent"
            allowance = f"—/— prompts ({variant_label})"

        # On overage (usage_count > total_count) pct_used can exceed 100 — clamp
        # the displayed remaining percentage at 0 rather than showing negative.
        remaining_pct = max(0.0, 100 - pct_used)

        return {
            "service_name": "MiniMax",
            "icon": "🤖",
            "remaining": f"{remaining_pct:.1f}%",
            "unit": "capacity",
            "reset": human_delta(reset_at),
            "health": health,
            "pace": pace,
            "detail": f"{allowance} [{'App' if self._credential_kind == 'oauth' else 'API'}]",
            "used_value": used_value,
            "limit_value": limit_value,
            "is_unlimited": False,
            "unit_type": unit_type,
            "pct_used": pct_used,
            "window_type": window_type,
            "model_id": None,
            "reset_at": reset_at.isoformat() if reset_at else None,
            "data_source": self.DATA_SOURCE_API,
            "input_source": getattr(self, "_current_input_source", "unknown"),
            "usage_url": self.USAGE_URL,
            "updated_at": now_str,
        }

    def _fallback_strategies(self) -> list[Any]:
        """No fallback tier — the HTML coding-plan page is a client-rendered SPA and
        can't be scraped; the API endpoint is the only source."""
        return []

    async def _error_handler(self) -> list[dict[str, Any]]:
        """Return the ultimate error card(s) when all strategies fail."""
        from app.core.utils import error_card

        if not self.api_key:
            return [error_card("MiniMax", "🤖", "Missing API key", error_type="missing_config")]

        return [error_card("MiniMax", "🤖", "API connection failed", error_type="api_error")]
