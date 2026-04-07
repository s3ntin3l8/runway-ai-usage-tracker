# Runway — AI Subscription Limits Dashboard

**Runway** is a local-first, stateless monitoring tool designed to track remaining capacity and reset timers across your entire generative AI stack. Instead of digging through opaque usage menus, Runway aggregates everything into a single, high-performance glassmorphism dashboard.

![Runway Dashboard](file:///Users/bjoern/.gemini/antigravity/brain/53ba3247-9958-4671-8ffc-419e940bc0eb/runway_dashboard_final_1775511401280.png)

## 🚀 Key Features

- **13+ Data Points Integrated**: Dedicated collectors for Claude, Gemini, GPT-4, GitHub Copilot, and more.
- **Sidecar Ingestion API**: Push metrics from external scripts or host-side services into the dashboard via `POST /api/ingest`.
- **Resilient Rendering**: Individual API failures or malformed responses won't break the dashboard; failing services gracefully show "Error Cards."
- **Real-Time Sync**: Pings live APIs and parses local log/state files simultaneously.
- **Stateless & Secure**: No database required. Keeps your API keys safe in a local `.env` file.
- **Docker Ready**: Headless-first architecture designed to run in containerized environments without native OS dependencies.
- **Humanized Timers**: High-precision countdown clocks for quota resets and prepaid balance expiry.

## 🏗️ Architecture

Runway follows a modular **Service-Collector Pattern** with three deployment modes:

### Deployment Modes

**Standalone** (Single Machine): Runway and coding tools on the same computer.
- Direct local file access (`~/.claude/`, `opencode.db`)
- Chrome cookie extraction
- No sidecar needed

**Multi-Host** (Multiple Computers): Main PC runs dashboard, laptop sends data.
- Main PC aggregates from all sources
- Sidecar runs on secondary machines
- Combines local + sidecar data

**Server/Docker** (Containerized): Runway in Docker, workstations send data.
- No local file access
- Sidecar required on ALL machines
- Web APIs use tokens from sidecars

### Core Principles
1. **Server Does Heavy Lifting**: API calls, aggregation, dashboard logic run ONLY on main app
2. **Sidecar is Thin**: Only extracts and forwards raw data (cookies, tokens, DB files)
3. **No Duplication**: If main app can access directly (standalone), don't use sidecar
4. **Docker = Sidecar Only**: In containers, ALL data comes from sidecars + web APIs

### Components

1.  **Direct API Collectors**: Use `httpx` to fetch live usage data from official provider endpoints (e.g., Claude OAuth, GitHub API).
2.  **Local File Parsers**: Extract usage metrics from local CLI logs, SQLite databases (OpenCode), or JSON/JSONL state files.
3.  **Ingestion Sidecar**: A dedicated service for loading metrics from external sources (e.g., shell scripts monitoring terminal activity).

## 🔌 Supported Services

Currently monitoring **13 data sources** across the following providers:

*   **Claude (Anthropic)**: Primary OAuth monitoring (supports 5h and 7d windows) + Fallback log parsing for `~/.claude`.
*   **Gemini (Google)**: Multi-model telemetry from terminal-based usage logs.
*   **GitHub Copilot**: Live rate limit tracking for Copilot Chat and Indent.
*   **OpenCode**: Local line-change metrics from `opencode.db` and live cloud usage via API. Supports multi-host aggregation via sidecar.
*   **Chinese AI Ecosystem**: Prepaid balance tracking for **zAI (GLM)** and **Kimi K2.5**.
*   **ChatGPT Codex**: Session log parsing for local Codex activity.
*   **Antigravity IDE**: Multi-model telemetry (`gemini-3.1-pro`, `claude-3-5-sonnet`, `o3-mini`).
*   **Custom Sidecars**: Any service can push metrics via the Ingestion API.

## 📥 Ingestion API

For services that cannot be reached directly by the dashboard, Runway provides a lightweight ingestion endpoint:

**Endpoint**: `POST /api/ingest`
**Content-Type**: `application/json`

```bash
curl -X POST http://localhost:8765/api/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "my-custom-service",
    "metrics": [
      {
        "service": "Usage Limit",
        "icon": "⚡",
        "remaining": "85%",
        "unit": "capacity",
        "health": "good",
        "detail": "850/1000 tokens remaining"
      }
    ]
  }'
```

## 🖥️ Deployment Examples

### Standalone (Single Machine)

Runway runs on the same computer as your coding tools.

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Run
python3 -m app.main
```

Access at `http://localhost:8765`. No sidecar needed.

### Multi-Host (Main PC + Laptop)

**Main PC** (runs full Runway app):
```bash
python3 -m app.main
```

**Laptop** (sidecar only):
```bash
python3 scripts/sidecar.py \
  --api-url http://main-pc:8765 \
  --api-key sidecar-default-secret
```

The main PC combines its own local data with data from the laptop's sidecar.

### Server/Docker (Containerized)

**Server** (Docker - no local file access):
```bash
docker run -e OPENCODE_LOCAL_COLLECTOR_ENABLED=false \
  -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  runway
```

**Each Workstation** (sidecar required):
```bash
python3 scripts/sidecar.py \
  --api-url http://server:8765 \
  --api-key your-secret-key
```

The server aggregates data from ALL workstation sidecars. Heavy lifting (API calls, aggregation) happens server-side.

## 📦 Setup

### Quick Start (Standalone)

For running Runway on the same machine as your coding tools:

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Environment**:
    ```bash
    cp .env.example .env
    # Edit .env with your API keys
    ```

3.  **Run the App**:
    ```bash
    python3 -m app.main
    ```

Access the dashboard at `http://localhost:8765`.

### Docker Deployment

For running Runway on a server or container:

```bash
docker run -e OPENCODE_LOCAL_COLLECTOR_ENABLED=false \
  -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  runway
```

**Note**: You MUST run sidecar scripts on each workstation to send data to the container.

---
*Built for the 2026 Developer Workflow.*
