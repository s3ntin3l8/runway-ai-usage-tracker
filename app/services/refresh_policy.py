"""Who may refresh an OAuth login (pure rules; no I/O, no cache imports).

Kept a leaf module so both ``token_cache`` and ``token_refresher`` can use it without
importing each other.
"""

from __future__ import annotations

# Providers whose token endpoint rotates the refresh token: a refresh invalidates the
# previous one. Refreshing a credential that a machine's own CLI also holds therefore
# logs that CLI out, and the new token is never sent back to it. Google does not rotate
# Gemini's, so server-side refresh is safe there. xAI does rotate: verified 2026-10-04
# against auth.x.ai — every refresh returns a *new* refresh token (the previous one kept
# working for at least a moment afterwards, so there is a reuse window, but the CLI's copy
# is not the live one once anyone else refreshes). Refreshing without writing the result
# back to the CLI's file would strand it, which is why only the sidecar keep-alive
# (scripts/sidecar_pkg/xai_renewer.py, which writes back) renews a machine-owned xAI login.
ROTATING_REFRESH_PROVIDERS = frozenset({"anthropic", "chatgpt", "xai"})

# Providers whose sidecar can renew a machine-owned login itself (``--keep-alive``):
# agy via ``agy models``, xAI by refreshing and writing the token back to the CLI's file.
KEEP_ALIVE_PROVIDERS = frozenset({"xai", "antigravity"})

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
