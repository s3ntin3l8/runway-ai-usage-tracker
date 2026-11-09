# Upgrading to Runway 3.x

> **Scope.** This guide covers upgrading a **2.x** database to the **3.x** schema. Migrations run automatically at startup and need no manual step; the ones that change behavior are described below. Later 3.x releases may add further automatic migrations: after any upgrade, open **Settings → Data health** and run a scan.

## Before you upgrade

**Back up the database first.** `~/.config/runway/runway.db` (or your
`RUNWAY_CONFIG_DIR`) — a plain file copy is enough if the server is stopped;
otherwise use `sqlite3 runway.db "VACUUM INTO 'runway.db.bak'"` so you get a
consistent snapshot without stopping anything. The first boot after
upgrading runs a one-time, one-way cleanup (below) that deletes
cross-account duplicate events; there's no automatic rollback if something
about your data trips it up.

## First boot after upgrading

`usage_events` used to be unique on `(provider_id, account_id, event_id)`,
so the same message pushed under a second account (an operator retag, a tag
hint that arrived after the fact) was inserted again and double-counted.
The very first startup on an existing database now collapses those
duplicates and rebuilds the affected rollups from the surviving events —
this is what makes `(provider_id, event_id)` alone unique going forward.
It's a one-shot migration (a no-op on every later boot) and, even on a
database with hundreds of thousands of events, is a matter of seconds, not
minutes — the server does not accept requests until it finishes. Closed
historical windows (the frozen totals in `usage_windows`) are not
recomputed; a window that closed while the double-count was live keeps its
inflated total.

## Events held back under an unresolved identity

Previously, an event pushed with no resolvable account identity (no
saved credential to match it to, no operator tag yet) was attributed to a
`default` catch-all account and counted right away. That
event is now held in a pending queue — excluded from account totals —
until an admin assigns it. This is more conservative (nothing gets silently
mis-attributed to `default` and mixed with other accounts' totals) but
means usage that previously appeared automatically may now sit unassigned
after upgrading.

- The Fleet page shows an **Unassigned usage** banner whenever the queue is
  non-empty, with a picker per event.
- The account picker only offers accounts that already have a
  `provider_configs` row (Settings → Providers) for that specific provider —
  if the account you want isn't listed, add it there first.
- **If a configured account is still keyed `account_id="default"`**
  (label may show your email, but the row's own id is `default`), you'll
  see it in the picker labeled `<label> (default)` — a hint, not a
  requirement, that assigning there keeps the event on the shared `default`
  bucket rather than its own account. Nothing stops you from choosing it
  deliberately if that's what you want; re-key the config to its real
  account id first if it's not (Settings → Providers → that provider →
  Account ID).
- Nothing forces the queue empty. Events sit there safely (excluded from
  totals) for as long as you leave them.

## The server no longer reads CLI login files

Credentials now reach the server **only** from sidecars, env vars and Settings →
Providers. The server reads no credential file from its own host: not
`~/.gemini/oauth_creds.json`, `~/.claude/.credentials.json`, `~/.codex/auth.json`,
`~/.config/gh/hosts.yml`, the opencode `auth.json`, nor the agy token. The one
exception is Runway's own `github_oauth.json` in its config dir, written by the
UI's "Sign in with GitHub" flow. (The server may run in Docker with only its
config dir mounted; CLI logins live in a user's home, which is the sidecar's job.)

- **If you ran a single-host server with no sidecar** and relied on it finding
  your CLI logins: run the sidecar on that machine (`make dev-all` in a dev
  checkout), or set an env var / paste the credential in Settings → Providers.
