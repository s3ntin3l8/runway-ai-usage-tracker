# Architecture

Runway is a local-first monitoring tool for AI provider quotas with
SQLite-backed history. This document is the deep reference behind
[`AGENTS.md`](../AGENTS.md) — topologies, environments, the event-sourced
data model, collectors, and how CI actually builds and ships.

## Topologies

- **Two topologies**: Local (server + sidecar on same host) and
  Multi-Host/Docker (server + one or more remote sidecars). The server never
  performs local detection itself — all LSP probes, browser cookies, and
  IDE/file introspection live in the sidecar.
- **Docker rule**: No native desktop UI/keychains in the server container —
  credentials come from ENV vars or sidecar payloads.
- **Cookie collectors**: Claude, ChatGPT, Ollama, Kimi Coding, OpenCode need
  browser cookies; the sidecar extracts them and ships them to the server.

## Environments (dev vs prod)

Dev and prod are meant to run side by side with **separate data dirs** —
SQLite is single-writer, so never let two processes write one `runway.db`.

- **Dev**: `make dev-all` (hot reload). DB + sidecar config live in the
  gitignored **`./data`** (Makefile defaults `RUNWAY_CONFIG_DIR` there).
  Disposable sandbox.
- **Prod**: Docker. DB lives in the container's config dir
  (`/home/runway/.config/runway`), persisted via a host bind-mount — point it
  at the platform config dir (`~/.config/runway`) to keep prod data out of the
  repo. Attach to an existing reverse proxy with a **gitignored
  `docker-compose.override.yml`** (template:
  `docker-compose.override.example.yml`); the tracked `docker-compose.yml`
  stays a generic blueprint, and `docker-compose.traefik.yml` is the
  bundled-Traefik option. Non-localhost binds trip the multi-host gates (see
  *Data Model → Multi-host startup gates*).

**Updates / channels**: the SPA is **baked into the server image**
(`Dockerfile` copies `webapp/dist/`), so UI/server changes ship by pulling a
new **server image** — `:edge` (rebuilt on every push to `main`), and on a
release either `:latest`/`:vX.Y.Z` (stable) or `:beta`/`:vX.Y.Z-beta.N`
(prerelease beta). The **sidecar edge channel updates only the
collector binary, never the UI**. Schema upgrades are **forward-safe** (no
Alembic; `init_db` runs `create_all` + idempotent `ALTER TABLE ADD COLUMN`),
so bumping the image won't break an existing DB.

## Data model

Runway is **event-sourced**. The authoritative table is `usage_events` — one
row per assistant message — and everything else is a derived view. All models
live in `app/models/db.py`.

