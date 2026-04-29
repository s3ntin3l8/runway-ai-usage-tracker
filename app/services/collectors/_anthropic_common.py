"""Shared constants and helpers used across all Anthropic collector mixins."""

ANTHROPIC_WINDOW_NAME_MAP: dict[str, str] = {
    "five_hour": "Session",
    "seven_day": "Weekly",
    "seven_day_sonnet": "Sonnet Weekly",
    "seven_day_opus": "Opus Weekly",
    "seven_day_omelette": "Claude Design",
    "seven_day_cowork": "Cowork",
    "seven_day_oauth_apps": "OAuth Apps",
    "extra_usage": "Extra Usage",
}

# Maps model-specific seven_day API keys → short model_id for UsageSnapshot.
ANTHROPIC_MODEL_ID_MAP: dict[str, str] = {
    "seven_day_sonnet": "sonnet",
    "seven_day_opus": "opus",
    "seven_day_omelette": "design",
    "seven_day_cowork": "cowork",
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
