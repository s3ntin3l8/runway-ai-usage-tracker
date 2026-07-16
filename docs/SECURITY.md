# Security Guidelines

## 🛡️ Core Principles

1. **Local-First & Database-Backed**: Runway uses an encrypted SQLite database to store credentials, configuration, and usage history. The `DB_ENCRYPTION_KEY` (from `.env`) encrypts sensitive fields at rest. Credentials and tokens can also be supplied via environment variables for stateless deployments.
2. **Server-Side API Calls**: All external provider API calls are made from the main application server, never the sidecar.
3. **Signed Ingestion**: Data sent from sidecars to the server is cryptographically signed to ensure integrity and authenticity.

## 🔑 Credential Management

### Environment Variables (.env)
Credentials MUST be stored in a `.env` file at the project root. This file is excluded from version control via `.gitignore`.
Refer to [.env.example](file:///.env.example) for the required structure.

### Automatic Protection (Pre-commit)
The project uses `pre-commit` hooks to prevent accidental credential leaking.

**Installation:**
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files  # Optional: Test on existing files
```

**What it checks:**
- `detect-secrets` - Scans for API keys, tokens, and secrets
- `check-json` - Validates JSON syntax
- `trailing-whitespace` & `end-of-file-fixer`

## 🛰️ Ingestion API Security (Sidecars)

In Multi-Host or Docker modes, sidecars send metrics, tokens, and per-message events via `POST /api/v1/fleet/ingest`. To prevent replay attacks and token theft, Runway uses **HMAC-SHA256 Signing**:

1. **Shared Secret**: The `INGEST_API_KEY` (from `.env`) is the HMAC secret.
2. **Signature Generation**:
   - Sidecars calculate a signature over the JSON body and a `X-Timestamp`.
   - The server verifies the signature matches and that the timestamp is within a 5-minute sliding window.
3. **Rate Limit**: `POST /ingest` is capped at **600 requests/minute per source IP** to bound damage from a stolen key or a misconfigured sidecar.
4. **Requirement**: Always use **HTTPS** for the `APP_HOST` in production to encrypt the request body during transit.

## 🚦 Multi-Host Startup Gates

When `APP_HOST` is not `127.0.0.1` / `localhost`, the server refuses to start unless three things are in place — HMAC alone is not enough confidentiality for sidecar payloads carrying OAuth tokens, cookies, and API keys.

| Setting             | Required | Why |
|---------------------|----------|-----|
| `DB_ENCRYPTION_KEY` | yes      | Encrypts credentials at rest. |
| `TLS_TERMINATED=1`  | yes      | Operator assertion that nginx / caddy / cloudflare / kube ingress terminates TLS in front of Runway. |
| `CORS_ORIGINS=…`    | yes      | Explicit allow-list. The legacy `["*"]` fallback combined with `allow_credentials=True` is rejected by browsers. |

These are fail-fast checks: a misconfigured deployment dies at import time with a clear `RuntimeError`, never silently exposing tokens over cleartext or serving with a broken CORS policy. Localhost binds are exempt by design — Runway's primary topology is "developer's laptop".

Blank values are treated as unset: an empty or whitespace-only `ADMIN_API_KEY` or `DB_ENCRYPTION_KEY` normalizes to `None` (so `KEY=""` in `.env` doesn't masquerade as a configured secret). A **malformed** `DB_ENCRYPTION_KEY` (set but not a valid Fernet key) also fails fast at startup, rather than silently falling back to plaintext storage.

The `["*"]` CORS fallback only takes effect when `APP_HOST` resolves to `127.0.0.1` / `localhost`; the gate above guarantees any non-localhost bind must ship an explicit allow-list, so wildcard CORS is never exposed off-host.

## 🔐 Application Authentication

Runway is local-first; its secondary mode is self-hosted behind a reverse proxy. The auth model reflects that — it does **not** try to be an identity provider. Three paths gate every mutating endpoint (resolved by `app/core/security.py:resolve_auth`, shared by the `require_admin_key` dependency and the `/system/settings` probe so they can never disagree):

1. **Localhost trust** — client is `127.0.0.1`/`::1` *and* the server is bound localhost-only. Zero-touch for the developer-laptop case; no key needed.
2. **Reverse-proxy SSO** *(recommended for multi-host)* — a forward-auth proxy asserts the user via a configurable identity header (`X-Forwarded-User` by default, or e.g. Authentik's `X-authentik-username`), trusted **only** when the source IP is in `TRUSTED_PROXY_IPS`. The proxy owns real identity; Runway just consumes it. See *Forward-auth / SSO* below.
3. **Built-in admin key** — `ADMIN_API_KEY`, presented either as the `X-Admin-Key` header (API/script clients) or, in the browser, exchanged once for a session cookie (below). This remains the **break-glass fallback** even when forward-auth is configured — it keeps working for scripts and for recovering access if the identity provider is unreachable.

### Decision (issue #92): why this shape

We deliberately **did not** build username/password login, a built-in OIDC client, or role-based access control. For a self-hosted tool, multi-user SSO is the reverse proxy's job — Authelia, oauth2-proxy, Traefik forward-auth, Cloudflare Access, or Tailscale all do it better and are already in most homelab stacks. Building those into Runway would add attack surface and maintenance for little gain. They remain open as scoped follow-ups (RBAC #101, OIDC #102) if concrete demand appears.

### Browser session cookies (hardened admin key)

The SPA no longer keeps the admin key in `localStorage` (readable by any XSS). Instead `POST /api/v1/auth/session` validates the key once and sets an **`HttpOnly`, `SameSite=Strict`** cookie (`runway_session`), marked `Secure` whenever `TLS_TERMINATED=1`. The cookie is a Fernet token carrying only an expiry — no identity, since the built-in path has a single admin.

- **Signing key** (`SESSION_SECRET`): auto-generated on first use, stored encrypted-at-rest in `system_config`, kept **separate from `DB_ENCRYPTION_KEY`**. It is never derived from `ADMIN_API_KEY`.
- **Lifetime**: `SESSION_LIFETIME_HOURS` (default 12), or `SESSION_REMEMBER_DAYS` (default 30) when "remember me" is checked.
- **Login throttle**: `POST /auth/session` is rate-limited (10/min) — the `X-Admin-Key` header path has no such throttle.
- **Migration**: a leftover `localStorage` key from older builds is auto-exchanged for a cookie on first load, then cleared.

### Logout & revocation

- `POST /api/v1/auth/logout` clears this browser's cookie.
- `POST /api/v1/auth/revoke-all` (admin) **rotates `SESSION_SECRET`**, invalidating every session everywhere at once (issue #100) — the "sign out everywhere" in **Settings → System → Session**. Because the session key is independent of `DB_ENCRYPTION_KEY`, this does **not** re-encrypt provider secrets.
- Rotating `DB_ENCRYPTION_KEY` is *not* required to drop sessions; use revoke-all.

### Forward-auth / SSO (recommended multi-host pattern)

Put a forward-auth proxy in front and trust its identity header — this is how Runway supports Authentik, Authelia, oauth2-proxy, Cloudflare Access, and Tailscale without a built-in OIDC client (see *Decision* above). Minimal shape, composed with the startup gates above:

```yaml
# Runway env
APP_HOST=0.0.0.0
TLS_TERMINATED=1
DB_ENCRYPTION_KEY=<fernet-key>
CORS_ORIGINS=https://runway.example.com
TRUSTED_PROXY_IPS=10.0.0.2          # the proxy's source IP, NOT a CIDR you don't control
# ADMIN_API_KEY optional — the proxy is the gate; the key is the break-glass fallback
```

**Header names are configurable** (`app/core/security.py:resolve_auth`), so a proxy that emits non-standard header names doesn't need remapping:

| Setting                         | Default              | Purpose |
|----------------------------------|----------------------|---------|
| `FORWARD_AUTH_USER_HEADER`       | `X-Forwarded-User`   | Asserted username — grants the session when trusted. |
| `FORWARD_AUTH_EMAIL_HEADER`      | `X-Forwarded-Email`  | Recorded in the audit log (`actor_meta_json`). |
| `FORWARD_AUTH_GROUPS_HEADER`     | `X-Forwarded-Groups` | Recorded in the audit log, and checked against `FORWARD_AUTH_ALLOWED_GROUPS` if set. |

A CGI-style `Remote-User` header is always accepted as a fallback when the configured user header is absent, for back-compat.

**Authentik**: point the three header settings at the outpost's native headers — no proxy-side header renaming required:

```yaml
FORWARD_AUTH_USER_HEADER=X-authentik-username
FORWARD_AUTH_EMAIL_HEADER=X-authentik-email
FORWARD_AUTH_GROUPS_HEADER=X-authentik-groups   # Authentik pipe-delimits multiple groups; both "|" and "," are accepted
```

Configure an Authentik **Proxy Provider** in *forward auth (single application)* mode bound to Runway's outpost, then wire your reverse proxy's forward-auth middleware to it (e.g. Traefik's `forwardAuth` middleware pointed at the outpost, with `authResponseHeaders` listing the three headers above so they reach Runway). Other identity providers:

- **oauth2-proxy / Authelia**: configure them to inject `X-Forwarded-User` (and optionally `X-Forwarded-Email` / `X-Forwarded-Groups`) — the defaults already match.
- **Cloudflare Access / Tailscale**: terminate identity at the edge / tailnet; forward the asserted user header from a fixed proxy IP listed in `TRUSTED_PROXY_IPS`.

In every case, ensure the proxy **strips client-supplied** identity headers before adding its own — the IP allowlist (`TRUSTED_PROXY_IPS`) is load-bearing: without it, anyone could forge `X-Forwarded-User` directly. Never list an IP range you don't fully control.

**Optional authorization allowlist** — defense-in-depth on top of the identity provider's own app binding. Empty (default) trusts any user the proxy asserts, matching prior behavior:

```yaml
FORWARD_AUTH_ALLOWED_GROUPS=runway-admins    # comma-separated; matched against FORWARD_AUTH_GROUPS_HEADER
FORWARD_AUTH_ALLOWED_USERS=alice,bjorn       # comma-separated; matched against the asserted username
```

If set, a proxy-asserted user who matches neither list does **not** get the proxy trust branch — the request falls through to the session cookie / admin-key checks instead of a hard failure, so a real admin can still log in with the break-glass key even while SSO is locked down to a specific group.

Once forward-auth is configured, the `/system/settings` probe adds `"forward_auth"` to `auth_methods` and the dashboard's Settings → About / System sections surface the SSO user — the browser never needs the admin key.

## 🔒 Response Headers

Every response carries a defence-in-depth header set:

- `Content-Security-Policy`: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`. The v2 SPA bundles all scripts and fonts (no CDN, no inline handlers), so `script-src` is a strict `'self'`; `style-src` keeps `'unsafe-inline'` only for the runtime style *attributes* ECharts/Radix set. `frame-ancestors 'none'` blocks framing.
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY` (for older user agents that ignore `frame-ancestors`)
- `Referrer-Policy: no-referrer`

## 📜 Audit Log

Every successful admin mutation (sidecar pause/resume/delete/patch, webhook CRUD, provider-config writes, token-cache refresh/evict) is written to the `audit_log` table with actor, source IP, action, target, and a JSON payload. Read it via `GET /api/v1/system/audit-log` (admin-only) or from the **Settings → Audit Log** panel.

Attribution is structured (issue #103): alongside the human-readable `actor` string, each row records `actor_type` (`localhost` / `proxy` / `session` / `api-key` / `none`) and, for the proxy path, the asserted identity plus any `X-Forwarded-Email` / `X-Forwarded-Groups` in `actor_meta_json`. Rows predating this degrade gracefully (null structured fields).

Scope: this is a diagnostic trail with the same trust model as the rest of Runway — useful for "what happened when something surprising changed" — not a legal-grade tamper-evident log. The table is append-only at the application layer; rows can still be deleted by anyone with direct DB access.

## 🔄 Maintenance & Hygiene

### Credential Rotation
Rotate credentials quarterly or immediately if you suspect exposure.
1. Generate a new key/token from the provider dashboard (GitHub, Anthropic, etc.).
2. Update the value in your local `.env`.
3. Revoke the old key/token immediately.

### CI/CD Security
When using GitHub Actions or other CI/CD pipelines:
- Use **Encrypted Secrets** (e.g., GitHub Repository Secrets).
- Never log secrets to standard output or log files.
- Inject secrets into the environment at runtime using the `env:` block in YAML.

## 👮 Reporting Vulnerabilities
If you discover a security vulnerability, please open a private security advisory on GitHub or contact the maintainer directly.
