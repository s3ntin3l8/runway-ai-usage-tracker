"""
LimitCardBuilder — Fluent builder for constructing LimitCard dicts.

Enforces mandatory fields at construction time, provides intelligent defaults,
and validates the result against the LimitCard Pydantic schema before returning.
This prevents the class of 500 errors caused by manual dict construction missing
fields like `icon`, `reset`, or `pace`.

Usage:
    card = (
        LimitCardBuilder("Claude Pro", "🟠", "80.0%", "capacity")
        .set_provider("anthropic", window_type="rolling")
        .set_health("good")
        .set_timing(reset="in 2h 30m")
        .set_detail("20% used [OAuth]")
        .build()
    )

    error_card = LimitCardBuilder.error("Claude Pro", "🟠", "Auth failed", "auth_failed")
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.models.schemas import LimitCard


class LimitCardBuilder:
    """
    Fluent builder for LimitCard dicts.

    Mandatory fields: service_name, icon, remaining, unit
    Defaults: reset="—", pace="Stable", health="unknown",
              data_source="unknown", updated_at=utcnow()
    """

    def __init__(self, service_name: str, icon: str, remaining: str, unit: str):
        if not service_name:
            raise ValueError("service_name is required")
        if not icon:
            raise ValueError("icon is required")
        if remaining is None:
            raise ValueError("remaining is required")
        if unit is None:
            raise ValueError("unit is required")

        self._data: Dict[str, Any] = {
            "service_name": service_name,
            "icon": icon,
            "remaining": remaining,
            "unit": unit,
            # Enforced defaults
            "reset": "—",
            "pace": "Stable",
            "health": "unknown",
            "detail": "",
            "data_source": "unknown",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ─── Provider / schema fields ──────────────────────────────────────────────

    def set_provider(self, provider_id: str, window_type: str = "unknown") -> "LimitCardBuilder":
        self._data["provider_id"] = provider_id
        self._data["window_type"] = window_type
        return self

    def set_account(
        self,
        account_id: Optional[str] = None,
        account_label: Optional[str] = None,
    ) -> "LimitCardBuilder":
        if account_id is not None:
            self._data["account_id"] = account_id
        if account_label is not None:
            self._data["account_label"] = account_label
        return self

    def set_model(self, model_id: str) -> "LimitCardBuilder":
        self._data["model_id"] = model_id
        return self

    def set_sidecar(self, sidecar_id: str) -> "LimitCardBuilder":
        self._data["sidecar_id"] = sidecar_id
        return self

    # ─── Display fields ────────────────────────────────────────────────────────

    def set_health(self, health: str) -> "LimitCardBuilder":
        self._data["health"] = health
        return self

    def set_timing(self, reset: str, reset_at: Optional[str] = None) -> "LimitCardBuilder":
        self._data["reset"] = reset
        if reset_at is not None:
            self._data["reset_at"] = reset_at
        return self

    def set_usage(
        self,
        used_value: float,
        limit_value: float,
        unit_type: str,
        currency: Optional[str] = None,
        is_unlimited: bool = False,
    ) -> "LimitCardBuilder":
        self._data["used_value"] = used_value
        self._data["limit_value"] = limit_value
        self._data["unit_type"] = unit_type
        self._data["is_unlimited"] = is_unlimited
        if currency is not None:
            self._data["currency"] = currency
        return self

    def set_pace(self, pace: str) -> "LimitCardBuilder":
        self._data["pace"] = pace
        return self

    def set_detail(self, detail: str) -> "LimitCardBuilder":
        self._data["detail"] = detail
        return self

    def set_tier(self, tier: str) -> "LimitCardBuilder":
        self._data["tier"] = tier
        return self

    def set_source(self, data_source: str) -> "LimitCardBuilder":
        self._data["data_source"] = data_source
        return self

    def set_usage_url(self, usage_url: str) -> "LimitCardBuilder":
        self._data["usage_url"] = usage_url
        return self

    def set_metadata(self, metadata: Dict[str, Any]) -> "LimitCardBuilder":
        self._data["metadata"] = metadata
        return self

    def set_error_type(self, error_type: str) -> "LimitCardBuilder":
        self._data["error_type"] = error_type
        return self

    # ─── Build ─────────────────────────────────────────────────────────────────

    def build(self) -> Dict[str, Any]:
        """
        Validate against LimitCard Pydantic schema and return a dict.

        Raises ValidationError if mandatory fields are missing or invalid.
        Catches schema errors at construction time, not at the API endpoint.
        """
        # Validate through Pydantic — raises ValidationError on schema violations
        LimitCard(**self._data)
        return dict(self._data)

    # ─── Convenience factory methods ───────────────────────────────────────────

    @classmethod
    def error(
        cls,
        service_name: str,
        icon: str,
        message: str,
        error_type: str = "unknown",
        provider_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a standardised error card dict."""
        from app.core.utils import truncate_string
        b = (
            cls(service_name, icon, "ERR", "Check State")
            .set_health("critical")
            .set_timing("—")
            .set_pace("Stopped")
            .set_detail(truncate_string(message, 40))
            .set_source("error")
            .set_error_type(error_type)
        )
        if provider_id:
            b.set_provider(provider_id)
        return b.build()
