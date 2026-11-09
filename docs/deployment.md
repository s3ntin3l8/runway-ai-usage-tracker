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

The server never performs local detection itself — LSP probes, browser cookies, and IDE/file introspection live in the sidecar. The sidecar pushes batches of up to 1000 events to `POST /api/v1/fleet/ingest` (HMAC-signed, 600/min/IP). The trust model is in [SECURITY.md](SECURITY.md); the architecture in [architecture.md](architecture.md).

## Server runtime

### Python

```bash
make install                # venv, deps, git hooks
cp .env.example .env        # add API keys
make dev-all                # server + Vite frontend (:5173) + local sidecar, hot reload, Ctrl-C stops all
```

For production-style runs (no hot reload — the server serves the built SPA at :8765):

```bash
make run-all                # build SPA, then server + sidecar in one command
# or separately:
make run                    # server only (serves webapp/dist)
make sidecar                # in a second shell, or under a process manager
```

The server binds `127.0.0.1:8765` and trusts localhost. To reach it from other devices set `APP_HOST=0.0.0.0` in `.env` and restart. A non-local bind turns on the [startup gates](#startup-gates). Never expose Runway to the internet without a reverse proxy and HTTPS.

### Docker

Use the shipped [`docker-compose.yml`](../docker-compose.yml) or the [Traefik stack](#docker-behind-traefik). Copy the matching example env file to `.env` and fill in the required security settings first. A raw `docker run` needs the same settings and a TLS-terminating proxy.

```bash
cp .env.example .env        # fill in the startup-gate variables
docker compose up -d
```

Restrict direct access to the published port `8765`; if the proxy runs on the same host, publish it as `127.0.0.1:8765:8765`.

> [!IMPORTANT]
> **Docker runs the server, not the sidecar.** A container has no desktop keychain or browser cookies. Every cookie- or local-file-backed collector needs a [sidecar](sidecar.md) on the host where those credentials live (see [Which providers need a sidecar](#which-providers-need-a-sidecar)).

### Startup gates

The container binds `0.0.0.0`, so the server refuses to start without `DB_ENCRYPTION_KEY`, `TLS_TERMINATED=1`, an explicit `CORS_ORIGINS` allow-list, and either `ADMIN_API_KEY` or `TRUSTED_PROXY_IPS` (forward-auth). Set `TLS_TERMINATED` only when a proxy really provides TLS. The same gates apply to any non-loopback `APP_HOST`; the reasoning is in [SECURITY.md](SECURITY.md#multi-host-startup-gates).

### Docker behind Traefik

[`docker-compose.traefik.yml`](../docker-compose.traefik.yml) is a self-contained Traefik + Runway stack with automatic HTTPS (Let's Encrypt). Runway publishes no host ports; traffic enters only through Traefik on `:443`. [`.env.traefik.example`](../.env.traefik.example) holds the full flag set: `cp .env.traefik.example .env`, fill it in, then `docker compose -f docker-compose.traefik.yml up -d`. The mandatory keys:

```bash
APP_HOST=0.0.0.0
TLS_TERMINATED=true                       # Traefik terminates TLS
CORS_ORIGINS=https://runway.example.com   # your public origin
DB_ENCRYPTION_KEY=<fernet key>            # generate below
INGEST_API_KEY=<strong secret>            # for sidecar ingestion
RUNWAY_HOST=runway.example.com            # public DNS name (Host rule)
ACME_EMAIL=you@example.com                # Let's Encrypt contact
ADMIN_API_KEY=<strong secret>             # required unless forward-auth uses TRUSTED_PROXY_IPS
TRUSTED_PROXY_IPS=<traefik container IP>  # forward-auth trust and per-client rate limiting

# Generate the Fernet key:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

> [!NOTE]
> The HTTP-01 challenge needs ports **80 and 443** publicly reachable and a DNS **A record** for `RUNWAY_HOST`. If you already run Traefik, attach Runway to it with the [override file](#updates-and-the-devprod-split) instead.

Once TLS is in place, add a forward-auth identity provider (Authentik, Authelia, oauth2-proxy, Cloudflare Access) so the dashboard signs in through SSO instead of the admin key: see [forward-auth.md](forward-auth.md).

#### Sidecars behind Traefik

`:8765` lives only on the internal `proxy` network. Sidecars reach the server through the **public URL**; `/api/v1/fleet/ingest` is just another path under the `Host()` router. Set `INGEST_API_KEY` on the sidecar host to the server's key:

```bash
RUNWAY_API_URL=https://runway.example.com RUNWAY_API_KEY="$INGEST_API_KEY" python3 scripts/sidecar.py
```

This applies to remote and same-host sidecars alike: the sidecar always runs natively and talks to the server over the network. Going through Traefik is preferred because ingest payloads carry OAuth tokens and cookies (HMAC protects integrity, not confidentiality). The sidecar needs only outbound HTTPS.

Four things to know:

- **Behind SSO, five routes must bypass it.** `/api/v1/fleet/ingest` is not the only path a sidecar calls: `config`, `credentials/manifest`, `source-sync` (an enrolled device's consent sync) and `pair` (one-time-code pairing) are too, and a login redirect on any of them fails quietly (ingest keeps working). The list, a verification probe and the Authentik equivalent are in [forward-auth.md](forward-auth.md#5-production-hardening-split-the-traefik-router).
- **Rate-limit bucketing.** The ingest limiter keys on client IP. Behind a proxy that is Traefik's IP, so every sidecar shares one bucket unless `TRUSTED_PROXY_IPS` names Traefik's container IP; Runway then honors the `X-Forwarded-For` Traefik sets (this also gates `X-Forwarded-User` admin auth). The match is exact, not CIDR, so pin Traefik to a static IP ([forward-auth.md](forward-auth.md#7-stabilizing-trusted_proxy_ips)).
- **Same-host shortcut.** To let a native sidecar skip the public DNS and Traefik round trip, publish a loopback port on the `runway` service (`ports: ["127.0.0.1:8765:8765"]`) and point that sidecar at `http://localhost:8765`.
- **Certificate trust.** The sidecar bundles [`certifi`](https://pypi.org/project/certifi/), so a public certificate verifies out of the box. For a self-signed or internal-CA certificate, give the sidecar your CA chain via `ca_bundle` in its config or `RUNWAY_CA_BUNDLE` / `SSL_CERT_FILE`; `RUNWAY_INSECURE=1` disables verification as a trusted-network last resort. See [sidecar.md](sidecar.md#tls--certificate-errors-certificate_verify_failed).

### Updates and the dev/prod split

To attach Runway to an existing external `proxy` network, use a **`docker-compose.override.yml`**, which Compose merges onto the base file:

```bash
cp docker-compose.override.example.yml docker-compose.override.yml   # gitignored
docker compose up -d                                                 # base + override
```

The override pins the `:edge` image, drops host port publishing, joins `proxy`, and adds router labels (see the template).

The SPA is baked into the server image, so UI and server changes ship in the image, not through the sidecar update channel (which only updates the collector binary). Two image channels:

- **`:edge`** — rebuilt on every push to `main`: `docker compose pull runway && docker compose up -d runway`.
- **`:latest` / `:vX.Y.Z`** — published on a release; pin these for change-controlled updates.

Schema upgrades are forward-safe ([architecture.md](architecture.md#data-model)); there is no automatic downgrade, so pin a known-good tag to roll back.

**Dev beside prod.** `make dev`, `make dev-all` and `make sidecar` default `RUNWAY_CONFIG_DIR` to the gitignored `./data`, so the dev loop never touches real data. SQLite is single-writer: keep only one process on a given database. To keep prod data in `~/.config/runway`, point the override's volume at it:

```yaml
    volumes: !override
      - ${HOME}/.config/runway:/home/runway/.config/runway
```

The container's `runway` user is UID 1000, so a host user with UID 1000 can bind-mount the directory in place. To iterate against real data, snapshot prod into dev while dev is stopped: `sqlite3 ~/.config/runway/runway.db ".backup ./data/runway.db"`.

## Sidecar deployment

### Same host

```bash
make sidecar
```

`make sidecar` sources `.env`, so `RUNWAY_CONFIG_DIR` and `INGEST_API_KEY` match the local server. `make dev-all` runs server, Vite and sidecar together; `make run-all` is the production-build equivalent.

### Remote hosts

Each workstation runs its own sidecar pointed at the server:

```bash
RUNWAY_API_URL=https://runway.example.com \
RUNWAY_API_KEY="$INGEST_API_KEY" \
python3 scripts/sidecar.py
```

Pre-built installers and portable payloads are attached to every [GitHub release](https://github.com/s3ntin3l8/runway-ai-usage-tracker/releases). [sidecar.md](sidecar.md) covers installation and pairing from the dashboard; [sidecar-build.md](sidecar-build.md) covers asset naming. The sidecar needs only outbound HTTP.

### Which providers need a sidecar

Per-provider credentials and collection paths are in the [collectors index](collectors/README.md). In short:

| Sidecar | Providers |
|---|---|
| **Required** for the full picture | Claude and Claude Console (cookie or CLI login), ChatGPT (cookie or CLI login), Gemini (CLI login), Antigravity (login or events from another host) |
| **Optional** (enrichment or discovery) | GitHub Copilot, OpenCode, xAI, Kimi Coding, Ollama |
| **Not needed** (API key works everywhere) | OpenRouter, DeepSeek, MiniMax, zAI, Kimi API, Kimi K2 |

"Not needed" still benefits from a sidecar for token breakdowns, session counts and per-message events. **The server reads no credential files** except its own `github_oauth.json`: CLI logins (`~/.gemini`, `~/.claude`, `~/.codex`, `~/.config/gh`, opencode, agy) reach it only through a sidecar, an env var, or Settings → Providers.

## Configuration

[`.env.example`](../.env.example) is the contract: every supported variable, its default and an explanation. The ones that decide whether a deployment works:

| Variable | Required | Purpose |
|---|:---:|---|
| `INGEST_API_KEY` | for sidecars | HMAC secret shared by server and sidecars. Empty disables ingest silently |
| `DB_ENCRYPTION_KEY` | non-loopback bind | Fernet key for sensitive data at rest |
| `TLS_TERMINATED` | non-loopback bind | Assertion that an upstream proxy terminates TLS |
| `CORS_ORIGINS` | non-loopback bind | Comma-separated origin allow-list |
| `ADMIN_API_KEY` or `TRUSTED_PROXY_IPS` | non-loopback bind | Admin key, or the forward-auth proxy's IP |
| `PUBLIC_URL` | optional | Server URL handed out in sidecar pairing links |
| `RUNWAY_CONFIG_DIR`, `DATABASE_PATH` | optional | Where config and the SQLite file live |
| `SESSION_LIFETIME_HOURS`, `SESSION_REMEMBER_DAYS` | optional | Admin session cookie lifetimes (12 h / 30 d) |
| `TOKEN_AUTO_REFRESH_ENABLED` | optional | Background OAuth refresh of tokens the server holds (default on) |
| `CLAUDE_CLOUD_COLLECTOR_ENABLED` | optional | Claude cloud-runner collector (default on); the Settings → Providers → Claude switch overrides it once set |
| `RUNWAY_BUILD_SHA` | optional | Commit shown in Settings → About; the official image bakes it in |

For local single-host development no variable is mandatory. `.env.example` ships `INGEST_API_KEY` empty: the server starts, but sidecar ingest is disabled.

### Providing credentials by hand

In Docker or on a headless host a provider login arrives as an environment variable, through a [sidecar](sidecar.md), or entered under **Settings → Providers** (add a provider, or open an existing account).

- **API key** — the provider's key or bearer token (for ChatGPT and Claude the full token, usually starting `eyJ…`). Takes priority over a cookie.
- **Session cookie** — a fallback for providers without a token. It expires with the browser session, so expect to renew it.

**Settings → Credentials** shows where each account's login comes from and when it expires.

### Polling

Collectors poll every 15 minutes by default. Override per account with the poll interval under Settings → Providers; a global default can be stored in `system_config` (`default_poll_interval_seconds`). Resolution order is per-provider, then global, then 900 s. After several polls with no quota change the poller sleeps at a 2-hour cadence; any usage event or `POST /api/v1/system/wake` wakes it. Sidecar-forwarded tokens are held in memory only.

### Memory

`SQLITE_TEMP_STORE` and `MALLOC_ARENA_MAX` tune server RAM: see [memory.md](memory.md).

## Network and secrets

- Use HTTPS in production (a reverse proxy terminating TLS); sidecars need only outbound access.
- Provide provider keys as environment variables, Docker secrets, or sidecar-only (no keys in the container; needs a sidecar on every credential-bearing host).
- Rotate `INGEST_API_KEY` periodically; every sidecar must then be re-paired or given the new key.
