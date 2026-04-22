"""Tests for shared Anthropic window classification helpers."""

import pytest

from app.services.collectors._anthropic_common import (
    ANTHROPIC_WINDOW_NAME_MAP,
    anthropic_model_id_for,
    classify_anthropic_window_type,
)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("five_hour", "session"),
        ("seven_day", "weekly"),
        ("seven_day_sonnet", "weekly"),
        ("seven_day_opus", "weekly"),
        ("seven_day_omelette", "weekly"),
        ("extra_usage", "unknown"),
        ("unknown_key", "unknown"),
    ],
)
def test_classify_anthropic_window_type(key, expected):
    assert classify_anthropic_window_type(key) == expected


@pytest.mark.parametrize(
    "key,expected",
    [
        ("seven_day_sonnet", "sonnet"),
        ("seven_day_opus", "opus"),
        ("seven_day_omelette", "design"),
        ("seven_day", None),
        ("five_hour", None),
        ("extra_usage", None),
    ],
)
def test_anthropic_model_id_for(key, expected):
    assert anthropic_model_id_for(key) == expected


def test_name_map_has_all_expected_keys():
    assert "five_hour" in ANTHROPIC_WINDOW_NAME_MAP
    assert "seven_day" in ANTHROPIC_WINDOW_NAME_MAP
    assert "seven_day_sonnet" in ANTHROPIC_WINDOW_NAME_MAP
    assert "seven_day_opus" in ANTHROPIC_WINDOW_NAME_MAP
    assert "seven_day_omelette" in ANTHROPIC_WINDOW_NAME_MAP
    assert "extra_usage" in ANTHROPIC_WINDOW_NAME_MAP


def test_name_map_omelette_is_claude_design():
    assert ANTHROPIC_WINDOW_NAME_MAP["seven_day_omelette"] == "Claude Design"
