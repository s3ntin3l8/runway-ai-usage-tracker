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
| **Antigravity** | ✅ Full | ⚠️ Sidecar | ⚠️ Sidecar | Sidecar reads local JSON |

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
  -e LOCAL_COLLECTOR_ENABLED=false \
  -e LOCAL_CREDENTIAL_SCRAPING_ENABLED=false \
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
    environment:
      - INGEST_API_KEY=${INGEST_API_KEY:-sidecar-default-secret}
      - LOCAL_COLLECTOR_ENABLED=false
      - GITHUB_TOKEN=${GITHUB_TOKEN:-}
      - ZAI_API_KEY=${ZAI_API_KEY:-}
      - KIMI_API_KEY=${KIMI_API_KEY:-}
    restart: unless-stopped
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
| `INGEST_API_KEY` | Optional | Required | Required | Sidecar auth |
| `LOCAL_COLLECTOR_ENABLED` | true | true | **false** | Disable in Docker |
| `LOCAL_CREDENTIAL_SCRAPING_ENABLED` | true | true | **false** | Disable in Docker |

## Data Collection & Caching

Runway uses **SmartCollector** for intelligent caching:

### Cache TTL by Provider

| Provider | TTL | Reason |
|----------|-----|--------|
| **Gemini** | 5 min | Fast-changing quotas |
| **Anthropic** | 10 min | OAuth rate limit safety |
| **ChatGPT** | 10 min | Session-based windows |
| **OpenCode** | 30 min | Slow-changing usage |
| **GitHub** | 15 min | Stable quotas |
| **zAI/Kimi** | 15 min | API-based, stable |

### Features

- **TTL Caching**: Each provider has configurable cache duration
- **Error Tracking**: Monitors consecutive errors, forces retry after threshold
- **Graceful Degradation**: Returns stale cached data during API failures
- **Token Cache**: Sidecar tokens cached in memory (30-min TTL)

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

*Last updated: 2026-04-10*
