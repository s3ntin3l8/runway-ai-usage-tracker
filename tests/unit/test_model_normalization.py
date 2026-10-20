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
