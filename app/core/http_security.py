"""Browser boundaries for the API's loopback authentication mode."""

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class BrowserBoundaryMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from urllib.parse import urlsplit

        from app.core.config import settings

        headers = Headers(scope=scope)
        host = headers.get("host", "")
        try:
            hostname = urlsplit(f"//{host}").hostname
        except ValueError:
            hostname = None
        # A loopback TCP peer can be a browser following a rebound DNS name,
        # or a LAN-facing dev proxy. Neither inherits local administrator trust.
        if settings.APP_HOST in _LOCAL_HOSTS and hostname not in _LOCAL_HOSTS:
            await JSONResponse({"detail": "Untrusted Host"}, status_code=403)(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin and scope["method"] != "OPTIONS":
            same_origin = f"{scope['scheme']}://{host}"
            if origin != same_origin and origin not in settings.CORS_ORIGINS:
                await JSONResponse({"detail": "Untrusted Origin"}, status_code=403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
