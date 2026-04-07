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

Runway follows a modular **Service-Collector Pattern**:

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

## 🖥️ Multi-Host Setup

Runway supports aggregating usage data from multiple computers (e.g., laptop, desktop, server) using the **sidecar pattern**:

### Architecture
- **Primary host**: Runs the Runway dashboard (Docker or bare metal)
- **Secondary hosts**: Run lightweight sidecar scripts that push local metrics to the primary

### OpenCode Multi-Host Aggregation

For OpenCode usage tracking across multiple machines:

1. **On each secondary host**, run the sidecar script:
   ```bash
   python3 scripts/sidecar.py --provider opencode \
     --api-url http://runway-primary:8765 \
     --api-key sidecar-default-secret
   ```

2. **Install as a background task** (runs every 30 minutes):
   ```bash
   python3 scripts/sidecar.py --provider opencode \
     --api-url http://runway-primary:8765 \
     --api-key sidecar-default-secret \
     --install
   ```

3. **Dashboard displays aggregated cards**:
   - `OpenCode (5h Combined)` - Aggregated 5-hour window from all hosts
   - `OpenCode (7d Combined)` - Aggregated 7-day window from all hosts  
   - `OpenCode (30d Combined)` - Aggregated 30-day window from all hosts

The aggregation automatically sums usage across all reporting hosts and shows the combined remaining budget against OpenCode Go limits ($12/5h, $30/week, $60/month).

### Docker Deployment Note

When running Runway in Docker (where local files are not accessible), disable the local OpenCode collector:

```bash
docker run -e OPENCODE_LOCAL_COLLECTOR_ENABLED=false -p 8765:8765 runway
```

Only sidecar data will be displayed in this mode.

## 📦 Setup

1.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

2.  **Configure Environment**:
    Create a `.env` file in the root directory (see `.env.example` for the list of required tokens).

3.  **Run the App**:
    ```bash
    python3 -m app.main
    ```
    Access the dashboard at `http://localhost:8765`.

---
*Built for the 2026 Developer Workflow.*
