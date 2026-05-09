import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.utils import (
    HealthCalculator,
    PaceCalculator,
    error_card,
    http_request_with_retry,
)

logger = logging.getLogger(__name__)

# Model display name mapping
MODEL_DISPLAY_NAMES = {
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-3-flash-preview": "Gemini 3 Flash (Preview)",
    "gemini-3-pro-preview": "Gemini 3 Pro (Preview)",
    "gemini-3.1-flash-lite-preview": "Gemini 3.1 Flash Lite (Preview)",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro (Preview)",
}


class GeminiApiMixin:
    """Mixin for Gemini Cloud Code API collection."""

    def _map_model_to_class(self, model_name: str) -> str:
        """Map raw model name to card category (pro, flash, flash-lite)."""
        if not model_name:
            return "unknown"
        lower = model_name.lower()
        if "flash-lite" in lower or "gemini-1.5-flash-lite" in lower:
            return "flash-lite"
        if "flash" in lower or "gemini-1.5-flash" in lower:
            return "flash"
        if "pro" in lower or "gemini-1.5-pro" in lower:
            return "pro"
        if "ultra" in lower:
            return "ultra"
        return model_name

    async def _collect_via_api(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch Gemini quota from Google Cloud Code API."""
        now = datetime.now(UTC)
        backoff_until = getattr(self, "_last_429_backoff_until", None)
        if backoff_until and now < backoff_until:
            wait_rem = (backoff_until - now).total_seconds()
            return [
                error_card(
                    "Gemini",
                    "🔵",
                    f"Rate Limited (429) - Backoff for {wait_rem:.0f}s",
                    error_type="rate_limited",
                )
            ]

        token = await self._get_valid_token(client)
        if not token:
            return []

        try:
            headers = {"Authorization": f"Bearer {token}"}

            # 1. Load Code Assist - get project and tier info
            tier_resp = await http_request_with_retry(
                client,
                "POST",
                "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
                json={"metadata": {"ideType": "GEMINI_CLI", "pluginType": "GEMINI"}},
                headers=headers,
                timeout=10,
            )

            if tier_resp.status_code == 429:
                retry_after = tier_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [
                    error_card(
                        "Gemini",
                        "🔵",
                        f"Rate Limited (429) - Try in {wait_sec / 60:.0f}m",
                        error_type="rate_limited",
                    )
                ]

            tier_info = tier_resp.json()
            project_id = tier_info.get("cloudaicompanionProject", "")

            # Extract tier
            paid_tier = tier_info.get("paidTier", {})
            current_tier = tier_info.get("currentTier", {})
            tier_id_raw = paid_tier.get("id", current_tier.get("id", "unknown"))

            tier_mapping = {
                "g1-pro-tier": "pro",
                "g1-ultra-tier": "ultra",
                "standard-tier": "free",
            }
            tier = tier_mapping.get(tier_id_raw, tier_id_raw if tier_id_raw != "unknown" else None)

            # 2. Retrieve Quota
            quota_resp = await http_request_with_retry(
                client,
                "POST",
                "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
                json={"project": project_id},
                headers=headers,
                timeout=10,
            )

            if quota_resp.status_code == 429:
                retry_after = quota_resp.headers.get("Retry-After")
                wait_sec = float(retry_after) if retry_after and retry_after.isdigit() else 300
                self._last_429_backoff_until = now + timedelta(seconds=wait_sec)
                return [
                    error_card(
                        "Gemini",
                        "🔵",
                        f"Rate Limited (429) - Try in {wait_sec / 60:.0f}m",
                        error_type="rate_limited",
                    )
                ]

            self._last_429_backoff_until = None
            quota_data = quota_resp.json()
            buckets = quota_data.get("buckets", [])

            if not buckets:
                return [
                    error_card("Gemini", "🔵", "No quota buckets returned", error_type="api_error")
                ]

            results = []
            seen_classes = set()

            # Multiple buckets can map to the same model class (e.g. both
            # "gemini-2.5-pro" and "gemini-2.5-pro-preview" → "pro"). The API
            # often returns the preview variant first with a null quotaLimit
            # (fraction-only) and the GA variant later with concrete numbers.
            # Stable-sort so quota-bearing buckets come first; the dedup below
            # then keeps the variant that carries actual numbers.
            buckets = sorted(
                buckets,
                key=lambda b: 0 if b.get("quotaLimit") is not None else 1,
            )

            for bucket in buckets:
                raw_model = bucket.get("modelId", "Unknown")
                model_class = self._map_model_to_class(raw_model)

                if model_class in seen_classes:
                    continue
                seen_classes.add(model_class)

                # Determine display name
                if "flash-lite" in raw_model:
                    display_name = "Gemini Flash Lite"
                elif "flash" in raw_model:
                    display_name = "Gemini Flash"
                elif "pro" in raw_model:
                    display_name = "Gemini Pro"
                else:
                    display_name = MODEL_DISPLAY_NAMES.get(raw_model, raw_model)

                remaining_fraction = bucket.get("remainingFraction", 1.0)
                percent_remaining = int(remaining_fraction * 100)
                percent_used = 100 - percent_remaining

                quota_limit = bucket.get("quotaLimit")
                quota_remaining = bucket.get("quotaRemaining")
                token_type = bucket.get("tokenType", "units").lower()

                reset_at = None
                reset_dt = None
                if "resetTime" in bucket:
                    reset_time = bucket["resetTime"]
                    try:
                        reset_dt = datetime.fromisoformat(reset_time.replace("Z", "+00:00"))
                        reset_at = reset_dt.isoformat()
                    except (ValueError, TypeError):
                        pass

                health = HealthCalculator.from_percentage(percent_used)
                pace = PaceCalculator.estimate_longevity(percent_used, reset_dt)

                if quota_limit is not None and quota_remaining is not None:
                    detail_text = f"{percent_remaining}% remaining | {quota_remaining:,} / {quota_limit:,} {token_type} left"
                    used_val = float(quota_limit - quota_remaining)
                    limit_val = float(quota_limit)
                else:
                    detail_text = f"{percent_remaining}% remaining | Model: {raw_model}"
                    used_val = float(percent_used)
                    limit_val = 100.0

                results.append(
                    {
                        "service_name": display_name,
                        "icon": "🔵",
                        "remaining": f"{percent_used}%",
                        "unit": "used",
                        "reset_at": reset_at,
                        "health": health,
                        "pace": pace,
                        "detail": detail_text,
                        "used_value": used_val,
                        "limit_value": limit_val,
                        "model_id": model_class,
                        "unit_type": token_type if quota_limit is not None else "percent",
                        "data_source": self.DATA_SOURCE_API,
                        "input_source": getattr(self, "_current_input_source", "unknown"),
                        "tier": tier,
                        "usage_url": "https://one.google.com/settings",
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )

            results.sort(key=lambda x: int(x["remaining"].rstrip("%")), reverse=True)
            return results

        except Exception as e:
            logger.warning(f"Gemini API collection failed: {e}")
            return []
