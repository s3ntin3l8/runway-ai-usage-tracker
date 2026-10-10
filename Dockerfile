# Stage 1: Build the SPA
FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend-builder
WORKDIR /app
COPY webapp/package*.json ./webapp/
RUN npm --prefix webapp ci
COPY webapp/ ./webapp/
RUN npm --prefix webapp run build

# Stage 2: Build Python dependencies
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS python-builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
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

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8765 \
    RUN_MODE=docker

# Expose port
EXPOSE 8765

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/system/health', timeout=5).close()"]

# Run application
CMD ["python", "-m", "app.main"]
