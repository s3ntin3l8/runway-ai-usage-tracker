"""Tests for app/services/model_normalization.py — the model-id predicates
shared by the sidecar extractors and the server.
"""

from __future__ import annotations

import pytest

from app.services.model_normalization import is_free_tier_model_id


@pytest.mark.parametrize(
    "model_id",
    [
        # OpenCode's "-free" catalog entries.
        "minimax-m3.1-flash-free",
        "MiniMax-M3.1-Flash-free",
        "deepseek-v4-flash-free",
        # OpenRouter's ":free" entries.
        "vendor/model:free",
        "minimax/minimax-m3:free",
        # The "openrouter/free/*" prefix form.
        "openrouter/free",
        "openrouter/free/gpt-4",
        # A bare "free" id.
        "free",
    ],
)
def test_free_tier_ids_are_recognised(model_id: str):
    assert is_free_tier_model_id(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        "",
        None,
        "MiniMax-M3.1-Flash",
        "minimax-m3",
        "grok-4",
        "free-tier-model",  # mentions "free" but is not a free-tier id
        "freemium-model",
        "claude-opus-4.8",
    ],
)
def test_non_free_ids_are_not_recognised(model_id: str | None):
    assert is_free_tier_model_id(model_id) is False


def test_free_tier_detection_is_case_and_whitespace_insensitive():
    assert is_free_tier_model_id("  MINIMAX-M3.1-FLASH-FREE  ") is True
    assert is_free_tier_model_id("Vendor/Model:Free") is True


def test_sidecar_free_model_predicate_is_deliberately_narrower():
    """The sidecar's `_is_free_model` decides which provider bucket a Hermes
    session files under, so it stays narrower than the shared pricing
    predicate: widening it would re-route "openrouter/free/*" sessions from
    "opencode" to "opencode-free". Pin the difference so nobody closes it by
    accident."""
    from scripts.sidecar_pkg.event_extractors.hermes import _is_free_model

    # Shared pricing predicate — the broader one.
    assert is_free_tier_model_id("openrouter/free/gpt-4") is True
    # Sidecar routing predicate — unchanged behaviour.
    assert _is_free_model("openrouter/free/gpt-4") is False
    # Both agree on the suffix forms.
    for model_id in ("minimax-m3.1-flash-free", "vendor/model:free", "free"):
        assert is_free_tier_model_id(model_id) is True
        assert _is_free_model(model_id) is True