| Table | Role |
|-------|------|
| `usage_events` | Per-message events. Unique on `(provider_id, event_id)` — a re-push under a new account from the *same* sidecar re-attributes the row and moves its rollups (retag-safe); another sidecar can't steal it. `kind="message"` for billable activity, `kind="error"` for provider failures. Carries project-context enrichment — raw per-message `cwd`, indexed `project` (the session's root basename, derived in `EventIngestor` via `app/services/project_label.py` — worktree/tmp cwds collapse at the `/.claude/` boundary, and `scripts/consolidate_session_projects.py` consolidates per-session subfolder drift offline; backed by `ix_usage_events_project_ts`), `git_branch`, and `tool_names` — that powers the project/tool rankings. |
| `usage_period_rollup` | Pre-aggregated rollups (hour/day/month/year/lifetime × model × sidecar grain). Updated incrementally on each event ingest. |
| `usage_windows` | Closed-window archive — totals frozen at each authoritative `reset_at` boundary by `app/services/window_closer.py`. |
| `latest_usage` | Live gauge cards (`pct_used`, `limit_value`, `reset_at`) — what scrapers see. Merged via `merge_card_json` in `app/services/accumulator.py`. |
| `latest_usage_contributions` | Per-source last-good card payload behind a merged `latest_usage` row. One producer's complete report can't retire another producer's card — see [collection logic](collection_logic.md#current-card-reconciliation). |
| `quota_snapshots` | Append-only time-series of `pct_used`/`reset_at` observations. Written on every `upsert_latest_usage` call when `pct_used` is non-null. Backs the `%` history chart and the Theil-Sen forecast. |
| `provider_pricing` | Time-versioned per-(provider, model) prices used by `app/services/cost_calculator.py` so historical cost stays stable across price changes. |
| `provider_configs` | Per-provider user config — API keys, session cookies (Fernet-encrypted), account labels, poll intervals, per-strategy enable toggles. Unique on `(provider_id, account_id)`. |
| `credential_sources` | One row per credential *source* — a secret found in one place: a sidecar's file/env/cookie (`sidecar_id` set), a key pasted in Settings → Providers (`config:`), or an env var / file on the server host (`server:`, no origin). Holds the operator's `enabled`/`priority` failover order — a bundle whose access token has already expired is attempted after every live one, so priority orders usable credentials rather than dead ones; a source whose last attempt was rejected (`auth_failed`) goes behind every one that wasn't and rests between retries on the 15 min → 6 h doubling schedule (`next_retry_at`, reset by a success or by a changed secret at ingest), except that the last credential standing is always tried; `consecutive_failures` counts failed attempts in a row, and three `unavailable` ones make the source read `failing` unless a sibling has collected since — the credential's non-secret expiry/token types, `health` (reported as `untried` until a first collection attempt is recorded — a registered but never-used credential must not read as working), and collection provenance (`last_attempt_at`, `last_success_at`, `last_error`) stamped by the failover loop, and the `verified_subject` last seen behind a cookie source (account-switch detection) — the "active source" for an account is the one with the latest success. Backs `GET /system/credentials`. Unique on `(provider_id, account_id, source_id)`, so an account rename has to carry these rows with it (`config_rekey` does; rows an older build stranded are removed by `orphan_credential_sources`). |
| `provider_account_labels` | Operator display-name override for a discovered `(provider, account)`. |
| `credential_tags` | Operator mapping from a host-side credential origin (`path:...`/`env:...`, never the value) to a server account, optionally scoped to one sidecar or deployment-wide. Unresolved origins sit in `pending_credential_tags` until tagged in the Fleet UI. Origins are fingerprinted per credential, so a CLI re-login re-keys them; when the base location is unchanged and exactly one account was ever tagged there, the manifest carries that binding to the new origin (`set_by=rotation`) instead of reopening a pending row and withholding the token (#474). |
| `pending_credential_tags` | Credential origins a sidecar has reported but no operator tag exists yet — powers the "Untagged credentials" surface (`GET /fleet/credentials/tags/pending`). |
| `pending_usage_events` | Collected events awaiting account assignment (evidence-backed or manual) — `GET /fleet/events/pending`, assign via `POST /fleet/events/pending/assign`. |
| `sidecar_registry` | Known sidecars with hostname, custom name, tags, last-seen, version, OS, recent log lines, and a `collection_enabled` pause flag. |
| `webhook_configs` | Discord/Slack threshold alerts: `provider_id`, `account_id` (NULL = all accounts), `threshold_pct`, `url`, `channel`, last-fired timestamp, `credential_alerts` (opt-in, default on, for the credential-health alerts below). |
| `webhook_credential_alerts` | Dedup/re-arm state for credential-health alerts (`app/services/credential_alerts.py`): one row per `(webhook_id, provider_id, account_id)` bad episode, with a `healthy_since` hysteresis timestamp so a single healthy Token Health observation doesn't immediately re-arm. |
| `system_config` | Single-row global config — browser preference, default poll interval, dashboard layout JSON, user timezone. |
| `audit_log` | Append-only record of admin mutations (sidecar pause/resume/delete/patch, etc.). Diagnostic, not legal-grade. |
| `sidecar_pairing_codes` | One-time, short-lived (`PAIRING_CODE_TTL_SECONDS`) sidecar pairing codes, stored as SHA-256 only. Minted by admins (`POST /fleet/pairing-codes`, a `runway-sidecar://pair` deep link), redeemed once by a new sidecar (`POST /fleet/pair` → `api_url` + ingest key). See `app/services/pairing.py`, [SECURITY](SECURITY.md). |

**Ingest path:** Sidecar batches up to 1000 events per push to
`POST /api/v1/fleet/ingest` (HMAC-signed, rate-limited to 600/min per source
IP). Server runs `EventIngestor`, which deduplicates by
`(provider_id, event_id)` (re-attributing same-sidecar account changes),
computes cost via `cost_calculator`, updates rollups, and triggers
`window_closer._maybe_close_previous_window` on quota-window boundaries.

**Read / mutating paths:** the endpoint catalog lives in the
[API reference](api-reference.md). Non-obvious semantics worth knowing here:

- `/usage/fleet` adds `window_aggregations.longest` — per-model +
  per-sidecar splits aligned to the provider's longest active window
  (Claude weekly, Gemini daily, etc.), computed on demand from `usage_events`.
- `history/chart` takes `group=provider` for a cross-provider stack;
  `sessions/paginated` adds server-side sort + `project` filter; snapshot
  bucketing runs SQL-side via the `ix_quota_snapshots_series_ts` covering
  index.
- Forecasts: `/usage/forecast` (Theil-Sen regression on `quota_snapshots`,
  anchor-at-now) and `/usage/cost-forecast` (MTD + 7-day burn to EOM).
- Mutating endpoints (`usage/reset`, `usage/collect`, fleet pause/resume/
  update, pairing codes, `/system/{cleanup,wake,force-collect,check-updates}`,
  and the webhook/provider-config/app-config/dashboard-layout CRUD) go through
  `require_admin_key` and append to `audit_log`.

**Admin auth:** the dashboard logs in via `POST /api/v1/auth/session`
(validates `ADMIN_API_KEY`, sets an HttpOnly `SameSite=Strict` session
cookie, rate-limited 10/min); `POST /auth/logout` clears the cookie and
`POST /auth/revoke-all` rotates `SESSION_SECRET` to invalidate every session.
`SESSION_SECRET` is auto-generated, stored Fernet-encrypted in
`system_config`, and is separate from `DB_ENCRYPTION_KEY`. Scripts/API
clients can keep using the `X-Admin-Key` header. Blank
`ADMIN_API_KEY`/`DB_ENCRYPTION_KEY` env values normalize to unset, and a
malformed `DB_ENCRYPTION_KEY` fails fast at startup rather than silently
running plaintext. See [SECURITY](SECURITY.md).

**Account identity:** `app/services/account_identity.py:resolve_account_id` —
email-shaped label > the raw canonical id > PBKDF2 hash of the credential (64 hex) > `"default"`. Sidecar identity is the hostname;
never part of unique constraints on the canonical
`(provider_id, account_id)` pair. Every write path (cards, `EventIngestor`,
token-cache keys, provider-config PUT) stores ids via `canonical_account_id`
(emails lowercased, opaque ids verbatim); the sidecar mirrors it in
`scripts/sidecar_pkg/identity.py`, and
`app/services/account_canonicalization.py` repairs legacy rows at startup.

**Multi-host startup gates:** when `APP_HOST != 127.0.0.1`, the server
refuses to start without `DB_ENCRYPTION_KEY`, `TLS_TERMINATED=1`, an explicit
`CORS_ORIGINS` allow-list, and either `ADMIN_API_KEY` or
`TRUSTED_PROXY_IPS` — sidecar payloads carry tokens (HMAC isn't
confidentiality), and `resolve_auth`'s "no key configured" bypass only opens
on a loopback bind, so at least one real admin gate must exist off-loopback.
See [SECURITY](SECURITY.md).

**Data Health / repair logic:** all data-repair logic (retagging events,
rekeying a `provider_configs` row, merging gauge series, recosting, rebuilding
rollups/windows) lives in `app/services/maintenance/` — a `plan_*`/`apply_*`
pair per repair, each `apply_*` chunked (`_chunked_sql.py`'s id-cursor
`chunked_update`/`chunked_delete`) so it never holds SQLite's writer lock for
one giant transaction. `app/services/data_health/checks/` only decides *when*
to offer a repair (read-only `detect()`) and exposes it through
`/api/v1/system/data-health/*` (`app/services/data_health/jobs.py`'s
single-flight job registry) and the Settings → Data health page. Host-run
scripts (`scripts/recost_events.py`, `scripts/assign_default_events.py`,
`scripts/merge_gemini_default_account.py`) are thin wrappers over the same
functions, so an operator running one from the CLI and the in-app fixer can
never drift apart. See [data-health](data-health.md).

## Collectors

Strategies are categorized by data type, collected in phases, and merged into
a single card per provider. Anything `local` (CLI / statusline / log
scraping) executes inside the sidecar, not the server — server-side
collectors only do `api` and `web`.

| Type | Strategies | Where it runs | Provides |
|------|------------|---------------|----------|
| **quota** | api, web | server | Percentages, currency limits, tier |
| **enrichment** | local (cli / statusline / logs) | sidecar | Token breakdown, session counts, per-message events |

### Unidentified credentials

A sidecar that can't tell whose credential it found (no email in the id_token, no
configured identity) reports it as `identity_pending` and sends the secret only for
providers the server can verify: antigravity, anthropic, chatgpt, gemini, github and
opencode. The server then runs a dedicated `<provider>:default:identity-pending` collector
pinned to each pending source in turn; when its API call reveals the account, `CollectorManager`
binds that identity to the exact source and moves it to the account. How each provider
answers: Gemini and Antigravity ask Google's userinfo endpoint; Claude asks `claude.ai`
(`/api/account`) for a session cookie and `api.anthropic.com/api/oauth/profile` for an OAuth
token (the token's own holder; a token without the `user:profile` scope is refused and stays
"Needs mapping", and the organization's contact, which can be an admin, is never adopted);
ChatGPT reads the
email from the usage endpoint after exchanging the cookie. Anthropic files its pending bundles
under their source id rather than `default`, so the verifier looks across every cache slot
(`TokenCache.get_pending_sources`). A pinned run never reads the server host's own login (it
belongs to another account), and a source that works but cannot name its account stays pending
without blocking the others. Anything unverifiable stays "Needs mapping" for the operator, and
is retried with exponential backoff (`pending_credential_tags.next_verify_at`: 15 minutes
doubling to 6 hours; five sources per provider per cycle, oldest-due first). A new secret for
the same source resets it.
A ChatGPT token whose JWT already carries the email (`id_token`, or the
`https://api.openai.com/profile` claim of an access token, from a file or `CHATGPT_OAUTH_TOKEN`)
is identified by the sidecar and never needs verifying.

