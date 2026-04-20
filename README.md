<p align="center">
  <img src="assets/logo.svg" width="128" alt="Runway Logo">
</p>

# Runway — AI Subscription Limits Dashboard

**Runway** is a local-first monitoring tool that tracks remaining capacity and reset timers across your entire generative AI stack — aggregated into a single, high-performance glassmorphism dashboard with persistent history and fleet management.

![Runway Dashboard](assets/dashboard.png)

## Key Features

- **14 Collectors, 20+ Data Points**: Monitor Claude, Gemini, GitHub Copilot, OpenRouter, MiniMax, Ollama, and more
- **3-Tier Fallback**: APIs → Web scraping → Local files. If one fails, the next takes over
- **Smart Caching**: Per-collector TTL (5-30 min) reduces API calls while keeping data fresh
- **Instant Serving**: `/limits` returns from an in-memory registry — never blocks on collection
- **Persistent History**: SQLite-backed usage snapshots with 15-minute background polling
- **Provider Sections**: Dashboard cards grouped by provider with context filter pills (Source / Account / Window)
- **Fleet Management**: Persistent registry of all sidecars with custom names, tags, and activity tracking
- **Token Health**: Settings panel shows OAuth/cookie expiry status with one-click refresh for supported providers
- **Sidecar Ingestion**: Push metrics from external hosts via `POST /api/v1/fleet/ingest`
- **Resilient Rendering**: Individual API failures show "Error Cards" instead of breaking the dashboard
- **Docker Ready**: Headless-first architecture for containerized environments

## Quick Start

- **Python 3.12+** and **Node.js 18+** (for UI styling) are required.

```bash
# 1. Clone and setup (installs venv, dependencies, and git hooks)
git clone <repository-url> && cd runway
make install

# 2. Configure (add your API keys)
cp .env.example .env

# 3. Run (port 8765)
make dev
```

Access at `http://localhost:8765`

> [!TIP]
> Facing issues with cookie collection or setup? Check the [Troubleshooting Guide](docs/troubleshooting.md).

## Development Shortcuts

Runway includes a `Makefile` to automate common tasks. Run `make help` for the full list.

| Command | Description |
|---------|-------------|
| `make install` | Setup venv, install Python/Node dependencies, and wire up git hooks |
| `make dev` | Start the development server with hot-reload (port 8765) |
| `make run` | Start the production server |
| `make sidecar` | Run the standalone sidecar agent script |
| `make test` | Run the test suite (standard pytest; automatically ignores macOS-only cookie tests on Linux/WSL) |
| `make lint` | Run code quality checks (ruff + mypy + pip-audit) |
| `make format` | Automatically fix linting and formatting issues |
| `make clean` | Remove virtual environments and build artifacts |

### Docker (Headless/Server)

```bash
docker run -p 8765:8765 -e INGEST_API_KEY=secret ghcr.io/s3ntin3l8/ai-usage-tracker:latest
```

> [!IMPORTANT]
> **Docker & Headless Rule**: Containerized environments have no access to native desktop keychains. 
> 1. Collectors requiring browser cookies (Claude, ChatGPT, Ollama, etc.) **must** be configured via Environment Variables or provided via a [sidecar](docs/sidecar.md).
> 2. Use `DB_ENCRYPTION_KEY` to protect sensitive metadata in your persistent volume.

Run [sidecar scripts](docs/sidecar.md) on workstations to send file-based metrics.

### 🔑 Manual Authentication
If you are running in Docker or a headless environment where browser scraping is impossible, you can manually provide authentication tokens in the **Settings** tab.

- **API Key (Bearer Token)**: Paste the full token (usually starts with `eyJ...`). This takes higher priority than cookies.
- **Session Cookie**: Provide as a fallback if explicit tokens are not available.

*Note: For ChatGPT and Claude, we recommend using the Bearer token in the "API Key" field for the most reliable connection.*

### Sidecar desktop app (macOS / Windows)

