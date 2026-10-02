"""Who may refresh an OAuth login (pure rules; no I/O, no cache imports).

Kept a leaf module so both ``token_cache`` and ``token_refresher`` can use it without
importing each other.
"""

from __future__ import annotations

# Providers whose token endpoint rotates the refresh token: a refresh invalidates the
# previous one. Refreshing a credential that a machine's own CLI also holds therefore
# logs that CLI out, and the new token is never sent back to it. Google does not rotate
# Gemini's, so server-side refresh is safe there. xAI is treated as rotating without
# having been verified: wrongly blocking only pauses data until the CLI next runs,
# while wrongly allowing logs the user out.
ROTATING_REFRESH_PROVIDERS = frozenset({"anthropic", "chatgpt", "xai"})

_NON_MACHINE_SOURCES = (None, "server", "config", "manual_config")


def _refresh_secret(tokens: dict[str, str]) -> str | None:
    return tokens.get("refresh_token") or tokens.get("xai_refresh") or None


def is_machine_bundle(bundle: dict) -> bool:
    """A source bundle pushed by a sidecar. Any ``sidecar_id`` counts, even without a
    ``credential_origin`` (an older sidecar's push): wrongly calling it server-owned would
    rotate a CLI's token, wrongly calling it a machine's only pauses the refresh."""
    return bool(bundle.get("sidecar_id"))


def machine_owns_credential(
    provider: str,
    tokens: dict[str, str],
    bundles: list[dict] | tuple[dict, ...] = (),
    *,
    merged_source: str | None = None,
) -> bool:
    """True when a machine's CLI owns this OAuth credential, so the server must not refresh it.

    Only rotating providers qualify. *tokens* is owned by a machine when it shares its
    refresh secret with a sidecar source bundle (the same matching
    ``TokenCache.apply_refresh_to_sources`` uses), or when the merged entry says a sidecar
    last pushed it (``merged_source``). The shared-secret test is primary: the merged
    entry's ``source`` is just whichever push came last. A sidecar as the merged source is
    treated as ownership on its own (conservative: it can only pause a refresh, never rotate
    a CLI's token).

    A CLI's login file on the server host is a separate rule: ``CredentialProvider.
    is_cli_owned_file`` (the collector declines to refresh it), since a merged ``"server"``
    source cannot tell that file from an env var (see docs/architecture.md).
    """
    if provider not in ROTATING_REFRESH_PROVIDERS:
        return False
    if merged_source not in _NON_MACHINE_SOURCES:
        return True
    secret = _refresh_secret(tokens)
    if not secret:
        return False
    return any(
        is_machine_bundle(bundle) and _refresh_secret(bundle.get("tokens") or {}) == secret
        for bundle in bundles
    )