### Credential origins and operator tags

A credential's *origin* (`env:ZAI_API_KEY`, `path:/…/auth.json`, `cookie:kimi_coding/session`)
is what an operator tag is keyed on. For providers whose credential is a bare static key
(opencode siblings, plus the env-key providers kimi_api, kimi_k2 and zai) the sidecar suffixes
a fingerprint of the value (`env:ZAI_API_KEY#<fp>`), so a rotated key is a new origin and a
tag can't silently follow it. A tag written against the plain origin still applies to the
key-scoped one (the tag repo, the inventory and the sidecar's own hint lookup all fall back to
it), and the superseded plain-origin row is retired when its key-scoped successor first reports.

Cookies, keychain entries and OAuth bundles stay unfingerprinted on purpose: their secret
changes with every login or refresh, so a fingerprint would orphan the tag each time. A tag on
a **cookie or keychain** origin therefore can't be deployment-wide (422); on a **path** or
**env** origin it can (a shared home directory is one origin), and the Credentials view warns
that such a tag follows an account switch.

A browser switching accounts on one machine behind a machine-scoped **cookie** tag is caught
at poll time. Collectors whose provider reports the account's email (Claude via `/api/account`,
ChatGPT via the usage endpoint, Ollama via the page header) set `verified_identity`; when it
resolves to a different canonical account than the tagged one, `CollectorManager` drops the
tag, takes the source out of the old account (nothing from that poll is published there) and the
sidecar's next push files it as pending, so the identity verifier maps it to the new account. It
never second-guesses a tag on a non-email account (hash-keyed or label-based), a deployment-wide
tag (it applies on other machines too), or a source whose provider reported no email.

