"""Canonical provider mappings shared by sidecar event extractors.

Keys are upstream provider strings and values are the canonical Runway
provider ID plus an optional explicit account ID override.
"""

SHARED_CANONICAL_PROVIDER_MAP: dict[str, tuple[str, str | None]] = {
    "minimax-coding-plan": ("minimax", None),
    "ollama-cloud": ("ollama", None),
    "openrouter": ("openrouter", None),
    # OpenCode BYOK DeepSeek uses the direct balance; its Go subscription
    # stays on opencode, so only the exact deepseek provider is mapped here.
    # See the OpenCode module docstring for the account attribution details.
    "deepseek": ("deepseek", None),
    "xai": ("xai", None),
}
