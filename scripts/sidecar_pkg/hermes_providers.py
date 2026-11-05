"""Hermes billing-provider resolution.

Split out of ``hermes.py`` so the provider map can change without the
watermark log or the event parser. ``hermes`` re-exports every name here;
callers keep importing ``scripts.sidecar_pkg.event_extractors.hermes``.

This is not an extractor, so it stays out of ``event_extractors``: that
package is one module per provider, and each of those modules stamps a
``source_app``.
"""

from __future__ import annotations

from urllib.parse import urlparse

from scripts.sidecar_pkg.canonical_providers import (
    SHARED_CANONICAL_PROVIDER_MAP,
    CanonicalProviderTuple,
)

# Upstream billing_provider -> (canonical provider_id, explicit account override or None).
_HERMES_CANONICAL_MAP: dict[str, CanonicalProviderTuple] = {
    **SHARED_CANONICAL_PROVIDER_MAP,
    "kimi-coding": ("kimi_coding", None),
    "kimi": ("kimi_coding", None),
    "minimax": ("minimax", None),
    "minimax-oauth": ("minimax", None),
    "opencode-go": ("opencode", None),
    "opencode-zen": ("opencode", None),
    "opencode": ("opencode", None),
    "opencode-free": ("opencode-free", None),
    "deepseek-api": ("deepseek", None),
    "anthropic": ("anthropic", None),
    "gemini": ("gemini", None),
    "ollama": ("ollama", None),
    "xai-oauth": ("xai", None),
    "xai-api": ("xai", None),
}


def _is_free_model(model: str) -> bool:
    """Return True if model name indicates a free-tier model.

    Deliberately narrower than `app.services.model_normalization.is_free_tier_model_id`,
    which also matches the "openrouter/free/*" prefix form. This one decides
    which provider bucket a Hermes session files under; the shared one decides
    whether an id may be billed at a paid rate. Widening this would silently
    re-route "openrouter/free/*" sessions from "opencode" to "opencode-free".
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    if m.endswith((":free", "-free", "/free")):
        return True
    last_token = m.replace(":", "/").split("/")[-1]
    return last_token == "free" or last_token.endswith("-free")


def _url_host_matches(url: str, *domains: str) -> bool:
    """Safely check if a URL's hostname matches or is a subdomain of any candidate domain."""
    if not url:
        return False
    try:
        raw = url.strip()
        if "://" not in raw:
            raw = f"https://{raw}"
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        for domain in domains:
            d = domain.strip().lower()
            if host == d or host.endswith(f".{d}"):
                return True
        return False
    except Exception:
        return False


def map_hermes_canonical(billing_provider: str) -> tuple[str, str | None] | None:
    """Return the canonical provider and optional explicit account override."""
    return _HERMES_CANONICAL_MAP.get((billing_provider or "").strip().lower())


def map_hermes_provider_id(billing_provider: str) -> str:
    """Map a Hermes billing_provider to a Runway provider_id."""
    bp = (billing_provider or "").strip().lower()
    if not bp:
        return "hermes"
    canonical = map_hermes_canonical(bp)
    if canonical:
        return canonical[0]
    return f"hermes-{bp}"


def resolve_hermes_provider_and_canonical(
    billing_provider: str,
    billing_base_url: str,
    model: str,
    session_billing_provider: str = "",
    session_billing_base_url: str = "",
    session_model: str = "",
    session_billing_mode: str = "",
) -> tuple[str, CanonicalProviderTuple | None]:
    """Resolve raw and session metadata to (target_provider_id, canonical_tuple_or_None)."""
    bp = (billing_provider or "").strip().lower()
    sbp = (session_billing_provider or "").strip().lower()
    base_url = (billing_base_url or session_billing_base_url or "").strip().lower()
    m = (model or session_model or "").strip()
    m_lower = m.lower()

    # 1. Model-specific heuristics when billing_provider is missing, hermes, or generic
    if not bp or bp in ("hermes", "auto", "default", "custom"):
        if m_lower.startswith("kimi-") or m_lower == "kimi-for-coding":
            bp = "kimi-coding"
        elif m_lower.startswith("grok-"):
            bp = "xai-oauth"
        elif m_lower.startswith("minimax-") or m_lower == "minimax":
            bp = "minimax-oauth"
        elif sbp and sbp not in ("hermes", "auto", "default", "custom"):
            bp = sbp
        elif _url_host_matches(base_url, "opencode.ai") or sbp.startswith("opencode"):
            bp = "opencode"
        elif _url_host_matches(base_url, "api.x.ai", "x.ai"):
            bp = "xai-oauth"
        elif _url_host_matches(
            base_url, "api.kimi.com", "kimi.com", "api.moonshot.cn", "moonshot.cn"
        ):
            bp = "kimi-coding"
        elif _url_host_matches(base_url, "api.minimax.io", "minimax.io"):
            bp = "minimax-oauth"
        elif _url_host_matches(base_url, "openrouter.ai"):
            bp = "openrouter"

    # 2. OpenCode tier splitting: free models vs paid/subscription
    is_opencode = (
        bp in ("opencode", "opencode-go", "opencode-zen", "auto")
        or _url_host_matches(base_url, "opencode.ai")
        or sbp.startswith("opencode")
    )
    if is_opencode:
        mode = (session_billing_mode or "").strip().lower()
        if mode == "free" or _is_free_model(m):
            return "opencode-free", ("opencode-free", None)
        return "opencode", ("opencode", None)

    # 3. Canonical map lookup
    canonical = map_hermes_canonical(bp)
    if canonical is not None:
        return canonical[0], canonical

    # 4. Fallback provider ID
    provider_id = map_hermes_provider_id(bp)
    return provider_id, None
