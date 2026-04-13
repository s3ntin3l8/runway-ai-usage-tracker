from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from app.api.routes import router as api_router
from app.services.collector_manager import manager
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.db import init_db
from app.services.poller import poller
import os
import logging
import sys
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path


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
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB
    init_db()

    # Pre-populate in-memory registry so the first /limits request is instant
    try:
        logger.info("Pre-populating card registry on startup...")
        initial_cards = await manager.collect_all()
        # Registry updated inside _do_collect; just log the count here.
        logger.info(f"Registry pre-populated with {len(initial_cards)} cards")
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
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Cache for dashboard HTML
_DASHBOARD_HTML_CACHE = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

# Serve static files (frontend) with cache-busting headers
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles with no-cache headers for development."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        # Add cache-busting headers
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.mount("/static", NoCacheStaticFiles(directory=frontend_path), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Prefer SVG for transparency, fallback to PNG
    for ext in ["svg", "png"]:
        icon_path = os.path.join(frontend_path, f"favicon.{ext}")
        if os.path.exists(icon_path):
            return FileResponse(icon_path)
    return Response(status_code=204)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main dashboard page with caching."""
    global _DASHBOARD_HTML_CACHE
    if _DASHBOARD_HTML_CACHE is None:
        index_file = os.path.join(frontend_path, "index.html")
        if os.path.exists(index_file):
            with open(index_file, "r") as f:
                _DASHBOARD_HTML_CACHE = f.read()
        else:
            return "<h1>Frontend index.html not found!</h1>"

    # Return with no-cache headers to ensure sidecar updates are visible immediately in state
    return HTMLResponse(
        content=_DASHBOARD_HTML_CACHE,
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
        logger.warning(
            "Server bound to 0.0.0.0 - accessible from all network interfaces!"
        )
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
