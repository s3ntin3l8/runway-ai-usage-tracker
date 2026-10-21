# Stage 1: Build the SPA
FROM node:24-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5 AS frontend-builder
WORKDIR /app
COPY webapp/package*.json ./webapp/
RUN npm --prefix webapp ci
COPY webapp/ ./webapp/
RUN npm --prefix webapp run build

# Stage 2: Build Python dependencies
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS python-builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc=4:12.2.0-3 \
    libsqlite3-dev=3.40.1-2+deb12u2 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

# Stage 3: Final Image
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
# Create non-root user with home directory
RUN groupadd -r runway && useradd -r -g runway -u 1000 -m runway

WORKDIR /app

# Refresh the entire runtime distro, not only individually installed packages.
# Build tools and pip never belong in the application runtime.
RUN apt-get update && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/bin/python -m pip uninstall -y pip setuptools wheel \
    && find /usr -type f -perm /6000 -exec chmod a-s {} +

# Copy virtual environment
COPY --from=python-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code + built SPA
COPY app/ ./app/
COPY --from=frontend-builder /app/webapp/dist/ ./webapp/dist/

# Set ownership to non-root user
RUN chown -R runway:runway /app

# Switch to non-root user
USER runway

# The commit this image was built from. The build workflow passes the
# checked-out commit as a build arg, so the running server can report the
# exact build it is (Settings -> About). Public information — it only
# describes the image — and an operator can override it at run time.
#
# This ARG must be declared in the FINAL stage: a builder-stage ARG is
# consumed there and never reaches the image the server runs in. The build
# workflow rejects a Dockerfile that declares it anywhere else.
ARG BUILD_SHA=""

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    MALLOC_ARENA_MAX=4 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765 \
    RUN_MODE=docker \
    RUNWAY_BUILD_SHA=$BUILD_SHA

# Expose port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/system/health', timeout=5).close()"]

# Run application
CMD ["python", "-m", "app.main"]
