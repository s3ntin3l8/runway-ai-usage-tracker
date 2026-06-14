# Deployment Guide

Runway is always **server + sidecar(s)**. Two axes vary independently:

- **Server runtime** — Python (`make dev-all` / `make run`) **or** Docker (`docker run` / Compose).
- **Sidecar count** — one sidecar on the same host (single-developer setup) **or** N sidecars across remote workstations (fleet setup).

```
                     ┌────────────────────────┐
                     │        Server          │
                     │  Python  ──or──  Docker│
   ┌──── Sidecar ───▶│                        │
   │                 │  • Dashboard           │
   │   /api/v1/      │  • API + web collectors│
   │   fleet/ingest  │  • Aggregation + DB    │
   └──── Sidecar ───▶│                        │
       (1..N hosts)  └────────────────────────┘
```

The server never performs local detection itself — all LSP probes, browser cookies, and IDE/file introspection live in the sidecar. The sidecar batches up to 1000 events per push to `POST /api/v1/fleet/ingest` (HMAC-signed, 600/min/IP).

## Server runtime

### Python

For local development and single-machine setups:

```bash
make install                # venv, deps, git hooks
cp .env.example .env        # add API keys
make dev-all                # server + Vite frontend (:5173) + local sidecar, hot reload, Ctrl-C stops all
```

For production-style runs (no hot reload — server serves the built SPA at :8765):

```bash
make run-all                # build SPA, then server + sidecar in one command
# or run the pieces separately:
make run                    # server only (serves webapp/dist)
make sidecar                # in a second shell, or run via a process manager
```

Defaults: server binds `127.0.0.1:8765`. Set `APP_HOST=0.0.0.0` in `.env` for LAN access.

### Docker

For servers, headless environments, or team dashboards:

```bash
docker run -d \
  --name runway \
  -p 8765:8765 \
  -e INGEST_API_KEY=your-secret-key \
  ghcr.io/s3ntin3l8/runway:latest
```

Compose example:

```yaml
services:
  runway:
    image: ghcr.io/s3ntin3l8/runway:latest
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

> [!IMPORTANT]
> **Docker runs the server, not the sidecar.** Containers have no access to native desktop keychains or browser cookies. Every cookie-/local-file-backed collector (Claude, ChatGPT, Ollama, Kimi Coding, OpenCode, Antigravity, …) needs a sidecar running on the host where those credentials live.

When `APP_HOST != 127.0.0.1`, the server refuses to start without `DB_ENCRYPTION_KEY`, `TLS_TERMINATED=1`, and an explicit `CORS_ORIGINS` allow-list. See [SECURITY.md](SECURITY.md).

### Docker behind Traefik

For a public-facing dashboard, front Runway with a reverse proxy that terminates TLS. This crosses the multi-host gate above, so `DB_ENCRYPTION_KEY`, `TLS_TERMINATED=true`, and `CORS_ORIGINS` become mandatory — the container fails fast on startup without them.

[`docker-compose.traefik.yml`](../docker-compose.traefik.yml) is a self-contained Traefik + Runway stack with automatic HTTPS via Let's Encrypt. Runway publishes no host ports; traffic enters only through Traefik on `:443`.

[`.env.traefik.example`](../.env.traefik.example) ships the full flag set for this topology — `cp .env.traefik.example .env`, fill it in, then `docker compose -f docker-compose.traefik.yml up -d`. The mandatory keys:

```bash
APP_HOST=0.0.0.0
TLS_TERMINATED=true                       # Traefik terminates TLS
CORS_ORIGINS=https://runway.example.com   # your public origin
DB_ENCRYPTION_KEY=<fernet key>            # generate below
INGEST_API_KEY=<strong secret>            # for sidecar ingestion
RUNWAY_HOST=runway.example.com            # public DNS name (Host rule)
ACME_EMAIL=you@example.com                # Let's Encrypt contact
# Optional but recommended for a public deployment:
ADMIN_API_KEY=<strong secret>             # protect admin/config endpoints
TRUSTED_PROXY_IPS=<traefik container IP>  # per-client ingest rate limiting (see below)

# Generate the Fernet key:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> [!NOTE]
> Let's Encrypt's HTTP-01 challenge needs ports **80 and 443** publicly reachable and a real DNS **A record** for `RUNWAY_HOST` pointing at the host. The same labels apply if you already run Traefik — copy the `runway` service onto your existing proxy network.

#### Sidecars behind Traefik

