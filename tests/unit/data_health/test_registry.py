"""Tests for app/services/data_health/registry.py."""

from __future__ import annotations

import pytest

from app.services.data_health.registry import BY_ID, REGISTRY, get_check


def test_every_check_has_a_unique_id():
    ids = [check.id for check in REGISTRY]
    assert len(ids) == len(set(ids))


def test_every_check_has_user_facing_guidance():
    for check in REGISTRY:
        assert check.title.strip()
        assert check.description.strip()
        assert check.impact.strip()
        assert check.recommended_action.strip()


def test_get_check_returns_the_registered_instance():
    for check in REGISTRY:
        assert get_check(check.id) is check


def test_get_check_raises_for_an_unknown_id():
    with pytest.raises(KeyError, match="unknown data health check id"):
        get_check("not-a-real-check")


def test_every_blocked_by_reference_names_a_registered_check():
    for check in REGISTRY:
        for dep in check.blocked_by:
            assert dep in BY_ID, f"{check.id!r} names unknown blocked_by dependency {dep!r}"


def test_blocked_by_graph_has_no_cycles():
    def _walk(check_id: str, seen: set[str]) -> None:
        assert check_id not in seen, f"cycle detected at {check_id!r}"
        seen = seen | {check_id}
        for dep in BY_ID[check_id].blocked_by:
            _walk(dep, seen)

    for check in REGISTRY:
        _walk(check.id, set())
