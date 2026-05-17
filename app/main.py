import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlmodel import Session, select
from starlette.responses import Response

from app.api.routes import router as api_router
from app.core.config import settings
from app.core.db import get_session, init_db
from app.core.rate_limit import limiter
from app.models.db import SystemConfig
from app.services.collector_manager import manager
from app.services.poller import poller

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

    yield
    # Shutdown logic
    await poller.stop()
    await manager.close()


app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)
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


# Defence-in-depth security headers on every response. CSP is the meaningful
# guard against the dashboard's `innerHTML` exposure: even if escapeHTML*
# misses something, a CSP-blocked external <script> can't run. We keep
# 'unsafe-inline' for now because components.js emits `onclick="..."` strings;
# migrating those to addEventListener is a follow-up. fonts.googleapis.com
# and fonts.gstatic.com are whitelisted to keep the existing B612 font load.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
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
    logger.error(f"Global exception caught: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


# API routes
app.include_router(api_router, prefix="/api")

# Serve static files (frontend). Default StaticFiles sets ETag/Last-Modified so
# the browser can do conditional If-Modified-Since requests and receive 304s
# when nothing changed. index.html already cache-busts subresources via
# ?v=N query params (see <link href="/static/css/styles.css?v=7">), so bumping
# that param is the explicit signal to force a re-download on real updates.
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/static", StaticFiles(directory=frontend_path), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Prefer SVG for transparency, fallback to PNG
    for ext in ["svg", "png"]:
        icon_path = os.path.join(frontend_path, f"favicon.{ext}")
        if os.path.exists(icon_path):
            return FileResponse(icon_path)
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def dashboard(db: Session = Depends(get_session)):
    """Serve the main dashboard page, always reading from disk."""
    index_file = os.path.join(frontend_path, "index.html")
    if not os.path.exists(index_file):
        return HTMLResponse("<h1>Frontend index.html not found!</h1>", status_code=404)
    with open(index_file) as f:
        content = f.read()

    # Inject timezone config synchronously so getUserTz() works before any
    # async fetch completes — avoids charts rendering in UTC on first load.
    cfg = db.exec(select(SystemConfig)).first()
    user_tz = (cfg.user_timezone if cfg else None) or ""
    env_tz = settings.env_timezone or ""
    tz_script = (
        f"<script>window.runwayConfig="
        f'{{"user_timezone":{user_tz!r},"env_timezone":{env_tz!r}}};</script>'
    )
    content = content.replace("</head>", f"{tz_script}</head>", 1)

    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Runway on http://{settings.APP_HOST}:{settings.APP_PORT}")
    if settings.APP_HOST == "0.0.0.0":
        logger.warning("Server bound to 0.0.0.0 - accessible from all network interfaces!")
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
