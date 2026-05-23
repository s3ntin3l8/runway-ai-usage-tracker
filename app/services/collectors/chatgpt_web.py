import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.utils import HealthCalculator, PaceCalculator, http_request_with_retry, human_delta
from app.services.token_cache import token_cache

logger = logging.getLogger(__name__)


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
            if effective_account_id:
                asyncio.create_task(
                    token_cache.update_account_metadata("chatgpt", effective_account_id, name=email)
                )

        primary = data.get("rate_limit", {}).get("primary_window", {})
        if primary:
            pct = primary.get("used_percent", 0.0)
            reset_ts = primary.get("reset_at")
            reset_at = datetime.fromtimestamp(reset_ts, tz=UTC) if reset_ts else None

            return [
                {
                    "service_name": "ChatGPT",
                    "variant": "Codex",
                    "window_type": "weekly",
                    "icon": "💬",
                    "remaining": f"{(100 - pct):.1f}%",
                    "unit": "remaining",
                    "reset": human_delta(reset_at),
                    "health": HealthCalculator.from_percentage(pct),
                    "pace": PaceCalculator.estimate_longevity(pct, reset_at),
                    "detail": f"{tier.upper()} Account · {email} · {pct:.1f}% used",
                    "used_value": float(pct),
                    "limit_value": 100.0,
                    "unit_type": "percent",
                    "reset_at": reset_at.isoformat() if reset_at else None,
                    "data_source": source,
                    "input_source": getattr(self, "_current_input_source", input_source),
                    "tier": tier,
                    "updated_at": now.isoformat(),
                }
            ]
        return []
