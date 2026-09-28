"""Shared TLS trust-store helper for the sidecar (stdlib + optional certifi).

The frozen PyInstaller sidecar ships no system CA store, so urllib's default
certificate verification fails on some hosts (notably macOS) even against a
perfectly valid public cert. Resolving the SSL context through bundled certifi
— and honouring an explicit CA bundle or an insecure opt-in — fixes both the
data-push path (`scripts/sidecar.py`) and the GitHub self-update path
(`update_check` / `self_update`).

Like the rest of `scripts.sidecar_pkg`, this module uses only the standard
library plus an *optional* `certifi`; it never imports `app.*` or
`scripts.sidecar`, so the frozen binary stays self-contained and there is no
import cycle.
"""

from __future__ import annotations

import logging
import os
import ssl

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def _certifi_cafile() -> str | None:
    """Path to certifi's CA bundle, or None when certifi isn't bundled."""
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def build_context(
    url: str | None = None,
    *,
    ca_bundle: str | None = None,
    insecure: bool | None = None,
) -> ssl.SSLContext | None:
    """Resolve a TLS context for an HTTPS *url*.

    Returns ``None`` when *url* is a plaintext ``http://`` endpoint (urllib then
    needs no context). For HTTPS — or when *url* is omitted — resolution order is:

      1. *insecure* opt-in (or ``RUNWAY_INSECURE`` env) → verification disabled.
      2. *ca_bundle* / ``RUNWAY_CA_BUNDLE`` / ``SSL_CERT_FILE`` → custom CA file.
      3. bundled ``certifi`` → ``certifi.where()``.
      4. OpenSSL system default.
    """
    if url is not None and not url.lower().startswith("https"):
        return None

    if insecure is None:
        insecure = os.environ.get("RUNWAY_INSECURE", "").strip().lower() in _TRUTHY
    if insecure:
        logger.warning("RUNWAY_INSECURE set — TLS certificate verification DISABLED")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    ca = ca_bundle or os.environ.get("RUNWAY_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca and os.path.exists(ca):
        return ssl.create_default_context(cafile=ca)

    cafile = _certifi_cafile()
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    if cafile:
        # certifi.where() resolved to a path that no longer exists — typically a
        # PyInstaller onefile extraction dir (/tmp/_MEIxxxx) reaped out from under
        # a long-running daemon. Fall through to the OS trust store rather than
        # raising FileNotFoundError on every send.
        logger.warning(
            "certifi CA bundle missing at %s (frozen runtime likely reaped) — "
            "falling back to OS default trust store",
            cafile,
        )
    return ssl.create_default_context()


def build_context_from_config(url: str | None, config: dict | None = None) -> ssl.SSLContext | None:
    """`build_context` for a caller that only has the sidecar's raw config dict.

    Extracts the `ca_bundle` / `tls_insecure` config keys the same way for
    every caller, so the truthy-string parsing for `tls_insecure` (config
    values arrive as JSON, so it may be a real bool or a string like "true")
    lives in exactly one place. Used by `sidecar.build_ssl_context` and every
    `scripts.sidecar_pkg` module that talks to the Runway server over HTTPS —
    a caller that calls `build_context(url)` directly instead of through here
    silently ignores an operator's `ca_bundle`/`tls_insecure` config.
    GitHub update checks and downloads intentionally use `build_context`
    with normal public certificate verification instead.
    """
    config = config or {}
    insecure_raw = str(config.get("tls_insecure", "")).strip().lower()
    insecure = True if insecure_raw in _TRUTHY else None
    return build_context(url, ca_bundle=config.get("ca_bundle"), insecure=insecure)