Providers that never reveal an email may set `verified_subject` instead, a stable opaque id of
whoever the cookie logs in as (Kimi: the `sub` of the `kimi-auth` JWT; opencode is not watched,
its `subscriberUserId` names the workspace's subscriber, not necessarily the member). The subject is recorded on the source (`credential_sources.verified_subject`,
with the account it was recorded under); a different subject for the same account counts as a
switch and takes the same path. The first sighting, and the first one after the source moves to
another account, is recorded rather than compared. A source that reveals no subject is not
watched. **OAuth and keychain** origins need none of this: the sidecar re-claims the account from
the login itself on every push, so a re-login simply moves the source to the new account.

### Who refreshes an OAuth login

A refresh exchanges the refresh token for a new one. Anthropic, ChatGPT and xAI
**rotate** it, so a refresh invalidates the copy still sitting in the CLI's own
credentials file and signs that CLI out (the new token never goes back to it).
Google does not rotate Gemini's.

- **Machine-owned** (a sidecar discovered it, or any sidecar bundle holds the same
  refresh secret) and the provider rotates: the server **never** refreshes it. The
  machine's CLI renews it and the sidecar re-pushes. The inventory shows
  `refreshed_by: "machine"`, there is no Refresh action, and the endpoints answer 409.
- **A CLI's own login file on the server host** (`~/.claude/.credentials.json`,
  `~/.codex/auth.json`, anything outside Runway's config dir): the CLI renews it, so
  the server never rotates it, whether or not a sidecar also pushes it. The inventory
  shows `refreshed_by: "machine"`; on a headless host with no CLI running its access
  token simply expires (the usual 3-day alert applies). Put the credential into
  Runway's own config dir or an env var to have the server refresh it.
