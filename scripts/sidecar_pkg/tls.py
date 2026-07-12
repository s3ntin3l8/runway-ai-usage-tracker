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
