# Stage 1: Build the SPA
# The digest pins the multi-arch image index, not a per-architecture manifest,
# so one pin covers every architecture the build runs on. The base is Debian 13
# (trixie), the release the server image moved to in #805: Bookworm's util-linux
# has no fixed point release for the advisories that affect it. Node 24 ships
# npm 11.19, which webapp/package.json's devEngines requires; Node 22 ships npm
# 10, and npm 10 strips the libc metadata the lockfile needs.
FROM node:24-trixie-slim@sha256:43b9ba1142b8dcf33fa8f922ca1be85714adc444e0586c7cf7726daaeabc1bea AS frontend-builder
WORKDIR /app
# The .npmrc comes along because it carries the install-script policy: without
# it this install would run npm's advisory default, and a dependency that grows
# an install script would run it instead of failing the build.
COPY webapp/package*.json webapp/.npmrc ./webapp/
RUN npm --prefix webapp ci
COPY webapp/ ./webapp/
RUN npm --prefix webapp run build

# Stage 2: Build Python dependencies
FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS python-builder
WORKDIR /app
# The builder's compiler is deliberately unpinned, and it is here as insurance
# rather than as a current need: every requirement resolves to a wheel on both
# shipped platforms today, so nothing compiles. What it does install is a
# compiler, for the dependency that ships an sdist instead of a wheel.
#
# The sqlite3 headers it used to install alongside the compiler were dead
# weight: nothing in requirements.txt compiles against libsqlite3, because the
# stdlib's _sqlite3 is already compiled into the base image's CPython and
# aiosqlite is pure Python.
#
# An exact pin would stop resolving the next time Debian removes a superseded
# point release from the archive, and the image build would fail until the pin
# was re-pinned. That is a recurring tax on a rule only hadolint's DL3008 asked
# for, and hadolint is in no gate in this repository, so nothing enforced the
# pins and nothing checked them.
#
# The builder resolves the live archive rather than a frozen snapshot, so what
# it installs is the archive's newest at that moment. That, not a snapshot
# moving, is what breaks an exact pin.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools wheel

# Stage 3: Final Image
FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
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
COPY scripts/audit_claude_usage.py ./scripts/
COPY scripts/verify_claude_cloud.py ./scripts/
COPY scripts/claude_cloud_structure.py ./scripts/
COPY scripts/claude_chat_evidence.py ./scripts/
COPY scripts/verify_claude_chat_fixture.py ./scripts/
COPY scripts/claude_cloud_list_boundary.py ./scripts/
COPY scripts/sidecar_pkg/source_catalog.py scripts/sidecar_pkg/source_catalog_data.py ./scripts/sidecar_pkg/
COPY --from=frontend-builder /app/webapp/dist/ ./webapp/dist/

# Set ownership to non-root user
RUN chown -R runway:runway /app

# Switch to non-root user
USER runway

# The commit this image was built from. The shared docker-publish workflow
# (s3ntin3l8/.github) passes the checked-out commit as the COMMIT_SHA build arg,
# so the running server can report the exact build it is (Settings -> About).
# Public information — it only describes the image — and an operator can
# override it at run time.
#
# This ARG must be declared in the FINAL stage: a builder-stage ARG is
# consumed there and never reaches the image the server runs in. The shared
# workflow fails the build when the final stage uses $COMMIT_SHA without it.
ARG COMMIT_SHA=""

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    MALLOC_ARENA_MAX=4 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765 \
    RUN_MODE=docker \
    RUNWAY_BUILD_SHA=$COMMIT_SHA

# Expose port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/system/health', timeout=5).close()"]

# Run application
CMD ["python", "-m", "app.main"]
