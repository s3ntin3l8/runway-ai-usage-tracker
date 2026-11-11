# Security

Runway is local-first: its primary topology is a developer machine, its secondary one a self-hosted server behind a TLS reverse proxy. This document is the policy: what is trusted, what is verified, what is encrypted, and what an operator must do. Evidence and tracked gaps are in [security-validation.md](security-validation.md). To report a vulnerability, open a private security advisory on GitHub or contact the maintainer directly.

## Trust model

1. **Database-backed.** Credentials, configuration and usage history live in SQLite. `DB_ENCRYPTION_KEY` encrypts sensitive fields at rest. Credentials can also come from environment variables for stateless deployments.
2. **Provider calls come from the server.** The sidecar extracts and forwards raw data; it does not call provider APIs on the server's behalf.
3. **Signed ingestion.** Sidecar-to-server data is HMAC-signed.
4. **No credential files on the server.** The server reads only its own `github_oauth.json`; CLI logins reach it through a sidecar, an env var or Settings.

[`.env.example`](../.env.example) is the contract for every supported variable. Every variable is optional on a loopback bind; the app starts with no `.env`.

Collector session credentials passed as explicit `Cookie` headers through
`http_request_with_retry` are not forwarded cross-origin on redirects. The pinned
httpx 0.28.1 removes explicit cookie headers on every redirect (including same-origin
and `www.` redirects), then derives cookies from its cookie store. A MockTransport
regression test in `tests/unit/test_utils.py` verifies that an explicit session
cookie reaches the initial host only, including a redirect chain that returns to
that host. No custom redirect policy is needed for these explicit headers.

## Operator checklist

