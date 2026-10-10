"""Server URL policy and redirect-free sidecar HTTP requests."""

import threading
import time
from urllib import error, request
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ServerURLError(OSError):
    """A configuration error suitable for displaying without credentials."""


def validate_server_url(url: str, *, base: bool = False) -> str:
    """Allow remote HTTPS and canonical loopback HTTP, without URL credentials."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise ServerURLError("Server URL must not contain whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise ServerURLError("Invalid server URL") from None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ServerURLError("Server URL must use HTTPS (HTTP is allowed only on loopback)")
    if (
        parts.username is not None
        or parts.password is not None
        or parts.fragment
        or (base and parts.query)
    ):
        raise ServerURLError(
            "Server URL must not contain credentials, fragments or base URL parameters"
        )
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ServerURLError(
            "Remote sidecars require https://; http:// is allowed only on loopback"
        )
    return url.rstrip("/") if base else url


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise error.HTTPError(
            req.full_url, code, "Sidecar server redirects are refused", headers, fp
        )


def urlopen(req: request.Request, *, timeout: int, context=None):  # type: ignore[no-untyped-def]
    """Reject redirects before any signed headers/body reach another address."""
    validate_server_url(req.full_url)
    opener = request.build_opener(_NoRedirect(), request.HTTPSHandler(context=context))
    return opener.open(req, timeout=timeout)


_TIMESTAMP_LOCK = threading.Lock()
_LAST_TIMESTAMP_US = 0


def signing_timestamp() -> str:
    """Distinct timestamps even for immediate retries or simultaneous callers."""
    global _LAST_TIMESTAMP_US
    with _TIMESTAMP_LOCK:
        _LAST_TIMESTAMP_US = max(time.time_ns() // 1000, _LAST_TIMESTAMP_US + 1)
        return f"{_LAST_TIMESTAMP_US // 1_000_000}.{_LAST_TIMESTAMP_US % 1_000_000:06d}"
