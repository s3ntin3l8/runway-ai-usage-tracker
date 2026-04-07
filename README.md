# Runway — AI Subscription Limits Dashboard

**Runway** is a local-first, stateless monitoring tool designed to track remaining capacity and reset timers across your entire generative AI stack. Instead of digging through opaque usage menus, Runway aggregates everything into a single, high-performance glassmorphism dashboard.

![Runway Dashboard](file:///Users/bjoern/.gemini/antigravity/brain/53ba3247-9958-4671-8ffc-419e940bc0eb/runway_dashboard_final_1775511401280.png)

## 🚀 Key Features

- **10 Collectors, 15+ Data Points**: Comprehensive monitoring for Claude, Gemini, GitHub Copilot, zAI, Kimi, and more.
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
3.  **Ingestion Sidecar**: A dedicated service for loading metrics from external sources. See [Sidecar Documentation](docs/sidecar.md) for setup.

👉 **[Full Deployment Guide](docs/deployment-modes.md)** with Docker Compose examples and mode compatibility matrix

## 🔌 Supported Services

Runway monitors **10 AI providers** with **15+ data points**:

| Provider | Collection Method | Cards | Docs |
|----------|------------------|-------|------|
| **Claude** | OAuth API → Web API → Local logs | 2-5 | [📖](docs/collectors/claude.md) |
| **Gemini** | OAuth API + Local logs | 1-7 | [📖](docs/collectors/gemini.md) |
| **GitHub Copilot** | REST API | 2 | [📖](docs/collectors/github.md) |
| **ChatGPT** | Web API + Local logs | 1 | [📖](docs/collectors/chatgpt.md) |
| **OpenCode** | Web API → Local DB → Sidecar | 3 | [📖](docs/collectors/opencode.md) |
| **zAI API** | REST API (Balance) | 1 | [📖](docs/collectors/zai_api.md) |
| **zAI Plan** | REST API (Quotas) | 1-2 | [📖](docs/collectors/zai_plan.md) |
| **Kimi API** | REST API (Balance) | 1 | [📖](docs/collectors/kimi_api.md) |
| **Kimi Coding** | Web API (IDE Quotas) | 2 | [📖](docs/collectors/kimi_coding.md) |
| **Antigravity** | Local JSON file | 1-3 | [📖](docs/collectors/antigravity.md) |

**Collection Methods:**
- **Direct API** → OAuth/REST endpoints (real-time)
- **Web API + Cookies** → Chrome session extraction (aggregated)
- **Local Files** → SQLite/JSON/Logs (offline)
- **Sidecar** → External push via HTTP API (remote hosts)

👉 **[Sidecar Setup Guide](docs/sidecar.md)** | **[Deployment Modes](docs/deployment-modes.md)**

## ⚙️ Quick Start by Provider

| Provider | Required Setup | Optional Fallback |
|----------|---------------|-------------------|
| **GitHub** | `GITHUB_TOKEN` env var | — |
| **zAI** | `ZAI_API_KEY` env var | — |
| **Kimi API** | `KIMI_API_KEY` env var | — |
| **Kimi Coding** | Login to [kimi.com/code](https://www.kimi.com/code) in Chrome | `KIMI_AUTH_TOKEN` env var |
| **Claude** | — | `CLAUDE_CODE_OAUTH_TOKEN` env var or macOS keychain |
| **Gemini** | — | `GEMINI_OAUTH_CLIENT_ID/SECRET` or local logs |
| **ChatGPT** | — | `CHATGPT_OAUTH_TOKEN` or local logs |
| **OpenCode** | — | Chrome cookie or local DB |
| **Antigravity** | Antigravity IDE running | — |

See [`.env.example`](.env.example) for all configuration options.

## 📥 Ingestion API

For services that cannot be reached directly by the dashboard, Runway provides a lightweight ingestion endpoint:

**Endpoint**: `POST /api/ingest`
**Content-Type**: `application/json`
**Security**: HMAC-SHA256 signature required

```bash
# Example using manual curl with HMAC signature
curl -X POST http://localhost:8765/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Signature: <hmac-sha256-hex>" \
  -H "X-Timestamp: <unix-timestamp>" \
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

## 🏥 Health API

Monitor the status of your collectors and token cache.

**Endpoint**: `GET /api/health`

```bash
curl http://localhost:8765/api/health
```

### LimitCard Schema

Each metric card follows this schema:

| Field | Type | Description |
|-------|------|-------------|
| `service` | string | Provider name (e.g., "Claude Pro", "GitHub Copilot") |
| `icon` | string | Unicode emoji for visual identification |
| `remaining` | string | Remaining quota (number, percentage, or currency) |
| `unit` | string | Unit description (e.g., "tokens", "requests", "$12 limit") |
| `reset` | string | Human-readable reset time (e.g., "in 4h 23m") |
| `health` | string | Status: `good`, `warning`, `critical`, or `unknown` |
| `pace` | string | Consumption rate: "Stable", "Moderate Burn", "Fast Burn" |
| `detail` | string | Additional context, data source, or error reason |

**Extended Fields** (for programmatic use):

| Field | Type | Description |
|-------|------|-------------|
| `used_value` | float | Raw used amount for calculations |
| `limit_value` | float | Raw limit amount for calculations |
| `is_unlimited` | bool | Whether this is an unlimited quota |
| `unit_type` | string | `currency`, `tokens`, `requests`, `minutes`, `percent`, `generic` |
| `currency` | string | Currency code: `USD`, `EUR`, `CNY`, etc. |
| `reset_at` | string | ISO 8601 timestamp for absolute reset time |
| `data_source` | string | Source: `oauth`, `web_api`, `local`, `cache`, `api`, `sidecar` |

## 🚀 Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys (see Quick Start by Provider above)

# Run
python3 -m app.main
```

Access at `http://localhost:8765`

👉 **[Docker Deployment](docs/deployment-modes.md#docker)** | **[Multi-Host Setup](docs/deployment-modes.md#multi-host)**

## 🌐 Network Access

By default, Runway only accepts connections from the same machine (`127.0.0.1`).

### Access from Other Devices

To access the dashboard from phones, tablets, or other computers on your network:

1. **Edit `.env`:**
   ```bash
   APP_HOST=0.0.0.0
   ```

2. **Restart Runway:**
   ```bash
   python3 -m app.main
   ```

3. **Find your IP and access:**
   ```bash
   # Mac/Linux
   ifconfig | grep "inet " | head -1
   
   # Windows
   ipconfig | findstr "IPv4"
   ```
   Then open: `http://<your-ip>:8765`

⚠️ **Security Warning:** 
- `0.0.0.0` exposes Runway to your entire local network
- Anyone on your WiFi can view your AI usage dashboard
- The ingestion API (`/api/ingest`) becomes network-accessible — ensure `INGEST_API_KEY` is strong
- **Recommendations:**
  - Only enable `0.0.0.0` when actively needed
  - Use firewall rules to restrict access if desired
  - For production/multi-user setups: use a reverse proxy (nginx/traefik) with HTTPS and authentication

## 📦 Setup

### Prerequisites

- Python 3.9+
- API keys for providers you want to monitor (see [Quick Start by Provider](#%EF%B8%8F-quick-start-by-provider))

### Installation

```bash
# Clone or download the repository
git clone <repository-url>
cd runway

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your API keys

# Run
python3 -m app.main
```

Access the dashboard at `http://localhost:8765`.

### Docker (Headless/Server)

```bash
docker run -e OPENCODE_LOCAL_COLLECTOR_ENABLED=false \
  -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  runway
```

**Note**: Run [sidecar scripts](docs/sidecar.md) on workstations to send file-based metrics.

👉 **[Docker Compose Setup](docs/deployment-modes.md#docker-compose)** | **[Full Deployment Guide](docs/deployment-modes.md)**

---
*Built for the 2026 Developer Workflow.*
