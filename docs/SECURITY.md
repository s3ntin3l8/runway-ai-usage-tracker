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

## 🔒 Response Headers

Every response carries a defence-in-depth header set:

- `Content-Security-Policy`: `default-src 'self'`, with `'unsafe-inline'` for scripts and styles (required while `components.js` still emits inline `onclick="…"`), and the Google Fonts origins allow-listed. `frame-ancestors 'none'` blocks framing.
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY` (for older user agents that ignore `frame-ancestors`)
- `Referrer-Policy: no-referrer`

## 📜 Audit Log

Every successful admin mutation (sidecar pause/resume/delete/patch, webhook CRUD, provider-config writes, token-cache refresh/evict) is written to the `audit_log` table with actor, source IP, action, target, and a JSON payload. Read it via `GET /api/v1/system/audit-log` (admin-only) or from the **Settings → Audit Log** panel.

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
