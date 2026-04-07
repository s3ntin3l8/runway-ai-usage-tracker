FROM python:3.12-slim-bookworm

# Create non-root user with home directory
RUN groupadd -r runway && useradd -r -g runway -u 1000 -m runway

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy and install Python dependencies first (for layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/

# Set ownership to non-root user
RUN chown -R runway:runway /app

# Switch to non-root user
USER runway

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1

# Expose port (unprivileged user can bind to 8765)
EXPOSE 8765

# Health check (app responds on /api/limits)
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/api/limits')" || exit 1

# Run application (bind to 0.0.0.0 for Docker)
CMD ["python3", "-c", "import uvicorn; from app.main import app; uvicorn.run(app, host='0.0.0.0', port=8765)"]
