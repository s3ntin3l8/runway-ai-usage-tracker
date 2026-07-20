"""Tests for shared Anthropic window classification helpers."""

import pytest

from app.services.collectors._anthropic_common import (
    ANTHROPIC_WINDOW_NAME_MAP,
    anthropic_model_id_for,
    anthropic_scope_model_id,
    classify_anthropic_group,
    classify_anthropic_window_type,
)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("five_hour", "session"),
        ("seven_day", "weekly"),
        ("seven_day_opus", "weekly"),
        ("extra_usage", "unknown"),
        ("unknown_key", "unknown"),
    ],
)
def test_classify_anthropic_window_type(key, expected):
    assert classify_anthropic_window_type(key) == expected


@pytest.mark.parametrize(
    "key,expected",
    [
        ("seven_day_opus", "opus"),
        ("seven_day_sonnet", None),
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
    assert "seven_day_opus" in ANTHROPIC_WINDOW_NAME_MAP
    assert "extra_usage" in ANTHROPIC_WINDOW_NAME_MAP
    # seven_day_sonnet was retired by Anthropic; only aggregate weekly + session remain.
    assert "seven_day_sonnet" not in ANTHROPIC_WINDOW_NAME_MAP


@pytest.mark.parametrize(
    "group,expected",
    [
        ("session", "session"),
        ("weekly", "weekly"),
        ("daily", "daily"),
        ("monthly", "monthly"),
        ("unknown_group", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_anthropic_group(group, expected):
    assert classify_anthropic_group(group) == expected


@pytest.mark.parametrize(
    "scope,expected",
    [
        (None, None),
        ({}, None),
        ({"model": None}, None),
        ({"model": {"id": None, "display_name": "Fable"}}, "fable"),
        ({"model": {"id": "claude-opus-4", "display_name": "Opus"}}, "claude-opus-4"),
        ({"model": {"id": None, "display_name": "Claude Fable"}}, "claude-fable"),
        ({"model": {"id": None, "display_name": None}}, None),
    ],
)
def test_anthropic_scope_model_id(scope, expected):
    assert anthropic_scope_model_id(scope) == expected