- **Server-owned** (pasted key, env var, a login file inside Runway's config dir) and
  **Gemini** everywhere: the server refreshes it (`TokenAutoRefresher`, collectors,
  the Refresh action).
- A machine-renewed login that stays expired for more than 3 days raises the usual
  credential alert (an idle CLI is normal for a day or two, not for a week).
- While a machine's access token is expired and its CLI is idle, collection skips that
  source (no API call, so no false "revoked") and fails over to another source. The row
  reads `expired`, not `invalid`.

### Credential discovery rules (`registry.json`)

Where each provider's credentials live (env vars, files, keychain entries,
browser cookies) is declared once in `app/core/registry.json`. The server
evaluates the `env` and `file` rules itself; the sidecar, a single file with no
access to the server's registry, runs all of them from a **generated copy** of
that data between the `INJECTED REGISTRY` markers in `scripts/sidecar.py`.

- Edit `app/core/registry.json`, then run `make sidecar-registry`. Never edit
  the baked block by hand; `tests/unit/test_sidecar_registry_generated.py`
  fails on any difference.
- Differences that are intentional (a rule only the sidecar should run, a field
  the sidecar must not send) live in `scripts/sidecar_registry_overlay.json`,
  each with a reason. The server scans every `env`/`file` rule in
  `registry.json`, so a sidecar-only rule must go in the overlay, not there.
- Use `service_name` for keychain rules. Single-cookie providers (kimi_coding,
  ollama) map their cookie to `session_cookie`.

### Standard definitions

**`data_source` (origin of payload):**
- **`api`** — official API / OAuth endpoints.
- **`web`** — unofficial / cookie-based / scraped web endpoints.
- **`local`** — local log files / CLI statuslines / fast-path caches.

**`input_source` (origin of credentials):**
- **`config`** — entered via the Runway Dashboard UI (stored in DB).
- **`server`** — discovered by the local machine (ENV, local files, browser scraping).
- **`sidecar`** — discovered by a remote agent and pushed to the server.

Collection pipeline: server quota collection → merge with sidecar-ingested
enrichment → single card per
`(provider_id, account_id, window_type, variant, model_id)` tuple in
`latest_usage`. See [collection logic](collection_logic.md) for the full
bucket model, the per-collector strategy map, and `_merge_enrichment`
semantics.

## CI/CD

Workflows live in `.github/workflows/`.

