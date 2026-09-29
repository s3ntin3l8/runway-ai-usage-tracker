"""Canonical provider mappings shared by sidecar event extractors.

Keys are upstream provider strings and values are the canonical Runway
provider ID plus an optional explicit account ID override.
"""

CanonicalProviderTuple = tuple[str, str | None]

SHARED_CANONICAL_PROVIDER_MAP: dict[str, CanonicalProviderTuple] = {
    "minimax-coding-plan": ("minimax", None),
    "ollama-cloud": ("ollama", None),
    "openrouter": ("openrouter", None),
    # OpenCode BYOK DeepSeek is billed against the direct DeepSeek balance;
    # opencode-go subscription traffic stays on opencode. OpenCode does not
    # identify which BYOK credential handled a message, so account selection
    # comes from operator hints or pending assignment.
    "deepseek": ("deepseek", None),
    "xai": ("xai", None),
}
