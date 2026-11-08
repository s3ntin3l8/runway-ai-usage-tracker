"""Shared fixtures and helpers for collector manager unit tests."""

from __future__ import annotations

import pytest

from app.services.collector_manager import CollectorManager

__all__ = ["manager_fixture"]


@pytest.fixture(name="manager")
def manager_fixture():
    m = CollectorManager()
    # Reset state
    m._collect_future = None
    return m
