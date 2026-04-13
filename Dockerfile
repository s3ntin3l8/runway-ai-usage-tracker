# Stage 1: Build CSS
FROM node:20-slim AS frontend-builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY frontend/ ./frontend/
RUN npm run build:css

# Stage 2: Build Python dependencies
FROM python:3.12-slim-bookworm AS python-builder
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc=4:12.2.0-3 \
    libsqlite3-dev=3.40.1-2+deb12u2 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 3: Final Image
FROM python:3.12-slim-bookworm
# Create non-root user with home directory
RUN groupadd -r runway && useradd -r -g runway -u 1000 -m runway

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl=7.88.1-10+deb12u14 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment
COPY --from=python-builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY app/ ./app/
COPY frontend/ ./frontend/

# Copy built CSS from frontend-builder stage
COPY --from=frontend-builder /app/frontend/css/styles.css ./frontend/css/styles.css

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
  CMD curl -f http://localhost:8765/api/health || exit 1

# Run application
CMD ["python", "-m", "app.main"]