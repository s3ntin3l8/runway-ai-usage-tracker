"""Shared constants and helpers used across all Anthropic collector mixins."""

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.date_utils import parse_iso8601_utc
from app.core.utils import HealthCalculator, PaceCalculator, human_delta

logger = logging.getLogger(__name__)

ANTHROPIC_WINDOW_NAME_MAP: dict[str, str] = {
    "five_hour": "Session",
    "seven_day": "Weekly",
    "seven_day_opus": "Opus Weekly",
    "seven_day_oauth_apps": "OAuth Apps",
    "extra_usage": "Extra Usage",
}

# Maps model-specific seven_day API keys → short model_id for UsageSnapshot.
ANTHROPIC_MODEL_ID_MAP: dict[str, str] = {
    "seven_day_opus": "opus",
    "seven_day_oauth_apps": "oauth_apps",
}


def classify_anthropic_window_type(key: str) -> str:
    """Map an Anthropic usage window key to its canonical window_type string."""
    if key == "five_hour":
        return "session"
    if "seven_day" in key:
        return "weekly"
    return "unknown"


def anthropic_model_id_for(key: str) -> str | None:
    """Return the model_id to stamp on a snapshot for per-model weekly windows."""
    return ANTHROPIC_MODEL_ID_MAP.get(key)


# Maps the `group` field on the newer `limits[]` OAuth response entries
# (kind/group/percent/severity/scope shape) to the canonical window_type string.
ANTHROPIC_GROUP_WINDOW_TYPE_MAP: dict[str, str] = {
    "session": "session",
    "weekly": "weekly",
    "daily": "daily",
    "monthly": "monthly",
}


def classify_anthropic_group(group: str | None) -> str:
    """Map a `limits[]` entry's `group` field to its canonical window_type string."""
    if not group:
        return "unknown"
    return ANTHROPIC_GROUP_WINDOW_TYPE_MAP.get(group, "unknown")


def anthropic_scope_model_id(scope: dict | None) -> str | None:
    """Return the model_id to stamp for a `limits[]` entry's `scope.model`.

    `scope` is null for aggregate windows (e.g. weekly_all). When present, prefer
    the model's stable `id`; fall back to a lowercase slug of `display_name`
    (e.g. "Fable" -> "fable") since some models only carry a display name.
    """
    if not scope:
        return None
    model = scope.get("model")
    if not model:
        return None
    model_id = model.get("id")
    if model_id:
        return str(model_id)
    display_name = model.get("display_name")
    if display_name:
        return str(display_name).strip().lower().replace(" ", "-")
    return None


def anthropic_limits_from(data: dict[str, Any]) -> list[Any] | None:
    """Return the `limits[]` array from a response dict if present and non-empty.

    Returns None when `limits` is missing, not a list, or an empty list, so
    callers fall back to parsing the legacy dict-keyed-by-window-name shape — a
    present-but-empty array can occur during partial API rollout while the
    legacy keys still carry real values, and treating it as authoritative would
    silently drop every card.
    """
    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        return limits
    return None


def build_anthropic_limit_cards(
    limits: list[Any],
    *,
    tier: str | None,
    identity_str: str,
    identity_suffix: str,
    data_source: str,
    input_source: str,
    source_label: str,
    usage_url: str,
    tier_label: str = "",
) -> list[dict[str, Any]]:
    """Parse the `limits[]` response shape (shared by the OAuth and Web API parsers)
    into standardized percentage cards.

    Each entry looks like:
        {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
         "severity": "normal", "resets_at": None,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
         "is_active": False}

    `severity`/`is_active` are not consumed yet — health stays percentage-derived.
    """
    results = []
    for entry in limits:
        if not isinstance(entry, dict):
            continue

        raw_percent = entry.get("percent")
        pct_used = float(raw_percent) if raw_percent is not None else 0.0
        remaining_pct = 100.0 - pct_used

        reset_raw = entry.get("resets_at")
        reset_at = None
        if reset_raw:
            try:
                reset_at = parse_iso8601_utc(reset_raw)
            except (ValueError, TypeError):
                logger.debug("Failed to parse reset_at in limits[] entry", exc_info=True)

        w_type = classify_anthropic_group(entry.get("group"))
        model_id = anthropic_scope_model_id(entry.get("scope"))

        results.append(
            {
                "service_name": "Claude",
                "icon": "🟠",
                "remaining": f"{remaining_pct:.1f}%",
                "unit": "capacity",
                "reset": human_delta(reset_at),
                "health": HealthCalculator.from_percentage(pct_used),
                "pace": PaceCalculator.estimate_longevity(pct_used, reset_at),
                "detail": f"{pct_used:.1f}% used{tier_label} [{source_label}]{identity_suffix}",
                "used_value": pct_used,
                "limit_value": 100.0,
                "is_unlimited": False,
                "unit_type": "percent",
                "window_type": w_type,
                "model_id": model_id,
                "reset_at": reset_at.isoformat() if reset_at else None,
                "data_source": data_source,
                "input_source": input_source,
                "tier": tier,
                "account_label": identity_str,
                "usage_url": usage_url,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    return results
