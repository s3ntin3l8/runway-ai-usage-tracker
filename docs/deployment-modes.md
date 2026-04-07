# Deployment Modes Guide

Runway supports three deployment modes to fit different workflows and infrastructure setups.

## Quick Decision Tree

```
Running on single machine with coding tools?
├── YES → Standalone Mode
└── NO → Running in Docker or on server?
    ├── YES → Docker Mode (requires sidecar on workstations)
    └── NO → Multiple computers?
        └── YES → Multi-Host Mode
```

---

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
| **Antigravity** | ✅ Full | ⚠️ Sidecar | ⚠️ Sidecar | Sidecar reads local JSON, pushes to container |

**Legend:**
- ✅ **Full**: Works without sidecar
- ⚠️ **Sidecar**: Requires sidecar for full functionality
- ⚠️ **Sidecar/***: Recommended but not strictly required (fallbacks available)

---

## Standalone Mode

**Best for**: Individual developers using one machine

Runway runs on the same computer as your coding tools with direct access to:
- Local files (`~/.claude/`, `~/.config/`, etc.)
- Chrome cookies for authentication
- SQLite databases
- Environment variables

### Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Run
python3 -m app.main
```

Access at `http://localhost:8765`

### Network Access

By default, standalone mode only accepts local connections (`127.0.0.1`).

To allow other devices on your network to access the dashboard:

1. Set `APP_HOST=0.0.0.0` in your `.env`
2. Restart Runway
3. Access via your machine's IP address

⚠️ See [README security warning](../README.md#-network-access) before enabling.

### When to Use

- ✅ You code on a single machine
- ✅ You want zero configuration overhead
- ✅ You have direct file system access
- ✅ No Docker or servers involved

---

## Multi-Host Mode

**Best for**: Multiple computers (desktop + laptop, team workstations)

Main PC runs the full Runway dashboard, while secondary machines send data via sidecar.

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

### Setup

**Main PC** (runs full Runway app):
```bash
python3 -m app.main
```

**Secondary Machines** (sidecar only):
```bash
# Install (no dependencies needed - uses stdlib only)
python3 scripts/sidecar.py \
  --api-url http://main-pc:8765 \
  --api-key sidecar-default-secret

# Install as background task (runs every 30 min)
python3 scripts/sidecar.py --install \
  --api-url http://main-pc:8765 \
  --api-key sidecar-default-secret
```

### When to Use

- ✅ You code on multiple machines
- ✅ Main PC is always on
- ✅ Laptops need battery-efficient monitoring
- ✅ Team/shared dashboard setup

### Troubleshooting

**Issue**: Sidecar can't reach main PC
```bash
# Check connectivity
curl http://main-pc:8765/api/health

# Check firewall (Linux)
sudo ufw allow 8765/tcp
```

**Issue**: Metrics not appearing from laptop
- Verify `--api-key` matches `INGEST_API_KEY` on main PC
- Check sidecar logs: `python3 scripts/sidecar.py --provider all --dry-run`
- Ensure hostname is unique per machine

---

## Docker Mode

**Best for**: Servers, headless environments, team dashboards

Runway runs in a container with no local file access. All file-based data comes from sidecars.

```
┌─────────────────────────────────────────┐
│           Docker Network                │
│  ┌──────────────┐  ┌──────────────┐    │
│  │   Runway     │  │   Sidecar    │    │
│  │  (Container) │  │  (Optional)  │    │
│  │              │  │              │    │
│  │ • Dashboard  │  │ • Cookies    │    │
│  │ • APIs       │  │ • Files      │    │
│  │ • Aggregation│  │ • Push       │    │
│  └──────────────┘  └──────────────┘    │
│         ▲                              │
│         │ HTTP                         │
│    Workstations                        │
│  (Sidecar scripts)                     │
└─────────────────────────────────────────┘
```

### Docker Run

```bash
docker run -d \
  --name runway \
  -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  -e OPENCODE_LOCAL_COLLECTOR_ENABLED=false \
  runway
```

### Docker Compose

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  runway:
    build: .
    ports:
      - "8765:8765"
    environment:
      # Required for sidecar authentication
      - INGEST_API_KEY=${INGEST_API_KEY:-sidecar-default-secret}
      
      # Disable local collectors (no host file access in container)
      - OPENCODE_LOCAL_COLLECTOR_ENABLED=false
      
      # API keys for direct API collectors (optional - can also use sidecar)
      - GITHUB_TOKEN=${GITHUB_TOKEN:-}
      - ZAI_API_KEY=${ZAI_API_KEY:-}
      - KIMI_API_KEY=${KIMI_API_KEY:-}
      
    volumes:
      # Optional: Persist external metrics
      - ./data:/app/data
      
    restart: unless-stopped

  # Optional: Run sidecar in same compose for file-based collectors
  # Uncomment if you need file/cookie access within the same host
  # sidecar:
  #   build: .
  #   command: >
  #     python3 scripts/sidecar.py
  #     --provider all
  #     --api-url http://runway:8765
  #     --api-key ${INGEST_API_KEY:-sidecar-default-secret}
  #   environment:
  #     - KIMI_AUTH_TOKEN=${KIMI_AUTH_TOKEN:-}
  #   volumes:
  #     # Mount host directories for file access
  #     - ~/.antigravity:/root/.antigravity:ro
  #     - ~/.config/google-chrome:/root/.config/google-chrome:ro
  #   depends_on:
  #     - runway
  #   restart: unless-stopped
```

Run with:
```bash
# Create .env file with your keys
cp .env.example .env
# Edit .env

# Start services
docker-compose up -d
```

### Workstation Sidecars (Docker Mode)

Each workstation must run a sidecar to push file-based metrics:

```bash
# On each workstation
python3 scripts/sidecar.py \
  --api-url http://docker-server:8765 \
  --api-key your-secret-key
```

### When to Use

- ✅ Running on a server/VPS
- ✅ Headless environment (no GUI)
- ✅ Team dashboard on shared infrastructure
- ✅ You want isolated, reproducible deployment

### Troubleshooting

**Issue**: Container can't access APIs
```bash
# Check container network
docker exec runway ping api.github.com

# Check DNS
docker exec runway nslookup api.github.com
```

**Issue**: Sidecar can't reach container
```bash
# Use host IP or container name
--api-url http://host.docker.internal:8765  # Mac/Windows
--api-url http://<host-ip>:8765             # Linux
```

**Issue**: File-based collectors empty (Antigravity, etc.)
- File-based collectors don't work in Docker
- Must use sidecar on host machine
- See "Workstation Sidecars" above

---

## Mode-Specific Configuration

### Environment Variables by Mode

| Variable | Standalone | Multi-Host | Docker | Purpose |
|----------|:----------:|:----------:|:------:|---------|
| `GITHUB_TOKEN` | ✅ | ✅ | ✅ | GitHub API access |
| `ZAI_API_KEY` | ✅ | ✅ | ✅ | zAI API access |
| `KIMI_API_KEY` | ✅ | ✅ | ✅ | Kimi API access |
| `KIMI_AUTH_TOKEN` | Optional | Optional | Optional | Kimi IDE auth (fallback to cookie) |
| `INGEST_API_KEY` | Optional | Required on main | Required | Sidecar authentication |
| `OPENCODE_LOCAL_COLLECTOR_ENABLED` | true | true | **false** | Disable in Docker |

---

## Data Collection & Caching

Runway uses **SmartCollector** to implement intelligent caching and reduce API calls while maintaining fresh data.

### How SmartCollector Works

Each collector is wrapped with SmartCollector which implements:

- **TTL Caching**: Each provider has a configurable cache duration (5-30 minutes)
- **Error Tracking**: Monitors consecutive errors and forces retry after threshold
- **Graceful Degradation**: Returns stale cached data during API failures instead of error cards
- **Retry Delays**: Prevents API hammering during outages with 30s retry delays

### Cache TTL by Provider

| Provider | TTL | Reason |
|----------|-----|--------|
| **Gemini** | 5 min | Fast-changing quotas |
| **Anthropic** | 10 min | OAuth rate limit safety |
| **ChatGPT** | 10 min | Session-based windows |
| **OpenCode** | 30 min | Slow-changing usage |
| **GitHub** | 15 min | Stable quotas |
| **zAI/Kimi** | 15 min | API-based, stable |

### Cache Indicators

When viewing cached data, cards show `[Cached Xm ago]` in the detail field:
```python
{
    "detail": "25.0% used [OAuth] [Cached 5m ago]"
}
```

### Token Cache (Sidecar Integration)

For sidecar deployments, tokens received from sidecars are cached in memory with 30-minute TTL:

1. Sidecar extracts tokens from local files/keychain
2. Sends tokens to server via `/api/ingest`
3. Server stores in `token_cache` (memory-only, 30min TTL)
4. Collectors check token cache before attempting file/cookie extraction
5. Server makes API calls using cached tokens

This allows the main server to make OAuth API calls on behalf of sidecars without persistent storage.

---

## Advanced: Mixed Mode

You can combine modes for complex setups:

```
┌─────────────────────────────────────────────┐
│              Docker Host                    │
│  ┌──────────────┐      ┌──────────────┐    │
│  │   Runway     │◄─────│   Sidecar    │    │
│  │  (Container) │      │  (Container) │    │
│  └──────────────┘      └──────────────┘    │
│         ▲                                    │
│         │                                    │
│    Workstations                             │
│  ┌──────────┐  ┌──────────┐                │
│  │ Laptop 1 │  │ Laptop 2 │                │
│  │(Sidecar) │  │(Sidecar) │                │
│  └──────────┘  └──────────┘                │
└─────────────────────────────────────────────┘
```

This setup:
1. Runway in Docker (main aggregation)
2. Sidecar container for host file access (Antigravity, etc.)
3. Sidecars on laptops for remote metrics

---

## Security Considerations

### API Keys in Docker

**Option 1**: Pass via environment (shown above)
- Pros: Simple, standard
- Cons: Keys visible in `docker inspect`

**Option 2**: Use sidecar for all data
- Pros: No keys in container
- Cons: Requires sidecar on every workstation

**Option 3**: Docker secrets (Swarm/Kubernetes)
```yaml
# docker-compose.yml (Swarm mode)
secrets:
  github_token:
    external: true

services:
  runway:
    secrets:
      - github_token
```

### Network Security

- Use HTTPS in production (reverse proxy)
- Restrict `INGEST_API_KEY` rotation
- Sidecar only needs outbound HTTP (no inbound ports)

---

## Performance Tips

### Standalone
- Default settings optimal for single machine
- No network overhead

### Multi-Host
- Keep main PC on stable network
- Use wired connection if possible
- Sidecar caches locally (reduces API calls)

### Docker
- Set memory limits: `--memory=512m`
- Use bind mounts for logs if needed (not recommended)
- Consider reverse proxy (nginx/traefik) for HTTPS

---

## Migration Between Modes

### Standalone → Docker
1. Export current `.env` settings
2. Create `docker-compose.yml`
3. Set `OPENCODE_LOCAL_COLLECTOR_ENABLED=false`
4. Deploy sidecars on all workstations
5. Verify metrics appear in dashboard

### Docker → Standalone
1. Stop Docker container
2. Install locally: `pip install -r requirements.txt`
3. Copy `.env` from container or recreate
4. Set `OPENCODE_LOCAL_COLLECTOR_ENABLED=true`
5. Run: `python3 -m app.main`

---

## Questions?

- **[Collector Docs](../docs/collectors/)** - Individual provider setup
- **[Sidecar Guide](sidecar.md)** - Detailed sidecar configuration
- **[Security Guide](../docs/SECURITY.md)** - Credential management
