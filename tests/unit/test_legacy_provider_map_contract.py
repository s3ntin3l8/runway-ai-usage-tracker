"""Contract test: app/services/maintenance/legacy_providers.py's mirrored
copies of the sidecar's OpenCode provider maps must never drift from the
real source, and the derived LEGACY_PROVIDER_MAP must match what
scripts/reclassify_opencode_providers.py actually rescans.

app/ ships in the server image; scripts/sidecar_pkg/ does not run there, so
legacy_providers.py can't import the real maps directly — it keeps a
mirrored copy instead. This test is what keeps that copy honest.
"""

from __future__ import annotations

from app.services.maintenance.legacy_providers import (
    _OC_CANONICAL_MAP_KEYS,
    _OC_PROVIDER_MAP,
    LEGACY_PROVIDER_MAP,
)
from scripts.reclassify_opencode_providers import _RESCAN_PROVIDERS
from scripts.sidecar_pkg.event_extractors.opencode import (
    _OC_CANONICAL_MAP,
)
from scripts.sidecar_pkg.event_extractors.opencode import (
    _OC_PROVIDER_MAP as _REAL_OC_PROVIDER_MAP,
)


def test_provider_map_mirror_matches_the_sidecar_source():
    assert _OC_PROVIDER_MAP == _REAL_OC_PROVIDER_MAP


def test_canonical_map_keys_mirror_matches_the_sidecar_source():
    real_keys = {key: canonical_id for key, (canonical_id, _override) in _OC_CANONICAL_MAP.items()}
    assert _OC_CANONICAL_MAP_KEYS == real_keys


def test_legacy_provider_map_matches_reclassify_scripts_rescan_scope():
    """Every sibling id LEGACY_PROVIDER_MAP retags is exactly one that
    scripts/reclassify_opencode_providers.py would also rescan (minus the
    two historically-buggy bare ids that script special-cases up front,
    which never had a canonical-fold sibling id to derive)."""
    old_providers = {"opencode", "opencode-free"}
    assert set(LEGACY_PROVIDER_MAP) == set(_RESCAN_PROVIDERS) - old_providers


def test_legacy_provider_map_known_entries():
    """Pins the concrete mapping so a future edit to either source map
    surfaces here, not just via the drift-detection tests above."""
    assert LEGACY_PROVIDER_MAP == {
        "opencode-minimax-coding-plan": "minimax",
        "opencode-kimi-code-plan-global": "kimi_coding",
        "opencode-ollama": "ollama",
        "opencode-openrouter": "openrouter",
        "opencode-xai": "xai",
        "opencode-deepseek": "deepseek",
    }
