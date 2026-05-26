from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import settings


def _client_key(request: Request) -> str:
    """Resolve the IP to rate-limit on.

    Behind a reverse proxy, `request.client.host` is the proxy itself, so
    every real client would collapse into a single bucket. When the
    request comes from a trusted proxy IP we honour the left-most entry
    of X-Forwarded-For (the original client). Untrusted sources can't
    forge their way into a different bucket because we only consult the
    header for source IPs in the configured allowlist.
    """
    direct = get_remote_address(request)
    trusted = settings.trusted_proxy_ips
    if trusted and direct in trusted:
        xff = request.headers.get("X-Forwarded-For")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return direct


limiter = Limiter(key_func=_client_key)