Pre-built installers are attached to every [GitHub release](https://github.com/s3ntin3l8/ai-usage-tracker/releases): `Runway-Sidecar-macOS-<version>.zip` and `Runway-Sidecar-Windows-<version>.zip`.

The binaries are not signed with an Apple Developer ID / Windows code-signing certificate, so the OS's built-in malware gatekeeper will block the first launch.

**macOS** (Gatekeeper: *"Apple could not verify this app is free of malware"*):

```bash
# After unzipping, strip the quarantine attribute:
xattr -cr ~/Downloads/Runway\ Sidecar.app
open ~/Downloads/Runway\ Sidecar.app
```

Alternatively: right-click the app in Finder → **Open** → **Open** again in the dialog. Only required on the first launch.

**Windows** (SmartScreen: *"Windows protected your PC"*): click **More info** → **Run anyway**.

## Supported Providers

| Provider | Collection Method | Cards | Env Var | Docs |
|----------|------------------|-------|---------|------|
| **Claude** | OAuth → Web API → Local logs | 2-5 | `CLAUDE_CODE_OAUTH_TOKEN` (opt) | [📖](docs/collectors/claude.md) |
| **Gemini** | OAuth API + Local logs | 1-7 | `GEMINI_OAUTH_*` (opt) | [📖](docs/collectors/gemini.md) |
| **GitHub Copilot** | REST API | 2 | `GITHUB_TOKEN` | [📖](docs/collectors/github.md) |
| **ChatGPT** | OAuth API → Chrome cookie → Local logs | 1 | `CHATGPT_OAUTH_TOKEN` (opt) | [📖](docs/collectors/chatgpt.md) |
| **OpenRouter** | REST API (Credits) | 1 | `OPENROUTER_API_KEY` | [📖](docs/collectors/openrouter.md) |
| **MiniMax** | REST API (IDE Quotas) | 1-3 | `MINIMAX_API_KEY` | [📖](docs/collectors/minimax.md) |
| **Ollama** | Web API (Cloud) + Session cookie | 2 | `OLLAMA_SESSION_TOKEN` (opt) | [📖](docs/collectors/ollama.md) |
| **OpenCode** | Web API → Local DB → Sidecar | 3 | — (Chrome cookie) | [📖](docs/collectors/opencode.md) |
| **zAI API** | REST API (Balance) | 1 | `ZAI_API_KEY` | [📖](docs/collectors/zai_api.md) |
| **zAI Plan** | REST API (Quotas) | 1-2 | `ZAI_API_KEY` | [📖](docs/collectors/zai_plan.md) |
| **Kimi API** | REST API (Balance) | 1 | `KIMI_API_KEY` | [📖](docs/collectors/kimi_api.md) |
| **Kimi Coding** | Web API (IDE Quotas) | 2 | `KIMI_AUTH_TOKEN` (opt) | [📖](docs/collectors/kimi_coding.md) |
| **Kimi K2** | REST API (Credits) | 1 | `KIMI_K2_API_KEY` | [📖](docs/collectors/kimi_k2.md) |
| **Antigravity** | Local JSON file | 1-3 | — (IDE running) | [📖](docs/collectors/antigravity.md) |

**Env Var Legend:** (opt) = Optional, has fallback | — = Detected automatically

## Architecture

Runway follows a **Service-Collector Pattern** with three deployment modes: **Standalone** (single machine), **Multi-Host** (main + sidecars), and **Docker** (sidecars only).

👉 **[Full Deployment Guide](docs/deployment-modes.md)** with mode compatibility matrix and Docker Compose examples

## API Reference

All API routes are under `/api/v1/`.

### Usage

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/usage/limits` | All current quota cards (instant, from in-memory registry) |
| `GET` | `/api/v1/usage/history` | Usage history snapshots (params: `provider_id`, `account_id`, `days`, `limit`) |
| `POST` | `/api/v1/usage/reset/{provider}` | Clear terminal failure state for a provider |

### Fleet / Ingestion

| Method | Route | Description |
|--------|-------|-------------|
| `POST` | `/api/v1/fleet/ingest` | Push metrics from a sidecar (HMAC-SHA256 signed) |
| `GET` | `/api/v1/fleet/sidecars` | List all registered sidecars |
| `PATCH` | `/api/v1/fleet/sidecars/{id}` | Update sidecar custom name or tags |
| `DELETE` | `/api/v1/fleet/sidecars/{id}` | Remove sidecar from registry |

### System

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/system/health` | Liveness check |
| `GET` | `/api/v1/system/status` | Collector cache states and error counts |
| `GET` | `/api/v1/system/settings` | Non-sensitive runtime configuration |
| `GET` | `/api/v1/system/token-health` | OAuth/cookie expiry status for all credentials |
| `POST` | `/api/v1/system/token-health/refresh/{provider}/{account_id}` | Trigger OAuth token refresh |

**Desktop App (macOS & Windows):** Download the pre-built sidecar binary from the [releases page](https://github.com/bjoernf73/runway/releases/latest).

See [Sidecar Documentation](docs/sidecar.md) for ingest authentication and payload format.

### LimitCard Schema

```typescript
interface LimitCard {
  // Core display fields
  service_name: string;     // Provider name (e.g., "Claude Pro")
  icon: string;             // Unicode emoji
  remaining: string;        // Remaining quota (e.g., "85%", "$12.50")
  unit: string;             // Unit description (e.g., "tokens", "/ 100")
  reset: string;            // Human-readable reset (e.g., "in 4h 23m")
  health: string;           // "good" | "warning" | "critical"
  pace: string;             // "Stable" | "Moderate Burn" | "Fast Burn"
  detail: string;           // Additional context

  // Identity & routing
  provider_id?: string;     // Platform key (e.g., "anthropic", "gemini")
  account_id?: string;      // Unique account hash/ID
  account_label?: string;   // Human-readable identity (email, org)
  sidecar_id?: string;      // Originating host; null = local collection
  model_id?: string;        // Specific model; null = aggregate snapshot

  // Usage data
  used_value?: number;      // Raw used amount
  limit_value?: number;     // Raw limit amount
  is_unlimited?: boolean;
  unit_type?: string;       // "currency" | "tokens" | "requests" | "percent"
  currency?: string;        // "USD" | "EUR" | "CNY"
  window_type?: string;     // "daily" | "weekly" | "monthly" | "session" | "rolling" | "unknown"

  // Metadata
  reset_at?: string;        // ISO 8601 timestamp for tooltip
  data_source?: string;     // "oauth" | "web_api" | "local" | "api" | "cache"
  tier?: string;            // "Free" | "Pro" | "Enterprise"
  usage_url?: string;       // Link to provider usage page
  updated_at?: string;      // ISO 8601 timestamp
}
```

## Optional Security

**`RUNWAY_CONFIG_DIR`** — Override the default platform-specific configuration directory. This controls where Runway stores its database (`runway.db`), external metrics, and OAuth tokens. This is especially useful for Docker deployments or when you need to store configuration in a non-default location.

**`ADMIN_API_KEY`** — When set, the dashboard and admin API endpoints are protected. Unset by default (local-first, single-user). Remote access triggers a Login Screen.

**`DB_ENCRYPTION_KEY`** — Fernet key for encrypting sensitive metadata in SQLite. Unset = plaintext (acceptable for local deployments). Back up this key alongside the database file.

## Network Access

By default, Runway binds to `127.0.0.1` (local only). To access from other devices on your network:

1. Set `APP_HOST=0.0.0.0` in `.env`
2. Restart Runway
3. Access via `http://<your-ip>:8765`

## Authentication & Security

Runway provides a flexible, multi-layered security model:

- **Local Trust**: When accessing Runway from `127.0.0.1` (localhost), authentication is automated. You will jump straight to the dashboard even if an `ADMIN_API_KEY` is set.
- **Login Screen**: For remote access or Docker deployments, setting `ADMIN_API_KEY` in your `.env` triggers a dedicated **Login Portal**. Enter the key once, and it is persisted in your browser's secure storage.
- **Headless Auth (Proxy)**: If you offload authentication to a reverse proxy (e.g., Authelia, Cloudflare Access, Nginx Auth), Runway will automatically trust and bypass the login screen if `X-Forwarded-User` or `Remote-User` headers are present.

⚠️ **Public Internet**: Never expose Runway directly to the public internet without a reverse proxy (Nginx/Traefik) and HTTPS.

## License

MIT License - see [LICENSE](LICENSE) file.

*Last updated: 2026-04-13*
