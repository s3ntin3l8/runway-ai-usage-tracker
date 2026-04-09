from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response
from app.api.routes import router as api_router
from app.core.config import settings
import os
import logging
import sys
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.PROJECT_NAME)

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
        content={"detail": "Internal Server Error", "message": str(exc)}
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
            "Expires": "0"
        }
    )

if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting Runway on http://{settings.APP_HOST}:{settings.APP_PORT}")
    if settings.APP_HOST == "0.0.0.0":
        logger.warning("Server bound to 0.0.0.0 - accessible from all network interfaces!")
    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
