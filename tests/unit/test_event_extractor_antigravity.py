"""Unit tests for the Antigravity event extractor."""

from scripts.sidecar_pkg.event_extractors.antigravity import (
    _extract_ag_effort,
    _normalize_ag_model,
)

# ---------------------------------------------------------------------------
# Model normalisation — minor-version preservation
# ---------------------------------------------------------------------------


def test_flash_35_from_raw_and_display():
    assert _normalize_ag_model("gemini-3.5-flash", "Gemini 3.5 Flash (Low)", {}) == "flash-3.5"


def test_flash_36_from_display_when_raw_generic():
    assert _normalize_ag_model("gemini-default", "Gemini 3.6 Flash", {}) == "flash-3.6"


def test_flash_37():
    assert _normalize_ag_model("gemini-3.7-flash", "Gemini 3.7 Flash", {}) == "flash-3.7"


def test_flash_38():
    assert _normalize_ag_model("gemini-3.8-flash", "Gemini 3.8 Flash", {}) == "flash-3.8"


def test_pro_31_from_generic_raw():
    assert _normalize_ag_model("gemini-pro-default", "Gemini 3.1 Pro (High)", {}) == "pro-3.1"


def test_flash_3_major_only_no_minor():
    """gemini-3-flash-a with a display that has no minor → flash-3."""
    assert _normalize_ag_model("gemini-3-flash-a", "Gemini 3 Flash", {}) == "flash-3"


def test_pro_3_major_only_no_minor():
    assert _normalize_ag_model("gemini-3-pro", "Gemini 3 Pro", {}) == "pro-3"


def test_flash_3_from_raw_only_when_display_empty():
    assert _normalize_ag_model("gemini-3-flash", "", {}) == "flash-3"


def test_bare_flash_when_no_version():
    assert _normalize_ag_model("gemini-flash", "Gemini Flash", {}) == "flash"


def test_bare_pro_when_no_version():
    assert _normalize_ag_model("gemini-pro", "Gemini Pro", {}) == "pro"


def test_bare_flash_lite_when_no_3x_signal():
    assert _normalize_ag_model("gemini-flash-lite", "Gemini Flash Lite", {}) == "flash-lite"


def test_flash_lite_stays_major_only():
    """No versioned flash-lite-3.5 bucket — lite stays flash-lite-3."""
    assert (
        _normalize_ag_model("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite", {}) == "flash-lite-3"
    )


def test_flash_lite_3_from_major_only():
    assert _normalize_ag_model("gemini-flash-lite", "Gemini 3 Flash Lite", {}) == "flash-lite-3"


def test_non_3x_minor_falls_back_to_bare_family():
    """2.5 has no antigravity seed row → bare family (not flash-2.5)."""
    assert _normalize_ag_model("gemini-2.5-flash", "Gemini 2.5 Flash", {}) == "flash"


def test_non_family_raw_passthrough():
    assert _normalize_ag_model("gpt-oss", "", {}) == "gpt-oss"
    assert _normalize_ag_model("gpt-oss-120b", "GPT-OSS 120B", {}) == "gpt-oss-120b"


def test_empty_raw_with_display_family_and_version():
    assert _normalize_ag_model("", "Gemini 3.5 Flash", {}) == "flash-3.5"


def test_empty_raw_with_display_family_but_no_version_is_unknown():
    assert _normalize_ag_model("", "Gemini Flash", {}) == "unknown"


def test_empty_raw_and_display_is_unknown():
    assert _normalize_ag_model("", "", {}) == "unknown"


def test_empty_raw_display_without_family_is_unknown():
    assert _normalize_ag_model("", "Something Else", {}) == "unknown"


def test_gemini_default_with_flash_display():
    assert _normalize_ag_model("gemini-default", "Gemini 3.5 Flash", {}) == "flash-3.5"


def test_display_version_preferred_over_raw():
    """Display is the human-facing source of truth when both carry a minor."""
    # Raw says 3.5, display says 3.6 — trust the display.
    assert _normalize_ag_model("gemini-3.5-flash", "Gemini 3.6 Flash", {}) == "flash-3.6"


def test_unseeded_flash_minor_clamps_to_major_bucket():
    """flash-3.1 has no antigravity seed row — clamp to flash-3 (not bare flash)."""
    assert _normalize_ag_model("gemini-3.1-flash", "Gemini 3.1 Flash", {}) == "flash-3"


def test_unseeded_pro_minor_clamps_to_major_bucket():
    assert _normalize_ag_model("gemini-3.2-pro", "Gemini 3.2 Pro", {}) == "pro-3"


def test_hybrid_family_and_display_version_clamps():
    """Family from raw (pro) + version from display (3.5) must not emit pro-3.5."""
    assert _normalize_ag_model("gemini-pro-default", "Gemini 3.5 Flash", {}) == "pro-3"


def test_call_site_default_raw_is_empty_string():
    """Missing field 19 defaults to '' (not 'unknown') so both paths agree."""
    assert _normalize_ag_model("", "Gemini Flash", {}) == "unknown"
    assert _normalize_ag_model("", "Gemini 3.5 Flash", {}) == "flash-3.5"


# ---------------------------------------------------------------------------
# Claude KV branches — regression pin (PR #302 owns the real changes)
# ---------------------------------------------------------------------------


def test_used_claude_conservative_maps_to_opus():
    """Pin current behavior: conservative=true wins over used_claude and
    ignores raw/display entirely."""
    assert (
        _normalize_ag_model(
            "gemini-3.5-flash",
            "Gemini 3.5 Flash (Low)",
            {"used_claude_conservative": "true", "used_claude": "true"},
        )
        == "claude-opus"
    )


def test_used_claude_maps_to_sonnet():
    assert (
        _normalize_ag_model(
            "gemini-pro-default",
            "Gemini 3.1 Pro (High)",
            {"used_claude": "true"},
        )
        == "claude-sonnet"
    )


def test_used_claude_false_is_ignored():
    assert (
        _normalize_ag_model(
            "gemini-3.5-flash",
            "Gemini 3.5 Flash",
            {"used_claude": "false", "used_claude_conservative": "false"},
        )
        == "flash-3.5"
    )


def test_used_claude_non_true_value_is_ignored():
    assert (
        _normalize_ag_model("gemini-3.5-flash", "Gemini 3.5 Flash", {"used_claude": "yes"})
        == "flash-3.5"
    )


# ---------------------------------------------------------------------------
# Effort extraction from display name
# ---------------------------------------------------------------------------


def test_effort_high():
    assert _extract_ag_effort("Gemini 3.1 Pro (High)") == "high"


def test_effort_medium():
    assert _extract_ag_effort("Gemini 3.5 Flash (Medium)") == "medium"


def test_effort_low():
    assert _extract_ag_effort("Gemini 3.5 Flash (Low)") == "low"


def test_effort_case_insensitive():
    assert _extract_ag_effort("Gemini 3.1 Pro (HIGH)") == "high"
    assert _extract_ag_effort("Gemini 3.1 Pro (low)") == "low"


def test_effort_thinking_is_none():
    assert _extract_ag_effort("Gemini 3.1 Pro (Thinking)") is None


def test_effort_absent_is_none():
    assert _extract_ag_effort("Gemini 3.1 Pro") is None


def test_effort_empty_is_none():
    assert _extract_ag_effort("") is None


def test_effort_not_trailing_is_none():
    """Only a trailing parenthesized effort suffix counts."""
    assert _extract_ag_effort("(High) Gemini 3.1 Pro") is None