- Bind to a network only behind a reverse proxy that terminates TLS, and satisfy the [startup gates](#multi-host-startup-gates).
- Use HTTPS everywhere sidecars connect; HMAC does not provide confidentiality.
- Rotate credentials quarterly, or at once on suspected exposure: create the new key at the provider, update the value, revoke the old one. Rotating `INGEST_API_KEY` requires re-pairing every sidecar.
- Put secrets in a secret store (GitHub repository secrets in CI), inject them through `env:` at runtime, and never log them.
- Enable the git hooks (`make install`); the secret scan and the rest of the commit checks are described in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Ingestion (sidecars)

Sidecars send metrics, tokens and per-message events to `POST /api/v1/fleet/ingest`, authenticated with HMAC-SHA256 over the JSON body and an `X-Timestamp`, keyed by `INGEST_API_KEY` (or an enrolled device key).

- The server rejects nonfinite timestamps and accepts a 5-minute window. Each valid signature is claimed once in SQLite, so a replayed envelope returns `409` (`replayed_request`); unavailable receipt storage returns `503`. Retries use a fresh timestamp and signature, and event identifiers keep event-level idempotency.
- Ingest is capped at 600 requests/minute per source IP and at 8 MiB per request, regardless of `Content-Length`.
- `GET /api/v1/fleet/config` needs no key, but only a request signed with the same HMAC (which binds `sidecar_id`) receives account ids, tag hints and per-account credential tokens. Unsigned callers on a non-loopback bind get the enabled/strategies view; an invalid signature is rejected with 401.
- 400 responses report field paths and error types only, never the submitted value.
- Session names are free text and may be sensitive. The server cleans them and never logs or raises a name: logs carry counts only. Keep this rule in new code on both sides.
- Sidecar server URLs reject embedded credentials, fragments and redirects. Plain HTTP is allowed only for loopback hosts; `RUNWAY_INSECURE` / `tls_insecure` disables certificate verification (not remote HTTP) and should give way to a configured CA. The sidecar's settings server validates Host and exact Origin and never embeds the saved ingest key in HTML.

### Unidentified credentials

For providers whose collector can ask the upstream API who owns a credential (Anthropic, ChatGPT, Gemini, Antigravity, GitHub, OpenCode), a sidecar sends a credential it cannot attribute to the server flagged `identity_pending`; the server verifies it against the provider.

- Destinations are fixed literals in the collectors; a payload supplies only header values.
- The account email comes from the provider, not the sidecar, and never overwrites an operator's own mapping.
- A pending bundle is readable only through its pinned source: it never enters the shared `default` cache, never uses the server's own credential, and the server stores only a whitelisted quota preview.
- Retries back off exponentially (15 minutes up to 6 hours), at most five pending sources per provider per cycle.
- Identity is not authenticated beyond the shared key: whoever holds `INGEST_API_KEY` can claim any `sidecar_id` or account. Treat a sidecar host that can reach the key as trusted for every account it could pair with.

## Sidecar pairing

A sidecar can be set up from the dashboard (Fleet → Add sidecar → Generate pairing link) instead of pasting the server URL and key.

1. **Mint (admin).** `POST /api/v1/fleet/pairing-codes` (10/min, audited) creates a random 10-character Crockford-base32 code; only its SHA-256 is stored. It expires after `PAIRING_CODE_TTL_SECONDS` (default 10 minutes). The link is `runway-sidecar://pair?server=<url>&code=<code>`, where the server URL is `PUBLIC_URL`, else the admin's browser origin, else the request base URL.
2. **Confirm.** Any web page can open a `runway-sidecar://` link and pairing re-points where the machine ships credentials, so the sidecar never pairs silently: the link opens a local confirmation page on the tray's loopback settings server (Origin-checked), which names the target server, warns if it replaces the current one, and waits for an explicit click.
3. **Redeem.** The sidecar calls `POST /api/v1/fleet/pair` with the code, over HTTPS for non-loopback servers. A conditional update makes the code single-use even under races; unknown, expired and reused codes get one identical 400. Redeem is limited to 10/min/IP and every outcome is audited. The response is `{api_url, api_key}`.

When the OS launches a second sidecar process for the link, it forwards it to the running tray with a per-run random token held in an owner-only control file, which a browser cannot read or forge. Pairing delivers the same shared key as manual setup; it changes delivery, not the trust model.

## Multi-Host Startup Gates

When `APP_HOST` is not loopback, the server refuses to start unless all of these are set:

| Setting | Why |
|---|---|
| `DB_ENCRYPTION_KEY` | Encrypts credentials at rest. |
| `TLS_TERMINATED=1` | Operator assertion that a proxy terminates TLS in front of Runway. |
| `CORS_ORIGINS=…` | Explicit allow-list; a wildcard with credentials is rejected by browsers. |
| `ADMIN_API_KEY` or `TRUSTED_PROXY_IPS` | Off loopback, at least one real gate must exist or every request would be admin. |

A misconfiguration fails at import time with a clear `RuntimeError`. Blank values count as unset, and a malformed `DB_ENCRYPTION_KEY` also fails at startup rather than falling back to plaintext. If encryption fails at runtime, credential writes return 503 and nothing is stored in plaintext. Local mode requires Host to name a loopback address (DNS-rebinding protection); network deployments need an explicit CORS allow-list, and requests with a foreign Origin are rejected before handlers run.

If you launch uvicorn yourself, pass `--no-proxy-headers`. The built-in server already ignores forwarded headers, so trusted-proxy decisions use the immediate socket peer: set `TRUSTED_PROXY_IPS` to the proxy, strip client identity headers there, and never infer the trusted hop from `X-Forwarded-For`.

**Where else secrets rest:**

- `github_oauth.json`: the `access_token` is encrypted with `DB_ENCRYPTION_KEY` when one is set, and re-encrypted on first read if saved before a key existed. Without a key it stays plaintext, like every other secret.
- The sidecar offline queue holds usage only: credential-carrying cards are dropped before queueing and the queue files are 0600.
- Logs are redacted at write time (bearer values, JWTs, provider key shapes, credential-looking `name=value` pairs) on the sidecar, again when forwarded, and again on the server before storage.

## Authentication

Three paths gate every mutating endpoint, resolved by `resolve_auth` in `app/core/security.py` for both the `require_admin_key` dependency and the `/system/settings` probe:

1. **Localhost trust** — loopback client on a loopback-only bind.
2. **Reverse-proxy SSO** (recommended off-loopback) — a forward-auth proxy asserts the user through an identity header, trusted only from an IP in `TRUSTED_PROXY_IPS`. Header names, group/user allow-lists, the Authentik setup and the router split are in [forward-auth.md](forward-auth.md). The IP allow-list is the security boundary: the proxy must strip client-supplied identity headers, and the list must contain only IPs you control.
3. **Admin key** — `ADMIN_API_KEY` as `X-Admin-Key`, or exchanged in the browser for a session cookie. It stays the break-glass path when SSO is configured.

Runway deliberately has no built-in login, OIDC client or role model; multi-user identity is the proxy's job.

**Browser sessions.** `POST /api/v1/auth/session` validates the key once (10/min) and sets an `HttpOnly`, `SameSite=Strict` cookie (`runway_session`, `Secure` when `TLS_TERMINATED=1`) holding a Fernet token with only an expiry. It is signed with `SESSION_SECRET` (generated on first use, stored encrypted, separate from `DB_ENCRYPTION_KEY` and never derived from the admin key). Lifetime is `SESSION_LIFETIME_HOURS` (12), or `SESSION_REMEMBER_DAYS` (30) with "remember me". `POST /api/v1/auth/logout` clears one browser; `POST /api/v1/auth/revoke-all` rotates `SESSION_SECRET` and ends every session ("sign out everywhere", Settings → System → Session) without touching `DB_ENCRYPTION_KEY`.

**Read access.** Private usage, event, fleet, provider-configuration and system reads need the same admin authentication as writes. `/system/settings`, `/system/health` and `/system/sidecar-downloads` stay public for bootstrap, and fleet config keeps its signed/redacted contract. A LAN proxy cannot grant local admin trust. Vite binds `127.0.0.1` by default; for remote development set `VITE_HOST` and use a network-mode backend with an admin key, CORS origins and TLS. `X-Runway-Dev-Remote` is an internal marker the dev proxy overwrites; it never grants authentication, and production proxies should strip it.

**Webhooks.** Destinations must be HTTPS Slack (`hooks.slack.com/services/…`) or Discord (`discord.com/api/webhooks/…`) URLs on port 443, validated at save and at delivery; custom hosts and redirects are unsupported.

## Response headers

Every response carries a strict `Content-Security-Policy` (`default-src 'self'`; scripts only from `'self'`; inline styles allowed only for runtime style attributes; `frame-ancestors 'none'`), plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`. The SPA bundles all scripts and fonts, so no CDN is allowed.

## Audit log

Every successful admin mutation (sidecar pause/resume/delete, webhook changes, provider-config writes, token-cache actions) is written to `audit_log` with actor, source IP, action, target and a JSON payload. Rows also carry `actor_type` (`localhost`, `proxy`, `session`, `api-key`, `none`) and, for the proxy path, the asserted identity, email and groups. Read it through `GET /api/v1/system/audit-log` (admin) or Settings → Audit log. It is a diagnostic trail, not a tamper-evident log: the table is append-only in the application, but anyone with database access can delete rows.

## Debug captures

`GET /api/v1/system/debug/raw/{provider_id}` (admin, rate-limited) runs a collector live and returns the upstream exchanges. Secret headers are masked and URLs, bodies and errors pass through `app.core.log_redaction`, which replaces token shapes, emails and sensitive keys with `[REDACTED]`. This is best-effort pattern matching: treat captures as sensitive.

`POST /api/v1/system/debug/sources/{provider_id}` (admin, 5/min) makes one real request per credential source to show which work, returning only status codes, error types and counts. It is side-effect-free by construction (token-cache writes, rejection flags, provider error events and server-side OAuth refresh are suppressed, so no refresh token is rotated), but the provider still sees the request.

## Verifying sidecar downloads and updates

Every release asset has a `.sha256` and a `.sigstore.json` bundle from keyless Sigstore signing in the release workflow. To prove a file was built by this repository's CI:

The exact `cosign verify-blob` command is under [Verify a download](sidecar.md#verify-a-download).

Packaged self-updates require both the checksum and the bundle. The sidecar verifies the artifact digest, certificate chain, GitHub issuer, transparency evidence and the exact signing identity above before extracting or installing anything; a failure leaves the installation unchanged, releases without bundles are refused, and there is no checksum-only fallback. The insecure server-TLS option does not affect update verification.
