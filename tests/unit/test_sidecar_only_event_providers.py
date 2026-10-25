"""Drift guard: the scheduling-only Hermes id matches the sidecar's event-only providers."""

import json
import re
from pathlib import Path

from app.api.endpoints.fleet import SIDECAR_ONLY_EVENT_PROVIDERS

ROOT = Path(__file__).resolve().parents[2]


def _registry_ids() -> set[str]:
    data = json.loads((ROOT / "app/core/registry.json").read_text(encoding="utf-8"))
    return set(data["providers"])


def test_constant_matches_overlay_add_providers():
    overlay = json.loads(
        (ROOT / "scripts/sidecar_registry_overlay.json").read_text(encoding="utf-8")
    )
    assert set(overlay["add_providers"]) == SIDECAR_ONLY_EVENT_PROVIDERS


def test_constant_matches_event_providers_missing_from_registry():
    from scripts.sidecar import _EVENT_PROVIDERS

    assert set(_EVENT_PROVIDERS) - _registry_ids() == SIDECAR_ONLY_EVENT_PROVIDERS
    assert not SIDECAR_ONLY_EVENT_PROVIDERS & _registry_ids()


def test_constant_ids_are_plain_ids():
    assert all(re.fullmatch(r"[a-z0-9_-]+", p) for p in SIDECAR_ONLY_EVENT_PROVIDERS)


def test_minimax_code_is_scheduled_as_a_client_app():
    """The MiniMax Code runtime ledger is scheduled under its own id.

    Its events belong to the ``minimax`` *provider*, so the scheduling id
    must not be a provider the server knows — and the ledger must not be
    tied to the host having a MiniMax account (#928).
    """
    assert "minimax_code" in SIDECAR_ONLY_EVENT_PROVIDERS
    assert "minimax_code" not in _registry_ids()
    # The provider the events are filed under is untouched by the split.
    assert "minimax" in _registry_ids()