- **Gemini** is now a sidecar provider ([deployment.md](deployment.md#which-providers-need-a-sidecar)). The server still
  refreshes a sidecar-pushed Gemini token itself, into its own token cache; it never
  writes back to `~/.gemini`.
- **Ignored settings.** These env vars were removed and are now ignored (a leftover
  `.env` entry does not break startup): `GEMINI_OAUTH_PATH`, `ANTHROPIC_OAUTH_PATH`,
  `ANTIGRAVITY_OAUTH_PATH`, `CHATGPT_AUTH_PATH`, `CLAUDE_STATUSLINE_PATH`,
  `CLAUDE_PROJECTS_DIR`, `GEMINI_SESSIONS_DIR` and `CHATGPT_SESSIONS_DIR`. Delete
  them from your `.env` / compose file.
- **Stale rows.** On first boot the server deletes the `server:<provider>:file:<label>`
  credential-source rows it registered for those files (env-var rows and
  `github_oauth.json` stay). Credentials a sidecar reports are unaffected.
- **Anthropic tier.** The plan/tier label no longer falls back to the
  `claudeAiOauth.subscriptionType` / `rateLimitTier` of a server-side
  `.credentials.json`; it comes from the API's organization info and usage response.

## Open Settings → Data health after upgrading

Settings → Data health scans for data-quality drift that long-lived installs accumulate: provider configs still keyed `default`, events under legacy provider ids or a stale `default` account, orphaned credential tags and gauge series, models priced at $0, and rollup drift. Each finding previews before you apply it. See [data-health.md](data-health.md). Worth a scan right after upgrading.

## GitHub accounts, Copilot token fallback, and `GITHUB_TOKEN`

- **GitHub accounts are keyed by login**, not email. After upgrading,
  open Settings → Data health and run the `misidentified_gauge_series` fixer
  to merge series that were previously keyed by email.
- **The Copilot v2/token fallback was removed.** Classic personal
  access tokens may now be rejected by the Copilot endpoints; sign in with
  GitHub or use `gh auth login` for an OAuth token.
- **The `GITHUB_TOKEN` setting was removed from the app settings.** The
  GitHub collector now reads `GITHUB_TOKEN` / `GH_TOKEN` only from the real
  process environment, so a value that lives only in `.env` is no longer
  read by the app itself. Export it in the process environment instead (the
  bundled Docker Compose files pass `.env` to the container via `env_file:`,
  so Compose deployments are unaffected).

## Credential-health alerts are on by default

Existing Discord/Slack webhooks fire when a credential they watch goes `expired` or `invalid` — a new
`credential_alerts` column defaults to `true` for every existing row, not
just newly created ones. Turn it off per webhook in Settings → Alerts if
you only want the original threshold alerts.

## `/fleet/config` returns less to unsigned callers

On a non-loopback bind, `GET /api/v1/fleet/config` now returns only the
`enabled`/`strategies` view to callers that don't sign the request (or
aren't hitting `127.0.0.1`) — account ids, tag hints, and credential tokens
are withheld. Sidecars built for this release sign every request and are
unaffected; a third-party integration reading this endpoint directly for
account-level detail will need to add the signature (see
`verify_config_signature` in `app/core/security.py`) or run against a
loopback bind.

## Stable-channel sidecar self-update

If your sidecar is on the stable release channel (not `edge`), one manual
step is needed to reach v3.0.0: download and run the new installer once
(same as any release with a major version bump). After that, normal
self-update resumes — this release also fixes the updater to correctly
find stable-channel release assets, which it previously couldn't.

## Non-localhost binds require an admin gate

Leaving `ADMIN_API_KEY` unset used to make every caller admin, even on a network bind. Runway now refuses to start on a non-localhost `APP_HOST` unless `ADMIN_API_KEY` **or** `TRUSTED_PROXY_IPS` is set (localhost binds are unaffected; a forward-auth deployment that sets `TRUSTED_PROXY_IPS` needs no change). Otherwise the server exits with a `RuntimeError` naming the gate; see [SECURITY.md](SECURITY.md#multi-host-startup-gates).

- A forward-auth deployment that points `FORWARD_AUTH_USER_HEADER` at a custom header (for example `X-authentik-username`) no longer falls back to a bare `Remote-User` header. Send the configured header.
- `POST /api/v1/usage/reset/{provider}`, `POST /api/v1/usage/collect/{provider}` and the GitHub device-flow endpoints (`/api/v1/auth/github/{init,poll,logout}`) now need a real admin gate on a network bind. The dashboard is unaffected; scripts need `X-Admin-Key`, a trusted forward-auth identity, or a loopback connection.

## Provider configuration

Provider settings use the multi-account grid. Provider configuration writes require an explicit account ID; the one-segment `PUT /provider-config/{provider_id}` is removed:

```http
PUT /api/v1/system/provider-config/openrouter/default
PUT /api/v1/system/provider-config/anthropic/alice@example.com
```

Use the stable account ID as the final path segment and URL-encode it. A first credential normally belongs to `default`. For Kimi API, Kimi K2, MiniMax, OpenRouter and zAI keys, lookup precedence is the saved account credential, then that account's token-cache entry, then the server environment variable, so a cached `default` key beats the env var.

## Docker

`docker-compose.yml` and `docker-compose.traefik.yml` force `APP_HOST=0.0.0.0` inside the container, overriding `.env`. (A loopback bind inside a container cannot receive published-port traffic, which used to leave a dead port while the healthcheck kept passing.) The container is therefore off-loopback and needs `DB_ENCRYPTION_KEY`, `TLS_TERMINATED`, `CORS_ORIGINS` and `ADMIN_API_KEY` or `TRUSTED_PROXY_IPS` before it starts; see [deployment.md](deployment.md#startup-gates).

`make run` sources `.env` the way `make dev` does, because some settings (`CORS_ORIGINS`, registry "env" credentials) are read from the process environment rather than through pydantic's `.env` loading.

When an upgrade removes legacy server credential-file sources, Home and Settings → Credentials show a notice naming the affected providers. Pair a sidecar on the machine with those CLI logins from Fleet, or save an API key in Settings → Providers. Dismissing the notice applies to the whole installation. Only removals observed by this version are reported.

---

## Automatic schema migrations

These run once at startup and need no action; they are listed so a database moving between 3.x releases has something to go on.

| Migration | What it does |
|---|---|
| `usage_windows_series_identity_v1` | Adds `provider_id` / `account_id` / `model_id` to `usage_windows` so a closed window cannot be recorded twice under two accounts. Tracked in the `runway_schema_migrations` ledger, so it runs once. |
| `_migrate_webhook_uniqueness` | Enforces `(provider_id, account_id, url)` uniqueness on `webhook_configs`. Rows with a `NULL` account are left alone (SQLite treats NULLs as distinct); the API rejects duplicates of the "all accounts" form. |
| `_migrate_credential_tag_scoping` | Rebuilds `credential_tags` for per-sidecar scoping: a nullable `sidecar_id` plus two partial unique indexes. Existing rows get `sidecar_id = NULL` (deployment-wide, as before). |
| `migrate_to_provider_event_identity` | Collapses cross-account duplicate `usage_events` and creates the unique index that makes `(provider_id, event_id)` the identity; see [First boot after upgrading](#first-boot-after-upgrading). A no-op once the index exists. |

The scan under Settings → Data health reports anything a migration left behind and offers the repairs.