Runway publishes no host port in this topology — `:8765` lives only on the internal `proxy` network. Sidecars reach the server through the **public URL**; `/api/v1/fleet/ingest` is just another path under the `Host()` router, so it rides through Traefik on `:443`:

```bash
python3 scripts/sidecar.py --api-url https://runway.example.com --api-key <INGEST_API_KEY>
```

This applies to **remote and same-host sidecars alike** — the sidecar always runs natively (it needs host keychains/cookies/files) and talks to the server over the network. Going through Traefik is the preferred path: it's TLS-encrypted, which matters because ingest payloads carry OAuth tokens and cookies (HMAC protects integrity, not confidentiality). The sidecar needs only outbound HTTPS — no inbound ports.

Two things to know:

- **Rate-limit bucketing.** The ingest limiter (600/min) keys on client IP. Behind a proxy that IP is Traefik's, so every sidecar collapses into one bucket unless you set `TRUSTED_PROXY_IPS` to Traefik's container IP — then Runway honors the `X-Forwarded-For` Traefik sets and buckets per real client (this also gates `X-Forwarded-User` admin auth). Harmless for a couple of sidecars; set it for a fleet. The match is exact (not CIDR), so pin Traefik to a static IP if you depend on it.
- **Same-host shortcut.** To let a local native sidecar skip the public DNS → Traefik round-trip, add a loopback publish to the `runway` service — `ports: ["127.0.0.1:8765:8765"]` — and point that sidecar at `http://localhost:8765`. Remote sidecars keep using the public URL.
- **Certificate trust.** The sidecar bundles [`certifi`](https://pypi.org/project/certifi/), so a valid public cert (Let's Encrypt via Traefik) verifies with no extra setup. If you terminate TLS with a **self-signed or internal-CA** cert, hand the sidecar your CA chain via `ca_bundle` in its config (or `RUNWAY_CA_BUNDLE` / `SSL_CERT_FILE` env); `RUNWAY_INSECURE=1` disables verification entirely as a trusted-network last resort. See [sidecar.md → TLS / certificate errors](sidecar.md#tls--certificate-errors-certificate_verify_failed).

## Continuous deployment & the dev/prod split

If you already run Traefik (rather than the bundled stack above), attach Runway to your existing external `proxy` network with a **`docker-compose.override.yml`** — Compose auto-merges it onto the generic `docker-compose.yml`. Copy the tracked template and edit the host/resolver:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml   # gitignored
docker compose up -d                                                 # base + override
```

The override pins the **`:edge`** image, drops host port publishing, joins `proxy`, and adds your router labels (see the template for the full set).

**How updates reach a running deployment.** The SPA is baked into the server image (the `Dockerfile` copies `webapp/dist/`), so **UI/server changes ship in the server image — not via the sidecar edge channel** (that channel only updates the collector binary). Two channels:

- **`:edge`** — rebuilt on every push to `main`. Pull it to track your latest work: `docker compose pull runway && docker compose up -d runway` (optionally automate with watchtower).
- **`:latest` / `:vX.Y.Z`** — published only on a Release-Please release; pin these for deliberate, change-controlled updates.

Schema upgrades are forward-safe: there's no Alembic, just idempotent `create_all` + `ALTER TABLE ADD COLUMN` at startup (`app/core/db.py`), so bumping the image never breaks an existing DB. (No automatic *down*grade — pin a known-good tag to roll back.)

**Dev alongside prod — separate data dirs.** `make dev` / `make dev-all` / `make sidecar` default `RUNWAY_CONFIG_DIR` to the gitignored **`./data`** (override with an explicit env value), so the dev loop never touches your real data. Keep **prod data in `~/.config/runway`** by pointing the override's volume at the home dir:

```yaml
    volumes: !override
      - ${HOME}/.config/runway:/home/runway/.config/runway
```

Because the container's `runway` user is UID 1000, a host whose user is also UID 1000 can bind-mount `~/.config/runway` **in place — no copy, no chown**. SQLite is single-writer, so ensure only one process writes it: stop `make dev-all` (now on `./data` anyway) before `docker compose up -d`, and run your daily prod sidecar against the deployed URL (its own home config dir), reserving `make dev-all`'s bundled sidecar for the `./data` sandbox. To iterate against real data, snapshot prod into dev with `sqlite3 ~/.config/runway/runway.db ".backup ./data/runway.db"` (WAL-safe) while dev is stopped.

## Sidecar deployment

### Same host

Runs alongside a Python server during development, or alongside a Dockerised server on the same machine:

```bash
make sidecar
```

`make sidecar` sources `.env`, so `RUNWAY_CONFIG_DIR` and `INGEST_API_KEY` automatically align with the local server. `make dev-all` does this in one command (server + Vite frontend + sidecar in parallel, Ctrl-C stops all); `make run-all` is the production-build equivalent (built SPA served by the server + sidecar).

### Remote hosts

Each workstation runs its own sidecar pointed at the server:

```bash
python3 scripts/sidecar.py \
  --api-url http://runway-server:8765 \
  --api-key <strong-shared-secret>
```

Pre-built binaries are attached to every [GitHub release](https://github.com/s3ntin3l8/runway/releases): `Runway-Sidecar-macOS-<version>.zip` and `Runway-Sidecar-Windows-<version>.zip`. See [sidecar.md](sidecar.md) for the desktop app installer flow and payload format.

The sidecar only needs outbound HTTP — no inbound ports.

## Per-provider sidecar requirements

| Provider | Sidecar required? | Notes |
|----------|:-----------------:|-------|
| **Claude** | ⚠️ Yes for cookie-based auth | OAuth bearer via `CLAUDE_CODE_OAUTH_TOKEN` works server-only |
| **Gemini** | No (OAuth on server) | Sidecar adds local-log enrichment |
| **GitHub Copilot** | No | `GITHUB_TOKEN` API works everywhere |
| **ChatGPT** | ⚠️ Yes for cookie-based auth | OAuth bearer via `CHATGPT_OAUTH_TOKEN` works server-only |
| **OpenRouter** | No | `OPENROUTER_API_KEY` works everywhere |
| **MiniMax** | No | `MINIMAX_API_KEY` works everywhere |
| **Ollama** | ⚠️ Yes | Cookie extraction needs host access |
| **OpenCode** | Optional | Web API preferred, sidecar provides local DB fallback |
| **zAI API / zAI Plan** | No | API key works everywhere |
| **Kimi API** | No | `KIMI_API_KEY` works everywhere |
| **Kimi Coding** | ⚠️ Yes | Sidecar extracts cookie (or set `KIMI_AUTH_TOKEN`) |
| **Kimi K2** | No | API key works everywhere |
| **Antigravity** | ⚠️ Yes | Sidecar-only — reads local IDE JSON file |

**Legend:** ⚠️ Yes = sidecar required for full coverage. "No" means the server-side API path is enough, though a sidecar adds enrichment (token breakdowns, session counts, per-message events).

## Configuration

The relevant env vars apply to both runtimes:

| Variable | Required | Purpose |
|----------|:--------:|---------|
| `INGEST_API_KEY` | ✅ for remote sidecars (recommended for all) | HMAC-signing secret shared between server and sidecars |
| `GITHUB_TOKEN` | optional | GitHub Copilot API |
| `OPENROUTER_API_KEY` | optional | OpenRouter API |
| `MINIMAX_API_KEY` | optional | MiniMax API |
| `KIMI_API_KEY` | optional | Kimi API |
| `ZAI_API_KEY` | optional | zAI API & Plan |
| `DB_ENCRYPTION_KEY` | ✅ when `APP_HOST != 127.0.0.1` | Fernet key for sensitive metadata at rest |
| `TLS_TERMINATED` | ✅ when `APP_HOST != 127.0.0.1` | Operator assertion that an upstream proxy terminates TLS |
| `CORS_ORIGINS` | ✅ when `APP_HOST != 127.0.0.1` | Comma-separated origin allow-list |
| `ADMIN_API_KEY` | optional | Protects dashboard + admin endpoints from non-localhost callers |

For local single-host development the only mandatory variable is `INGEST_API_KEY` (default `sidecar-default-secret` ships in `.env.example`). For any multi-host or Docker deployment the three security gates (`DB_ENCRYPTION_KEY`, `TLS_TERMINATED`, `CORS_ORIGINS`) become hard requirements; the server fails fast on startup otherwise.

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

### API keys in Docker

**Option 1:** Environment variables (simple, standard)
**Option 2:** Sidecar-only (no keys in container, requires sidecar on every credential-bearing host)
**Option 3:** Docker secrets (Swarm/Kubernetes)

### Network

- Use HTTPS in production (reverse proxy terminating TLS)
- Rotate `INGEST_API_KEY` periodically
- Sidecars only need outbound HTTP — no inbound ports

---

See [sidecar.md](sidecar.md) for detailed sidecar configuration and the ingest payload format.

*Last updated: 2026-06-14*
