"""Contract tests for canonical provider mappings shared by extractors."""

from scripts.sidecar_pkg.canonical_providers import SHARED_CANONICAL_PROVIDER_MAP
from scripts.sidecar_pkg.event_extractors.hermes import _HERMES_CANONICAL_MAP
from scripts.sidecar_pkg.event_extractors.opencode import _OC_CANONICAL_MAP


def test_shared_canonical_provider_mappings():
    assert SHARED_CANONICAL_PROVIDER_MAP == {
        "minimax-coding-plan": ("minimax", None),
        "ollama-cloud": ("ollama", None),
        "openrouter": ("openrouter", None),
        "deepseek": ("deepseek", None),
        "xai": ("xai", None),
    }


def test_extractors_extend_shared_canonical_provider_mappings_without_drift():
    assert _OC_CANONICAL_MAP == {
        **SHARED_CANONICAL_PROVIDER_MAP,
        "kimi-code-plan-global": ("kimi_coding", None),
    }
    assert _HERMES_CANONICAL_MAP == {
        **SHARED_CANONICAL_PROVIDER_MAP,
        "kimi-coding": ("kimi_coding", None),
        "minimax": ("minimax", None),
        "minimax-oauth": ("minimax", None),
        "opencode-go": ("opencode", None),
        "opencode-zen": ("opencode", None),
        "opencode": ("opencode", None),
        "deepseek-api": ("deepseek", None),
        "anthropic": ("anthropic", None),
        "gemini": ("gemini", None),
        "ollama": ("ollama", None),
    }
