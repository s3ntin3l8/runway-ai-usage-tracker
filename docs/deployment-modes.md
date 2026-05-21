# Deployment Modes Guide

Runway supports three deployment modes to fit different workflows.

## Quick Decision Tree

```
Running on single machine with coding tools?
├── YES → Standalone Mode
└── NO → Running in Docker or on server?
    ├── YES → Docker Mode (requires sidecar)
    └── NO → Multi-Host Mode
```

## Compatibility Matrix

| Collector | Standalone | Multi-Host | Docker | Notes |
|-----------|:----------:|:----------:|:------:|-------|
| **Claude** | ✅ Full | ⚠️ Sidecar* | ⚠️ Sidecar* | *Sidecar can extract keychain on Mac |
| **Gemini** | ✅ Full | ✅ Full | ⚠️ OAuth refresh | OAuth may need refresh in Docker |
| **GitHub** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **ChatGPT** | ✅ Full | ⚠️ Sidecar* | ⚠️ Sidecar* | *Cookie extraction needs host access |
| **OpenCode** | ✅ Full | ✅ Full | ⚠️ Sidecar | Web API preferred, local DB fallback |
| **zAI API** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **zAI Plan** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **Kimi API** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **Kimi Coding** | ✅ Full | ⚠️ Sidecar | ⚠️ Sidecar | Sidecar extracts cookie or uses env var |
| **OpenRouter** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **MiniMax** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **Ollama** | ✅ Full | ⚠️ Sidecar | ⚠️ Sidecar | Cookie extraction needs host access |
| **Kimi K2** | ✅ Full | ✅ Full | ✅ Full | API key works everywhere |
| **Antigravity** | ⚠️ Sidecar | ⚠️ Sidecar | ⚠️ Sidecar | Sidecar-only — server has no antigravity collector |

**Legend:**
- ✅ **Full**: Works without sidecar
- ⚠️ **Sidecar**: Requires sidecar for full functionality
- ⚠️ **Sidecar/***: Recommended but not strictly required

## Standalone Mode

**Best for:** Individual developers on one machine

Runway has direct access to:
- Local files (`~/.claude/`, `~/.config/`, etc.)
- Chrome cookies
- SQLite databases
- Environment variables

**Setup:**
```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with API keys
python3 -m app.main
```

Access at `http://localhost:8765`

## Multi-Host Mode

**Best for:** Multiple computers (desktop + laptop)

Main PC runs Runway dashboard; secondary machines send data via sidecar.

```
┌──────────────┐         ┌──────────────┐
│   Main PC    │◄────────│   Laptop     │
│  (Runway)    │  HTTP   │  (Sidecar)   │
│              │         │              │
│ • Dashboard  │         │ • Cookies    │
│ • APIs       │         │ • Files      │
│ • Aggregation│         │ • Push only  │
└──────────────┘         └──────────────┘
```

**Setup:**
```bash
# Main PC
python3 -m app.main

# Laptop
python3 scripts/sidecar.py \
  --api-url http://main-pc:8765 \
  --api-key sidecar-default-secret
```

See [Sidecar Guide](sidecar.md) for details.

## Docker Mode

**Best for:** Servers, headless environments, team dashboards

Runway runs in container with no local file access. All file-based data comes from sidecars.

```
┌─────────────────────────────────────────┐
│           Docker Network                │
│  ┌──────────────┐                      │
│  │   Runway     │◄──── Workstations   │
│  │  (Container) │      (Sidecars)     │
│  │              │                      │
│  │ • Dashboard  │                      │
│  │ • APIs       │                      │
│  │ • Aggregation│                      │
│  └──────────────┘                      │
└─────────────────────────────────────────┘
```

**Docker Run:**
```bash
docker run -d \
  --name runway \
  -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  runway
```

**Docker Compose:**
```yaml
version: '3.8'

services:
  runway:
    build: .
    ports:
      - "8765:8765"
    env_file:
      - .env
    volumes:
      - ./data:/home/runway/.config/runway
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8765/api/v1/system/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

**Workstation Sidecars:**
```bash
python3 scripts/sidecar.py \
  --api-url http://docker-server:8765 \
  --api-key your-secret-key
```

## Mode Configuration

| Variable | Standalone | Multi-Host | Docker | Purpose |
|----------|:----------:|:----------:|:------:|---------|
| `GITHUB_TOKEN` | ✅ | ✅ | ✅ | GitHub API |
| `ZAI_API_KEY` | ✅ | ✅ | ✅ | zAI API |
| `KIMI_API_KEY` | ✅ | ✅ | ✅ | Kimi API |
| `OPENROUTER_API_KEY` | ✅ | ✅ | ✅ | OpenRouter API |
| `MINIMAX_API_KEY` | ✅ | ✅ | ✅ | MiniMax API |
| `INGEST_API_KEY` | Optional | Required | Required | Sidecar auth |

## Data Collection & Caching

Runway uses **SmartCollector** for intelligent caching.

### Cache TTL

The built-in default for every registered collector is **15 minutes (900 seconds)**, set in `app/services/collector_manager.py`. There is no longer a per-provider TTL table baked into the code — the schedule is uniform.

You can override the polling cadence at two layers:

- **Per-provider** — set `poll_interval_seconds` on a `provider_configs` row (Settings UI → Providers → poll interval). This wins for that `(provider_id, account_id)` pair.
- **Globally** — set `default_poll_interval_seconds` in `system_config` (Settings UI → App Config). Used whenever a provider row leaves the field null.

Resolution order is per-provider override → global default → built-in 900s. The accompanying smart-polling sleep mode stretches the effective interval to ~2 hours after 45 minutes without a quota change, so an idle dashboard isn't hammering provider APIs.

### Features

- **TTL Caching**: Each provider has configurable cache duration (see above)
- **Error Tracking**: Monitors consecutive errors, forces retry after threshold
- **Graceful Degradation**: Returns stale cached data during API failures
- **Token Cache**: Sidecar-forwarded tokens are kept in-memory only (30-min TTL, never persisted to disk on the server)
- **Smart sleep**: Idle dashboards downshift the poller to a 2-hour cadence; any usage event or `/api/v1/system/wake` resets it

## Security

### API Keys in Docker

**Option 1:** Environment variables (simple, standard)
**Option 2:** Sidecar-only (no keys in container, requires sidecar on all hosts)
**Option 3:** Docker secrets (Swarm/Kubernetes)

### Network Security

- Use HTTPS in production (reverse proxy)
- Restrict `INGEST_API_KEY` rotation
- Sidecar only needs outbound HTTP (no inbound ports)

---

See [Sidecar Guide](sidecar.md) for detailed sidecar configuration.

*Last updated: 2026-05-21*
