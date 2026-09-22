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


def test_raw_3x_wins_for_flash_lite_when_display_has_non_3x_minor():
    """Stale 2.5 display must not mask a major-3 lite raw id (lite analogue of raw_3x)."""
    assert _normalize_ag_model("gemini-3-flash-lite", "Gemini 2.5 Flash Lite", {}) == "flash-lite-3"


def test_non_3x_minor_falls_back_to_bare_family():
    """2.5 has no antigravity seed row → bare family (not flash-2.5)."""
    assert _normalize_ag_model("gemini-2.5-flash", "Gemini 2.5 Flash", {}) == "flash"


def test_non_family_raw_passthrough():
    assert _normalize_ag_model("gpt-oss", "", {}) == "gpt-oss"
    assert _normalize_ag_model("gpt-oss-120b", "GPT-OSS 120B", {}) == "gpt-oss-120b"


def test_non_family_raw_passes_through_despite_gemini_display():
    """Display-family fallback is scoped to gemini-prefixed raw ids only."""
    assert _normalize_ag_model("gpt-oss", "Gemini 3.5 Flash", {}) == "gpt-oss"


def test_empty_raw_with_display_family_and_version():
    assert _normalize_ag_model("", "Gemini 3.5 Flash", {}) == "flash-3.5"


def test_empty_raw_major_only_display_buckets_like_raw_present():
    """('', 'Gemini 3 Flash') must match ('gemini-3-flash-a', same) → flash-3."""
    assert _normalize_ag_model("", "Gemini 3 Flash", {}) == "flash-3"


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


def test_raw_3x_wins_when_display_has_non_3x_minor():
    """Stale non-3.x display must not mask a 3.x signal that lives in raw."""
    # Hermes probe: display 2.5, raw gemini-3.5-flash → flash-3.5 (not bare flash).
    assert _normalize_ag_model("gemini-3.5-flash", "Gemini 2.5 Flash", {}) == "flash-3.5"


def test_raw_3x_wins_when_display_first_token_is_not_minor_version():
    """`Backend v1.5` has a \\d+.\\d+ token but no 3.x — trust raw's 3.5."""
    assert _normalize_ag_model("gemini-3.5-flash", "Backend v1.5", {}) == "flash-3.5"


def test_major_only_3x_not_masked_by_non_3x_display_minor():
    """Raw major-only gemini-3 + stale non-3.x display minor → major bucket."""
    # Hermes probes: version resolves to 2.5/1.5, but looks_like_3x still holds.
    assert _normalize_ag_model("gemini-3-pro", "Gemini 2.5 Flash", {}) == "pro-3"
    assert _normalize_ag_model("gemini-3-flash-a", "Backend v1.5", {}) == "flash-3"
    # Display major-only 3 + raw non-3.x minor → major bucket (not bare family).
    assert _normalize_ag_model("gemini-2.5-flash", "Gemini 3 Flash", {}) == "flash-3"


def test_seeded_minors_match_pricing_seed():
    """_SEEDED_MINORS must match pricing_seed's antigravity versioned rows both ways.

    Seed row missing from the frozenset → silently clamps to {family}-3.
    Frozenset minor with no seed row → emits e.g. flash-3.4, which
    cost_calculator version-strips to bare flash with no warning.
    """
    import re

    from app.services.pricing_seed import PRICING_SEED
    from scripts.sidecar_pkg.event_extractors.antigravity import _SEEDED_MINORS

    seed_by_family: dict[str, set[str]] = {}
    for row in PRICING_SEED:
        if row.get("provider_id") != "antigravity":
            continue
        m = re.fullmatch(r"(flash|pro)-(\d+\.\d+)", str(row["model_id"]))
        if m and m.group(2).startswith("3."):
            seed_by_family.setdefault(m.group(1), set()).add(m.group(2))

    for family in sorted(set(seed_by_family) | set(_SEEDED_MINORS)):
        assert set(_SEEDED_MINORS.get(family, ())) == seed_by_family.get(family, set()), (
            f"_SEEDED_MINORS[{family!r}] and pricing_seed versioned rows disagree: "
            f"extractor={sorted(_SEEDED_MINORS.get(family, ()))} "
            f"seed={sorted(seed_by_family.get(family, set()))}"
        )


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
