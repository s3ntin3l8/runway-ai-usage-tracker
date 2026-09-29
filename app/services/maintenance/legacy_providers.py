"""Legacy OpenCode-sibling provider ids that main no longer emits.

``scripts/sidecar_pkg/event_extractors/opencode.py`` maps an OpenCode
``providerID`` to a runway ``provider_id`` two ways: a plain sibling id
(``_OC_PROVIDER_MAP``, e.g. ``"ollama-cloud" -> "opencode-ollama"``) or, for
backends Runway already collects directly, a fold-in onto the canonical
provider (``_OC_CANONICAL_MAP``, e.g. ``"xai" -> "xai"``). A database that
ingested events before a given ``_OC_CANONICAL_MAP`` entry existed — or
before the sidecar build that added it — still carries rows under the old
derived sibling id (``opencode-xai``, ``opencode-openrouter``, ...).

``LEGACY_PROVIDER_MAP`` is the reverse of that fold: sibling id -> canonical
id, for every entry ``_OC_CANONICAL_MAP`` currently folds. The Data Health
``legacy_provider_ids`` check uses it to find and retag those leftover rows.

The sidecar's own maps are the source of truth and are not imported from
here — ``app/`` ships in the server image, not ``scripts/``, and the sidecar
PyInstaller specs list hidden imports explicitly (moving the maps would be a
release-time risk). This module keeps a mirrored copy instead; a contract
test (``tests/unit/test_legacy_provider_map_contract.py``) asserts the two
never drift apart.
"""

from __future__ import annotations

# Mirrors scripts/sidecar_pkg/event_extractors/opencode.py::_OC_PROVIDER_MAP.
# Keep both in sync — see the contract test.
_OC_PROVIDER_MAP: dict[str, str] = {
    "opencode": "opencode-free",
    "opencode-go": "opencode",
    "opencode-zen": "opencode-zen",
    "open-design-byok": "opencode-byok",
    "openrouter": "opencode-openrouter",
    "ollama-cloud": "opencode-ollama",
}

# Mirrors scripts/sidecar_pkg/event_extractors/opencode.py::_OC_CANONICAL_MAP.
# Only the keys (and the fact that a canonical id exists) matter here — the
# account-override half of the tuple is a per-event ingest concern, not a
# retag concern. Keep both in sync — see the contract test.
_OC_CANONICAL_MAP_KEYS: dict[str, str] = {
    "minimax-coding-plan": "minimax",
    "kimi-code-plan-global": "kimi_coding",
    "ollama-cloud": "ollama",
    "openrouter": "openrouter",
    "xai": "xai",
    "deepseek": "deepseek",
}


def _derive_legacy_provider_map() -> dict[str, str]:
    """sibling provider_id -> canonical provider_id, for every canonical entry.

    Mirrors scripts/reclassify_opencode_providers.py::_RESCAN_PROVIDERS'
    derivation exactly: for each OpenCode providerID that has a canonical
    fold-in target, resolve the sibling id that map_opencode_provider_id
    would have derived for it — an explicit entry in _OC_PROVIDER_MAP, else
    the generic "opencode-<slug>" fallback.
    """
    return {
        _OC_PROVIDER_MAP.get(oc_provider_id, f"opencode-{oc_provider_id}"): canonical_id
        for oc_provider_id, canonical_id in _OC_CANONICAL_MAP_KEYS.items()
    }


LEGACY_PROVIDER_MAP: dict[str, str] = _derive_legacy_provider_map()
