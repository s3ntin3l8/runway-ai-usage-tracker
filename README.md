<p align="center">
  <img src="assets/logo.svg" width="128" alt="Runway Logo">
</p>

# Runway — AI Subscription Limits Dashboard

**Runway** is a local-first, stateless monitoring tool that tracks remaining capacity and reset timers across your entire generative AI stack. Everything aggregated into a single, high-performance glassmorphism dashboard.

![Runway Dashboard](assets/dashboard.png)

## Key Features

- **10 Collectors, 15+ Data Points**: Monitor Claude, Gemini, GitHub Copilot, zAI, Kimi, and more
- **3-Tier Fallback**: APIs → Web scraping → Local files. If one fails, the next takes over
- **Smart Caching**: Per-collector TTL (5-30 min) reduces API calls while keeping data fresh
- **Sidecar Ingestion**: Push metrics from external hosts via `POST /api/ingest`
- **Resilient Rendering**: Individual API failures show "Error Cards" instead of breaking the dashboard
- **Docker Ready**: Headless-first architecture for containerized environments

## Quick Start

```bash
# 1. Clone and setup
git clone <repository-url> && cd runway
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure (add your API keys)
cp .env.example .env

# 3. Run
python3 -m app.main
```

Access at `http://localhost:8765`

### Docker (Headless/Server)

```bash
docker run -p 8765:8765 -e INGEST_API_KEY=secret ghcr.io/user/runway:latest
```

Run [sidecar scripts](docs/sidecar.md) on workstations to send file-based metrics.

## Supported Providers

| Provider | Collection Method | Cards | Env Var | Docs |
|----------|------------------|-------|---------|------|
| **Claude** | OAuth → Web API → Local logs | 2-5 | `CLAUDE_CODE_OAUTH_TOKEN` (opt) | [📖](docs/collectors/claude.md) |
| **Gemini** | OAuth API + Local logs | 1-7 | `GEMINI_OAUTH_*` (opt) | [📖](docs/collectors/gemini.md) |
| **GitHub Copilot** | REST API | 2 | `GITHUB_TOKEN` | [📖](docs/collectors/github.md) |
| **ChatGPT** | OAuth API → Chrome cookie → Local logs | 1 | `CHATGPT_OAUTH_TOKEN` (opt) | [📖](docs/collectors/chatgpt.md) |
| **OpenCode** | Web API → Local DB → Sidecar | 3 | — (Chrome cookie) | [📖](docs/collectors/opencode.md) |
| **zAI API** | REST API (Balance) | 1 | `ZAI_API_KEY` | [📖](docs/collectors/zai_api.md) |
| **zAI Plan** | REST API (Quotas) | 1-2 | `ZAI_API_KEY` | [📖](docs/collectors/zai_plan.md) |
| **Kimi API** | REST API (Balance) | 1 | `KIMI_API_KEY` | [📖](docs/collectors/kimi_api.md) |
| **Kimi Coding** | Web API (IDE Quotas) | 2 | `KIMI_AUTH_TOKEN` (opt) | [📖](docs/collectors/kimi_coding.md) |
| **Antigravity** | Local JSON file | 1-3 | — (IDE running) | [📖](docs/collectors/antigravity.md) |

**Env Var Legend:** (opt) = Optional, has fallback | — = Detected automatically

## Architecture

Runway follows a **Service-Collector Pattern** with three deployment modes: **Standalone** (single machine), **Multi-Host** (main + sidecars), and **Docker** (sidecars only). 

👉 **[Full Deployment Guide](docs/deployment-modes.md)** with mode compatibility matrix and Docker Compose examples

## API Reference

### Ingestion API
Push metrics from external scripts or remote hosts.

**`POST /api/ingest`** - Submit metrics with HMAC-SHA256 signature (max 1 MB body)

See [Sidecar Documentation](docs/sidecar.md) for authentication and payload format.

### Health API
Monitor collector status and cache states.

**`GET /api/health`** - System health and collector statistics

### LimitCard Schema

```typescript
interface LimitCard {
  // Core display fields
  service: string;        // Provider name (e.g., "Claude Pro")
  icon: string;           // Unicode emoji
  remaining: string;      // Remaining quota (e.g., "85%", "$12.50")
  unit: string;           // Unit description (e.g., "tokens", "/ 100")
  reset: string;          // Human-readable reset (e.g., "in 4h 23m")
  health: string;         // "good" | "warning" | "critical"
  pace: string;           // "Stable" | "Moderate Burn" | "Fast Burn"
  detail: string;         // Additional context

  // Extended fields
  used_value?: number;    // Raw used amount
  limit_value?: number;   // Raw limit amount
  is_unlimited?: boolean;
  unit_type?: string;     // "currency" | "tokens" | "requests" | "percent"
  currency?: string;      // "USD" | "EUR" | "CNY"
  reset_at?: string;      // ISO 8601 timestamp for tooltip
  data_source?: string;   // "oauth" | "web_api" | "local" | "api"
  tier?: string;          // "Free" | "Pro" | "Enterprise"
  usage_url?: string;     // Link to provider usage page
  updated_at?: string;    // ISO 8601 timestamp
}
```

## Network Access

By default, Runway binds to `127.0.0.1` (local only). To access from other devices on your network:

1. Set `APP_HOST=0.0.0.0` in `.env`
2. Restart Runway
3. Access via `http://<your-ip>:8765`

⚠️ **Security**: `0.0.0.0` exposes the dashboard to your entire local network. Ensure `INGEST_API_KEY` is strong. For production, use a reverse proxy with HTTPS.

## License

MIT License - see [LICENSE](LICENSE) file.

*Last updated: 2026-04-10*
