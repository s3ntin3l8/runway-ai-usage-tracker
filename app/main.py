import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.responses import Response

from app.api.routes import router as api_router
from app.core.config import settings
from app.core.db import init_db
from app.core.rate_limit import limiter
from app.services.collector_manager import manager
from app.services.poller import poller
from app.services.sidecar_version_checker import sidecar_version_checker
from app.services.token_auto_refresher import token_auto_refresher

# Propagate the resolved display tz into the process so log %(asctime)s and
# any libc-backed time formatting render in the user's local zone. Pydantic
# Settings reads TZ from .env into settings.env_timezone but does not export
# it to os.environ — do that here, then refresh libc's tz cache.
if settings.env_timezone:
    os.environ["TZ"] = settings.env_timezone
if hasattr(time, "tzset"):
    time.tzset()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Silence noisy httpx logs
logging.getLogger("httpx").setLevel(logging.WARNING)
# Apply JSON formatter when LOG_FORMAT=json
if settings.LOG_FORMAT == "json":
    from app.core.logging import JsonFormatter

    _json_fmt = JsonFormatter()
    for _handler in logging.root.handlers:
        _handler.setFormatter(_json_fmt)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB
    init_db()

    # Remove stale error rows superseded by healthy rows (idempotent cleanup).
    try:
        from sqlmodel import Session as _Session

        from app.core.db import engine as _engine
        from app.services.accumulator import evict_orphan_error_rows

        with _Session(_engine) as _s:
            evict_orphan_error_rows(_s)
            _s.commit()
    except Exception as _e:
        logger.warning(f"Startup orphan error row cleanup failed: {_e}")

    # Pre-populate in-memory registry so the first /limits request is instant
    try:
        logger.info("Pre-populating card registry on startup (timeout 15s)...")
        initial_cards = await asyncio.wait_for(manager.collect_all(), timeout=15.0)
        # Registry updated inside _do_collect; just log the count here.
        logger.info(f"Registry pre-populated with {len(initial_cards)} cards")
    except TimeoutError:
        logger.warning("Startup collection timed out after 15s — registry empty until first poll")
    except Exception as e:
        logger.warning(f"Startup collection failed — registry empty until first poll: {e}")

    # Start background poller (keeps registry fresh every 15 min)
    poller.start()

    # Start background token auto-refresher (rolls JWTs before they expire,
    # independent of poller cadence).
    if settings.TOKEN_AUTO_REFRESH_ENABLED:
        token_auto_refresher.start()

    # Start background sidecar version checker (polls GitHub once on start +
    # every 24h so the fleet view can flag outdated sidecars).
    sidecar_version_checker.start()

    yield
    # Shutdown logic
    await poller.stop()
    await token_auto_refresher.stop()
    await sidecar_version_checker.stop()
    await manager.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore

# Cache for dashboard HTML

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gzip text responses (CSS, JS, HTML, JSON). Browsers without Accept-Encoding
# still get uncompressed bytes; the 512-byte floor skips tiny payloads where
# compression overhead exceeds the savings.
app.add_middleware(GZipMiddleware, minimum_size=512)


# Defence-in-depth security headers on every response. The v2 SPA bundles
# all scripts and fonts (no CDN, no inline handlers), so script-src is a
# strict 'self'. style-src keeps 'unsafe-inline' for the style *attributes*
# that ECharts and Radix set at runtime — scripts are the meaningful guard.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def _add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# Global Exception Handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Account IDs in this codebase are emails; tracebacks that close over
    # account-id-shaped values would otherwise emit them straight to the
    # log handlers. Format the traceback ourselves, scrub email-shaped
    # substrings, then emit. exc_info=False because we've already
    # rendered + scrubbed the trace.
    import traceback

    from app.core.log_redaction import scrub_pii

    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("Global exception caught: %s\n%s", scrub_pii(str(exc)), scrub_pii(rendered))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


# API routes
app.include_router(api_router, prefix="/api")

# Serve the built SPA (webapp/dist). Vite emits content-hashed filenames
# under /assets, so those get an immutable cache policy; index.html itself
# is always revalidated and is returned for every non-API path (the SPA
# router owns deep links like /provider/anthropic). The app fetches its
# timezone config from /api/v1/system/app-config at boot, which is what
# allows script-src to stay 'self' (no injected inline script).
frontend_path = os.path.join(os.path.dirname(__file__), "..", "webapp", "dist")
_assets_path = os.path.join(frontend_path, "assets")
if os.path.isdir(_assets_path):
    app.mount("/assets", StaticFiles(directory=_assets_path), name="assets")


class _ImmutableAssetsMiddleware:
    """Long-lived caching for hashed /assets files."""

    def __init__(self, app):  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http" and scope["path"].startswith("/assets/"):

            async def send_with_cache(message):  # noqa: ANN001
                if message["type"] == "http.response.start":
                    headers = [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() != b"cache-control"
                    ]
                    headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_cache)
            return
        await self.app(scope, receive, send)


app.add_middleware(_ImmutableAssetsMiddleware)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Prefer SVG for transparency, fallback to PNG
    for ext in ["svg", "png"]:
        icon_path = os.path.join(frontend_path, f"favicon.{ext}")
        if os.path.exists(icon_path):
            return FileResponse(icon_path)
    return Response(status_code=204)


_SPA_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _spa_index() -> HTMLResponse:
    index_file = os.path.join(frontend_path, "index.html")
    if not os.path.exists(index_file):
        return HTMLResponse(
            "<h1>Frontend build not found — run `make web` (npm run web:build).</h1>",
            status_code=404,
        )
    with open(index_file) as f:
        return HTMLResponse(content=f.read(), headers=_SPA_NO_CACHE)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the SPA entry, always reading from disk."""
    return _spa_index()


@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
async def spa_catch_all(path: str):
    """SPA fallback: client-side routes resolve to index.html.

    Registered after the API router, so /api/* never lands here. Dotted
    paths are real files: serve them from dist (Vite `public/` output like
    favicon.svg) or 404 — never HTML, which would mask missing assets.
    """
    if path.startswith(("api/", "assets/")) or "." in path.rsplit("/", 1)[-1]:
        file_path = os.path.realpath(os.path.join(frontend_path, path))
        if file_path.startswith(os.path.realpath(frontend_path) + os.sep) and os.path.isfile(
            file_path
        ):
            return FileResponse(file_path)
        return Response(status_code=404)
    return _spa_index()


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Runway on http://{settings.APP_HOST}:{settings.APP_PORT}")
    if settings.APP_HOST == "0.0.0.0":
        logger.warning("Server bound to 0.0.0.0 - accessible from all network interfaces!")
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