- **`ci-cd.yml`** — on push/PR to `main`:
  - **`test-python`** — reusable `s3ntin3l8/.github` `ci-python.yml`:
    `lint` (ruff check + format) and `type-and-test` (mypy, detect-secrets
    against `.secrets.baseline`, pip-audit, pytest with Codecov). Coverage
    floor 70%.
  - **`test-frontend`** — reusable `ci-node.yml` against `webapp/` with
    strict checks: typecheck, Vite build, vitest coverage. Floor 85%.
  - **`installer-check`** — compiles the Windows NSIS installer with a
    placeholder exe on every PR, so a broken `.nsi` or missing installer
    asset fails here, not at release time.
  - **`build-docker`** — on push only: GHCR image via the shared
    `docker-publish.yml` with `push-edge: true` → the `:edge` tag.
  - Required checks on `main`: `test-python / lint`,
    `test-python / type-and-test`, `test-frontend / lint-and-test`, and
    CodeQL — enforced *strict* (branch must be up to date).
- **`release-please.yml`** — on push to `main`: opens/merges the release PR
  from Conventional Commits (see *Releases*). When it cuts a release it also
  runs `docker-publish.yml` with `push-release: true` (stable → `:latest` +
  version tags; beta → `:beta` + version tag, selected by `release-channel`)
  and calls `sidecar-build.yml` via `build-sidecar`/`publish-sidecar` —
  for stable and beta releases alike.
- **`sidecar-build.yml`** — reusable (`workflow_call`) sidecar matrix, the
  only place PyInstaller runs. Per release label (`vX.Y.Z`, `vX.Y.Z-beta.N`,
  or `edge`) it
  builds the macOS **`.dmg`** (ad-hoc signed `.app`, `create-dmg`) and
  Windows **NSIS `-setup.exe`** (`installer/windows/runway-sidecar.nsi`)
  installers, plus the `.zip`/`.tar.gz` self-update payloads for all four
  targets. Its `attest` job then writes `SHA256SUMS.txt` and Sigstore
  keyless `.sig`/`.cert` files. Every asset name comes from
  `scripts/sidecar_pkg/asset_names.py` (the updater's source of truth), and
  `tests/unit/test_sidecar_release_contract.py` pins it all together.
  `sidecar-release.yml` is a manual wrapper (`workflow_dispatch`; empty
  `tag` = build-only artifacts, handy for testing installers from a branch).
- **`sidecar-edge.yml`** — on push to `main` touching sidecar/installer
  code; calls `sidecar-build.yml` with `label: edge` (version stamped
  `<base>+edge.<sha>`) and publishes to the always-overwritten `edge`
  prerelease — the sidecar analog of the Docker `:edge` tag. A flaky
  non-Linux runner doesn't block the rest (`allow-partial`).
- **`hermes.yml`** — automated PR review. Auto-reviews exactly once per PR
  (`opened` when non-draft, or `ready_for_review`); on-demand re-reviews via
  a `@s3ntin3l8-hermes Review` comment. Deliberately no `synchronize`
  trigger — re-reviewing every push would be an unbounded review loop.
  Auto-review excludes `mullion/task-*`, dependabot, and release-please PRs.
- **`claude.yml`** — `@claude` mentions on issues/PRs route to the shared
  reusable Claude Code workflow.
- Also `codeql.yml`, `dependency-review.yml`, `cleanup-ghcr.yml`.
  Dependabot updates actions, pip, and npm weekly. `.secrets.baseline` is
  tracked in git — CI's detect-secrets gate needs it.

## Releases

Releases are managed by **Release Please**
(`.github/workflows/release-please.yml`):

- Uses Conventional Commits to determine version bumps: `feat:` → minor,
  `fix:` → patch, `feat!:` → major, `chore:`/`docs:`/`test:` → no release.
- On qualifying commits to `main`, Release Please opens a PR updating
  `CHANGELOG.md` and `package.json`.
- Merging that PR creates the GitHub Release and tag automatically — which is
  what triggers the release-channel image and sidecar builds above (stable →
  `:latest`, beta prerelease → `:beta`).
- To force a version jump (e.g. v1.0.0): tag manually, push the tag, create
  the GitHub Release by hand — Release Please picks up from there.

The prerelease settings in `release-please-config.json` are temporary for
the 3.0.0 beta cycle (`prerelease: true`, `prerelease-type: beta.1`). Before
merging a stable release, follow
[issue #408](https://github.com/s3ntin3l8/runway-ai-usage-tracker/issues/408)
to remove prerelease mode and promote 3.0.0; otherwise future Release Please
releases will also be beta versions.
