import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.utils import HealthCalculator, PaceCalculator, http_request_with_retry, human_delta
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)

# Thresholds for classifying a `rate_limit.*_window.limit_window_seconds` value
# into the canonical WindowType enum. Nearest-bucket rather than exact-match, so
# a small nudge to the real duration (e.g. OpenAI tweaking 18000s slightly)
# still classifies correctly. Order matters — first match wins.
_WINDOW_SECONDS_THRESHOLDS: tuple[tuple[int, str], tuple[int, str], tuple[int, str]] = (
    (6 * 3600, "session"),
    (2 * 86400, "daily"),
    (10 * 86400, "weekly"),
)


def _classify_window_seconds(seconds: float | None) -> str:
    """Map a window duration in seconds to the canonical window_type enum.

    Two distinct cases both fall through to "monthly": `limit_window_seconds`
    absent (free/Go payloads never send it — see tests/fixtures/mock_data.py,
    preserving today's behavior for that shape) and `limit_window_seconds`
    present but > 10 days (a genuine month-or-longer window, should one ever
    appear). Deliberate — both describe a month-or-longer cadence, and nothing
    downstream (WINDOW_RANK, forecasting) distinguishes "explicit monthly"
    from "unknown, but at least monthly-length".
    """
    if seconds is None:
        return "monthly"
    for threshold, window_type in _WINDOW_SECONDS_THRESHOLDS:
        if seconds <= threshold:
            return window_type
    return "monthly"


def _window_variant_label(window_type: str) -> str:
    """Short human label for a window_type, used in the `detail` string."""
    return {"session": "5h", "daily": "daily", "weekly": "weekly", "monthly": "monthly"}.get(
        window_type, window_type
    )


class ChatGPTWebMixin:
    """Mixin for ChatGPT Web API collection."""

    async def _fetch_api_data(
        self,
        client: httpx.AsyncClient,
        token: str,
        account_id: str | None,
        source: str,
        input_source: str = "unknown",
    ) -> list[dict[str, Any]]:
        """Fetch from ChatGPT backend."""
        # Ensure we don't have a double Bearer prefix
        auth_token = token
        if token.lower().startswith("bearer "):
            auth_token = token[7:].strip()

        headers = {
            "Authorization": f"Bearer {auth_token}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "oai-device-id": await self._get_device_id(),
            "oai-language": "en-US",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id

        now = datetime.now(UTC)
        usage_resp = await http_request_with_retry(
            client, "GET", "https://chatgpt.com/backend-api/wham/usage", headers=headers, timeout=10
        )

        if usage_resp.status_code != 200:
            logger.warning(
                "ChatGPT usage fetch failed: HTTP %d — %s",
                usage_resp.status_code,
                usage_resp.text[:300],
            )
            return []

        data = usage_resp.json()
        tier = data.get("plan_type", "free")
        email = data.get("email", "")

        # Identity Promotion — always capture email; account_id may be None for default account
        if email:
            self.account_label = email
            effective_account_id = self.account_id or account_id
            pending = token_cache.current_source_metadata(
                "chatgpt", effective_account_id or "default"
            )
            # A source still waiting for its identity must not relabel the shared default slot.
            if effective_account_id and not (pending and pending.get("identity_pending") is True):
                asyncio.create_task(
                    token_cache.update_account_metadata("chatgpt", effective_account_id, name=email)
                )

        self._adopt_pending_identity(email)

        rate_limit = data.get("rate_limit", {})
        cards: list[dict[str, Any]] = []

        # Codex reports up to two independent windows — e.g. Plus/Pro get a 5h
        # session window (primary_window) plus a 7d weekly window
        # (secondary_window); free/go plans report only primary_window. Presence
        # of the dict is the gate, not a truthy used_percent — a window sitting
        # at 0% used (fresh weekly allowance) must still render (see MiniMax fix
        # in d6c865c2 for the same trap: hiding a window at 0/100% used is exactly
        # when the user needs to see it).
        for window_key in ("primary_window", "secondary_window"):
            window = rate_limit.get(window_key)
            if not window:
                continue

            pct = window.get("used_percent", 0.0)
            reset_ts = window.get("reset_at")
            if reset_ts:
                reset_at = datetime.fromtimestamp(reset_ts, tz=UTC)
            else:
                reset_after = window.get("reset_after_seconds")
                reset_at = now + timedelta(seconds=reset_after) if reset_after else None

            window_type = _classify_window_seconds(window.get("limit_window_seconds"))
            variant_label = _window_variant_label(window_type)

            cards.append(
                {
                    "service_name": "ChatGPT",
                    "variant": "Codex",
                    "window_type": window_type,
                    "icon": "💬",
                    "remaining": f"{(100 - pct):.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": HealthCalculator.from_percentage(pct),
                    "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                    "detail": f"{tier.upper()} Account · {email} · {pct:.1f}% used ({variant_label})",
                    "used_value": float(pct),
                    "limit_value": 100.0,
                    "pct_used": float(pct),
                    "unit_type": "percent",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": source,
                    "input_source": getattr(self, "_current_input_source", input_source),
                    "tier": tier,
                    "updated_at": now.isoformat(),
                }
            )

        return cards
