"""The sidecar and the server must agree on which event sources are never taggable."""

from __future__ import annotations

import pytest

from app.api.endpoints import fleet
from scripts import sidecar


def test_pass_through_provider_sets_match() -> None:
    assert fleet.PASS_THROUGH_PROVIDERS == sidecar._PASS_THROUGH_EVENT_PROVIDERS
    assert fleet.PASS_THROUGH_PROVIDER_PREFIX == sidecar._PASS_THROUGH_PROVIDER_PREFIX


@pytest.mark.parametrize(
    ("provider_id", "expected"),
    [
        ("hermes", True),
        ("hermes-nous", True),
        ("hermes-openai", True),
        ("minimax", False),
        ("opencode", False),
        ("kimi_coding", False),
    ],
)
def test_pass_through_predicates_agree(provider_id: str, expected: bool) -> None:
    assert fleet.is_pass_through_provider(provider_id) is expected
    assert sidecar._is_pass_through_provider(provider_id) is expected
