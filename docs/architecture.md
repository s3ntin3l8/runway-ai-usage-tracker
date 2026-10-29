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
| `usage_events` | Per-message events. Unique on `(provider_id, event_id)` — a re-push under a new account from the *same* sidecar re-attributes the row and moves its rollups (retag-safe); another sidecar can't steal it. `kind="message"` for billable activity, `kind="error"` for provider failures. Carries project-context enrichment — raw per-message `cwd`, indexed `project` (the session's root basename, derived in `EventIngestor` via `app/services/project_label.py` — the derivation is host-independent, so a Windows client's `D:\Projects\repo` labels as `repo` like the macOS client's `/home/u/repo`; worktree/tmp cwds collapse at the `.claude` boundary, and `scripts/consolidate_session_projects.py` consolidates per-session subfolder drift offline; backed by `ix_usage_events_project_ts`), `git_branch`, and `tool_names` — that powers the project/tool rankings. Also carries `source_app` — the **client app** that made the call (`opencode`, `claude_code`, `minimax_code`, ...), which is not the same thing as `provider_id`, the upstream provider the request was billed to: several client apps share one provider, and a canonical-provider retag (`scripts/sidecar_pkg/canonical_providers.py`) folds them onto one id. `source_app` survives that retag, so "which app spent the quota" stays answerable; it falls back to `provider_id` when a pushing sidecar reports none (an older sidecar), and pre-column rows are backfilled the same way. Carries `session_id` + `subagent_type` — the session a row belongs to and the subagent that produced it. A provider that emits one event per **request** (MiniMax Code) also carries `turn_id`, the provider's own turn identifier, so the Sessions view can count turns instead of requests; a provider that emits one event per message leaves it NULL and the turn count falls back to the event count. The session and per-project message counts follow that turn: the session-scoped ones count `COUNT(DISTINCT COALESCE(NULLIF(turn_id, ''), event_id))` — the session is constant inside their groups — and the per-project one prefixes the provider, because it groups across providers in the global mode and an event id is only unique per `(provider_id, event_id)`. A session row and its Agents card therefore speak the same unit. |
| `usage_period_rollup` | Pre-aggregated rollups (hour/day/month/year/lifetime × model × sidecar grain). Updated incrementally on each event ingest. |
| `usage_windows` | Closed-window archive — totals frozen at each authoritative `reset_at` boundary by `app/services/window_closer.py`. |
| `latest_usage` | Live gauge cards (`pct_used`, `limit_value`, `reset_at`) — what scrapers see. Merged via `merge_card_json` in `app/services/accumulator.py`. |
| `latest_usage_contributions` | Per-source last-good card payload behind a merged `latest_usage` row. One producer's complete report can't retire another producer's card — see [collection logic](collection_logic.md#current-card-reconciliation). |
| `quota_snapshots` | Append-only time-series of `pct_used`/`reset_at` observations. Written on every `upsert_latest_usage` call when `pct_used` is non-null. Backs the `%` history chart and the Theil-Sen forecast. |
| `provider_pricing` | Time-versioned per-(provider, model) prices used by `app/services/cost_calculator.py` so historical cost stays stable across price changes. |
| `provider_configs` | Per-provider user config — account labels, poll intervals, per-strategy enable toggles, and **three** Fernet-encrypted secret columns: `api_key_encrypted`, `session_cookie_encrypted`, `oai_sc_cookie_encrypted`. Unique on `(provider_id, account_id)`. |
| `credential_sources` | One row per credential *source* — a secret found in one place: a sidecar's file/env/cookie (`sidecar_id` set), a key pasted in Settings → Providers (`config:`), or an env var on the server host, or Runway's own `github_oauth.json` (`server:`, no origin; the server reads no other credential file, and a startup sweep drops `server:*:file:*` rows left by older builds). Holds the operator's `enabled`/`priority` failover order — a bundle whose access token has already expired is attempted after every live one, so priority orders usable credentials rather than dead ones; a source whose last attempt was rejected (`auth_failed`) rests between retries on the 15 min → 6 h doubling schedule (`next_retry_at`; once the rest is over it is simply tried in its normal place, reset by a success or by a changed secret at ingest), except that the last credential standing is always tried; `consecutive_failures`/`failing_since` track the current failure streak, and a streak of three or more `unavailable` attempts lasting over an hour makes the source read `failing` unless a sibling has collected since — the credential's non-secret expiry/token types, `health` (reported as `untried` until a first collection attempt is recorded — a registered but never-used credential must not read as working), and collection provenance (`last_attempt_at`, `last_success_at`, `last_error`) stamped by the failover loop, and the `verified_subject` last seen behind a cookie source (account-switch detection) — the "active source" for an account is the one with the latest success. Backs `GET /system/credentials`. Unique on `(provider_id, account_id, source_id)`, so an account rename has to carry these rows with it (`config_rekey` does; rows an older build stranded are removed by `orphan_credential_sources`). |
| `provider_account_labels` | Operator display-name override for a discovered `(provider, account)`. |
| `credential_tags` | Operator mapping from a host-side credential origin (`path:...`/`env:...`, never the value) to a server account, optionally scoped to one sidecar or deployment-wide. Unresolved origins sit in `pending_credential_tags` until tagged in the Fleet UI. Origins are fingerprinted per credential, so a CLI re-login re-keys them; when the base location is unchanged and exactly one account was ever tagged there, the manifest carries that binding to the new origin (`set_by=rotation`) instead of reopening a pending row and withholding the token (#474). `target_provider_id` (nullable) marks a tag whose account belongs to a related provider (Gemini usage assigned to an Antigravity account): the ingestor re-homes matching events to that provider, so they land on the `(antigravity, account)` series. |
| `pending_credential_tags` | Credential origins a sidecar has reported but no operator tag exists yet (`reason = token_withheld` marks one whose token stayed on the machine, so its provider cannot collect until an account is assigned — the inventory's `blocked_collection` and the Home banner; an older sidecar sends no reason and the server infers it for providers it cannot verify by source) — powers the "Untagged credentials" surface (`GET /fleet/credentials/tags/pending`). A `source_locked` / `source_unsupported` / `source_unreadable` reason marks a credential source the sidecar found but could not read (a browser cookie store the browser holds open, or one whose values are app-bound encrypted): there is no credential to map, so the row is kept out of the Untagged list and surfaces as the inventory's `blocked_sources` (the "Unavailable" tab, the Home banner, the provider page's empty state). The `source_store` / `source_detail` / `source_remedy` columns name the store, why it could not be read, and the fix — never a credential value, an account id or a filesystem path. |
| `pending_usage_events` | Collected events awaiting account assignment (evidence-backed or manual) — `GET /fleet/events/pending`, assign via `POST /fleet/events/pending/assign`. |
| `sidecar_registry` | Known sidecars with hostname, custom name, tags, last-seen, version, OS, recent log lines, and a `collection_enabled` pause flag. |
| `webhook_configs` | Discord/Slack threshold alerts: `provider_id`, `account_id` (NULL = all accounts), `threshold_pct`, `url`, `channel`, last-fired timestamp, `credential_alerts` (opt-in, default on, for the credential-health alerts below). |
| `webhook_credential_alerts` | Dedup/re-arm state for credential-health alerts (`app/services/credential_alerts.py`): one row per `(webhook_id, provider_id, account_id)` bad episode, with a `healthy_since` hysteresis timestamp so a single healthy Token Health observation doesn't immediately re-arm. |
| `system_config` | Single-row global config — browser preference, default poll interval, dashboard layout JSON, user timezone. |
| `audit_log` | Append-only record of admin mutations (sidecar pause/resume/delete/patch, etc.). Diagnostic, not legal-grade. |
| `sidecar_devices` | Enrolled sidecar identities — a random per-device HMAC secret, encrypted. Hostnames are labels, not identity. |
| `sidecar_request_receipts` | Claimed ingest signatures, so a replayed payload is rejected. |
| `sidecar_source_states` | Per-device, per-source consent state — enabled, generation, revocation. |
| `sidecar_source_contributions` | What each source contributed, so one source's withdrawal cannot retire another's card. |
| `sidecar_source_commands` | Durable, idempotent commands for one source, with ordered generations. |
| `sidecar_source_revision` | The capability revision a source is bound to. A newer catalog needs renewed consent. |
| `provider_account_merges` | Merge and unmerge journal for account identities. |
| `sidecar_pairing_codes` | One-time, short-lived (`PAIRING_CODE_TTL_SECONDS`) sidecar pairing codes, stored as SHA-256 only. Minted by admins (`POST /fleet/pairing-codes`, a `runway-sidecar://pair` deep link), redeemed once by a new sidecar (`POST /fleet/pair` → `api_url` + ingest key). See `app/services/pairing.py`, [SECURITY](SECURITY.md). |

**Ingest path:** Sidecar batches up to 1000 events per push to
`POST /api/v1/fleet/ingest` (HMAC-signed, rate-limited to 600/min per source
IP). Server runs `EventIngestor`, which deduplicates by
`(provider_id, event_id)` (re-attributing same-sidecar account changes),
resolves cost through `app/services/maintenance/event_cost.py::resolve_event_cost`
and updates the rollups. The quota-window boundary is closed by
`accumulator.upsert_latest_usage`, which triggers
`window_closer._maybe_close_previous_window` — not by the ingestor itself.

**Read / mutating paths:** the endpoint catalog lives in the
[API reference](api-reference.md). Non-obvious semantics worth knowing here:

- `/usage/fleet` adds `window_aggregations.longest` — per-model +
  per-sidecar splits aligned to the provider's longest active window
  (Claude weekly, Gemini daily, etc.), computed on demand from `usage_events`.
- `/usage/source-apps` ranks the **client apps** that produced events in a
  range — the same `model_remains`-style breakdown as `/usage/top-models`,
  but grouped by `source_app` instead of model, and filterable by
  `provider_id`/`account_id`/`sidecar_id`.
- `history/chart` takes `group=provider` for a cross-provider stack;
  `sessions/paginated` adds server-side sort + `project` filter; snapshot
  bucketing runs SQL-side via the `ix_quota_snapshots_series_ts` index.
  That index is **not** covering — it holds the key columns only, so a bucketed
  query still reads the table.
- Forecasts: `/usage/forecast` (Theil-Sen regression on `quota_snapshots`,
  anchor-at-now) and `/usage/cost-forecast` (MTD + 7-day burn to EOM).
- Mutating endpoints (`usage/reset`, `usage/collect`, fleet pause/resume/
  update, pairing codes, `/system/{cleanup,wake,force-collect,check-updates}`,
  and the webhook/provider-config/app-config/dashboard-layout CRUD) go through
  `require_admin_key`.
  **Not all of them write an `audit_log` row.** `usage/reset`, `usage/collect`,
  `/system/cleanup`, `/system/wake`, `/system/force-collect`, the webhook CRUD and
  `PUT /system/app-config` do not; the fleet and provider-config mutations do.

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
rollups/windows) lives in `app/services/maintenance/`.

The shape is **not** uniform. Most repairs expose a `plan_*`/`apply_*` pair,
but only **6 of the 18** modules use the shared chunked helpers
(`_chunked_sql.py`'s id-cursor `chunked_update`/`chunked_delete`), so a large
repair does not hold SQLite's writer lock for one giant transaction.
`account_merge` exposes `plan_merge_gauge_series` but the apply side is
`merge_gauge_series`, and `event_cost`, `legacy_providers`, `rollups` and
`windows` have **no** plan/apply pair at all.

`app/services/data_health/checks/` decides *when* to offer a repair. Each check
owns its own `detect()` **and its own repair** — 15 of the 18 import the shared
helpers from `maintenance/`, while `alert_channels`, `orphan_credential_tags`
and `pending_events` implement theirs in place. The checks are exposed through
`/api/v1/system/data-health/*` (`app/services/data_health/jobs.py`'s
single-flight job registry) and the Settings → Data health page.

Host-run scripts are **not** all thin wrappers. `scripts/recost_events.py` is;
`scripts/assign_default_events.py` and `scripts/merge_gemini_default_account.py`
are standalone. See [data-health](data-health.md) and
[maintenance scripts](maintenance-scripts.md).

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
(`TokenCache.get_pending_sources`). A pinned run reads only its own bundle (never the
server's env or Settings credential for another account), and a source that works but cannot name its account stays pending
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

**xAI** is the exception to "a rotated key is a new origin": it issues a new refresh token on
every refresh (keep-alive, the Grok/OpenCode CLI and the server all trigger one), so neither
token can identify the login. The sidecar fingerprints a stable claim of the access JWT instead
(`principal_id`, else `sub`; `jwt_stable_subject`), falling back to the refresh token and then
the access token only for a bearer that isn't a JWT. The server's `provider:xai#<fp>` hint for a
pasted bearer hashes the same claim. Logins keyed by older sidecars are cleaned up at ingest:
when an xAI origin first reports, the same machine's earlier `base#<fp>` source rows, their
cached bundles and their `rotation` tags are deleted (`retire_superseded_keyed_origins`;
operator tags are kept). Providers that can hold several distinct keys under one path are not
in that set, or they would delete each other every cycle (#523).

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

### Archived accounts and parked mappings

Archiving an account (`provider_configs.archived`) closes it: the row is forced to
`enabled=False`, the provider runs no collector while every one of its accounts is archived, and
the account disappears from the dashboard and the fleet view. The machine that found the
credential keeps reporting it, so the operator has to resolve the origin anyway — and a retired
account is a legal target. Such a mapping is **parked**: it resolves the origin (the credential
leaves the pending list, the "needs an account" card and the home banner, and the next manifest
does not re-open it) but stays inert — no hint ships to the sidecar, no incoming usage is stamped
on the archived account (neither a card nor a usage event), and the cached bundle is not moved into
the retired account's slot. Parking is *derived* from `provider_configs.archived`, never stored on
the tag, so restoring the account revives the mapping on the next `/fleet/config` with no
re-tagging. Whether a mapping is parked is answered in one place (`ParkedMapping` in
`app/services/credential_tags.py`), because a *redirect* tag names an account of its **target**
provider and the archived row has to be looked up there. See
[Parked mappings](api-reference.md#parked-mappings) for the API contract.

A retired account is left alone by the token auto-refresher too: it collects
nothing, its subscription has ended, so refreshing its credential can only
fail. The rule is keyed on the **account** — a retired account's credential is
skipped even when the provider still has another, live account — and on the
provider, whose every configured account being archived retires it as a whole
(a parked mapping leaves that credential unattributed, so it sits under no
account of its own). It is deliberately narrower than the collector's
`inactive_providers` rule, which also counts *disabled* rows: a disabled
account is a reversible state, and its credential should stay fresh for the day
the operator re-enables it. The credential inventory marks a retired account as
archived, so a parked credential does not read like an active one.

The archived rows are read in one place — `archived_account_pairs()` in
`app/services/credential_tags.py`, which canonicalises the account id (every
write path stores accounts canonically, a config row carries whatever the
operator typed) and is shared with `ParkedMapping`, so the inventory's marker
and the parked-mapping check can never disagree.

Archiving also **evicts the account's cached credential** — the token cache
entry and every source bundle of that account — so a retired account holds
nothing live: no credential to collect with, nothing to refresh, and no
expired row sitting in Token Health for an account the operator retired. The
Remove path has always done this, and so has the maintenance rekey when it
archives a source row; the archive write path was the one site that did not.
The stored credential (`provider_configs.api_key`, `session_cookie`) is *not*
touched — Archive is reversible, Remove is destructive — so restoring the
account makes it usable again at once, while a machine-reported credential
returns with that machine's next push.

The eviction narrows the window rather than closing it: the machine still has
the credential file and pushes it every cycle, and an enrolled machine's
credential also survives as a durable contribution that the cache reloads on
the next snapshot revision. Whether a credential parked on a retired account
should be cached at all is a separate design call.

A **retired account holds no credential at all** — the rule the eviction
intends. The machine keeps pushing, so the ingest used to file the secret
under the retired account again (and, for an enrolled machine, keep it at rest
as a durable contribution that the cache reloads on the next snapshot
revision). Now a credential resolved to a retired account is *recorded but not
stored*: the source row stays filed under the account — the operator still sees
that a machine reports a credential for the account they retired, marked
`archived` — while the secret is kept nowhere: not in the cache, not at rest.
Archiving the account also retires the durable contributions filed under it, so
the retired account holds nothing from that moment. Restoring the account
reverses it: the machine's next push files the credential under it again.

The two views therefore split: the **credential inventory** lists the retired
account's credential (it is the operator's record of what a machine reports),
while **Token Health** does not (it lists what is live — nothing is collectable
with a retired account's credential, and nothing to refresh).

## A merged account takes its credential with it

`POST /api/v1/system/provider-account-merge/apply` merges one account into
another: its usage history, quota cards, credential-source rows and operator
tags move to the destination, and the source id stops naming an account. The
machine, however, keeps resolving its credential to the source id and pushes it
every cycle — so the merge has to say where that id went, or the source account
comes back with a live credential and the destination is left holding a
credential-source row it cannot collect with.

The merge records the move (`provider_account_merges`: the source account, the
destination) and **moves the credential with the account**: the durable
contribution is re-filed under the destination and the cached bundle follows.
The ingest then resolves a claim of a merged-away id through that record, so
the credential is filed under the destination — and the source id's own
credential-source row is refreshed instead of duplicated. The chain is followed
(alice → bob → carol) and a merge whose source or destination was already
merged away is refused, so two records can never point at each other.

A merged-away account is therefore **not an account any more**: the merge
moved its history and its credential to the destination, so it holds nothing
and it disappears from every list the operator reads. The record is what says
where it went, and the read surfaces carry it:
- `GET /api/v1/system/provider-configs` lists the merges per provider under
  `merged_accounts` (the destination a chain ends at), and every account entry
  carries `merged_into` when that account was merged away — the leftover
  `provider_configs` row (archived, no credential) stays behind, and the flag
  is what tells a merged-away account from a merely archived one.
- `GET /api/v1/system/credentials` carries the same list: a merged-away
  account holds no credential, so it cannot appear among the inventory's
  accounts, and this is what tells a client "this account no longer exists"
  from "this account never existed".
- Settings → Providers lists them under "Merged accounts", where they are
  inert: they are not in the archived list, because the merge retired them —
  not an archive — and they have no Restore action.
- `GET /api/v1/usage/fleet` carries the merges beside its entries, and the Home
  page lists them under "Merged". A merged-away account is **not** a fleet entry:
  the merge moved its quota card to the destination, so it has no gauge, and
  inventing one would feed the risk rail and the stale-card banner a card that
  was never scraped. The provider page says where the account went instead of
  showing "no data" for a link the operator followed.

Restoring one would resurrect a phantom: the merge moved its history and its
credential to the destination, so the restored row would collect nothing while
the account showed up in the fleet again. The API refuses it (`409`, with the
destination in the message) rather than leaving the hole open to every client.

The merge also **journals what it moved** (`provider_account_merges.moved`): the
rows it re-filed, the credential sources, tags and contributions it carried over,
and what happened to the source's `provider_configs` row. The rows are recorded
**by id** — once a row sits under the destination it cannot be told apart from
the destination's own row, so an account id alone cannot say which rows the
merge carried over. Without that journal a merge cannot be reversed: the
destination's rows and the moved rows are one set. What the merge destroyed is
not journalled — the duplicates it deleted are gone, and so is the provider
credential it wiped. A merge performed before the journal existed has no
journal, and cannot be reversed.

The un-merge is the way back:
`POST /api/v1/system/provider-account-unmerge/apply` puts a journalled merge
back. The rows the merge moved go to the account they came from, the credential
sources and the durable contributions follow, the provider row is restored, and
the merge record is deleted — so the retired id names an account again, and the
guard that refused to restore it stops refusing. `preview` says what would go
back and what cannot: the duplicates the merge dropped and the stored
credential it wiped stay gone.

What the un-merge does **not** do is take the destination's *own* rows back:
what the destination had before the merge, and what it collected afterwards, is
the destination's and stays. The rows that go back are the ones the merge
carried over — which means the destination loses the quota card that came from
the source, and rebuilds it on the next collection. The one thing that is
dropped is a row that got in the way — an account recreated under the same id
while the merge was in force — and only after the operator confirms it: the
original history wins, because restoring it is the point. The confirmation is
for a *count*, not a yes: the request carries the number the preview showed,
and the un-merge refuses when the rows in the way changed since, because the
operator confirmed a different set of rows. A chain is unwound from the end
(alice → bob → carol: undo bob → carol first), since undoing the middle link
would rewrite the chain twice.

A parked mapping is *not shipped* to the sidecar — but a sidecar that cached
the hint before the account was archived was still applying it, because the
manifest's `resolved` map was merged into the sidecar's cache and a merge
never deletes a key. The manifest response is now **complete per provider**
(an empty map for a provider it resolves no origin for), and the sidecar
replaces what it holds for the providers it answered for, so a retraction
lands on the next manifest instead of the next `/fleet/config`. The sidecar
names the providers whose hints it holds in `hint_providers`, so the server
also answers for a provider whose tag was deleted — that provider has no tag
row left to make it a candidate.

The sidecar's cache therefore equals what it holds: a provider the server
answered for with an empty map is dropped from the cache, and so from
`hint_providers` — it is not asked about again on every manifest. The list
is bounded (256 ids, like `observations`): every id becomes one term in the
query that collects the hints and one bucket in the response. The sidecar
truncates the list to that cap rather than exceeding it — the server
rejects an oversized list, and a rejected manifest takes the cycle's
`completed_providers` with it, so the host's stale pending rows would stop
being pruned.

### Who refreshes an OAuth login

A refresh exchanges the refresh token for a new one. Anthropic, ChatGPT, xAI and
MiniMax Code **rotate** it, so a refresh invalidates the copy still sitting in the
CLI's own credentials file and signs that CLI out (the new token never goes back
to it). Google does not rotate Gemini's.

- **Machine-owned** (a sidecar discovered it, or any sidecar bundle holds the same
  refresh secret) and the provider rotates: the server **never** refreshes it. The
  machine's CLI renews it and the sidecar re-pushes. The inventory shows
  `refreshed_by: "machine"`, there is no Refresh action, and the endpoints answer 409.
- **Server-owned** (pasted key, env var, Runway's own `github_oauth.json`) and
  **Gemini** everywhere (Google does not rotate its refresh token, so the server may
  refresh a sidecar-pushed Gemini bundle into the token cache): the server refreshes it
  (`TokenAutoRefresher`, collectors, the Refresh action).
- The server reads no CLI login file from its own host (`~/.claude/.credentials.json`,
  `~/.codex/auth.json`, ...); only sidecars do. A rotating provider's CLI login therefore
  always arrives as a machine-owned bundle, and the sidecar keep-alive (xAI, Claude Code, Codex, agy) renews it.
- **MiniMax Code** is a machine-owned login with no keep-alive: the desktop app owns
  `~/.minimax/auth/<env>/<region>/mcode-public/auth.json` and renews the token in place
  (the stored `generation` counter increments) whenever it runs. Runway never refreshes
  it — there is no MiniMax refresh endpoint in `_REFRESH_ENDPOINTS`, so the server
  cannot rotate the app's refresh token even by accident. `minimax` is listed in
  `ROTATING_REFRESH_PROVIDERS` so the machine-ownership is explicit in the inventory
  (`refreshed_by: "machine"`) rather than an accident of the provider having no
  refresh endpoint.
- A machine-renewed login that stays expired for more than 3 days raises the usual
  credential alert (an idle CLI is normal for a day or two, not for a week).
- While a machine's access token is expired and its CLI is idle, collection skips that
  source (no API call, so no false "revoked") and fails over to another source. The row
  reads `expired`, not `invalid`.

### Credential discovery rules (`registry.json`)

Where each provider's credentials live (env vars, files, keychain entries,
browser cookies) is declared once in `app/core/registry.json`. The server
evaluates the `env` rules itself, and only the `file` rules under Runway's own config dir
(`{{CONFIG_DIR:runway}}`, i.e. `github_oauth.json`: `CredentialProvider._server_may_read`,
applied to the raw paths before any `~` expansion or glob, so the server never touches the
host home). Every other `file` rule is sidecar-only; the sidecar, a single file with no
access to the server's registry, runs all of them from a **generated copy** of
that data between the `INJECTED REGISTRY` markers in `scripts/sidecar.py`.

- Edit `app/core/registry.json`, then run `make sidecar-registry`. Never edit
  the baked block by hand; `tests/unit/test_sidecar_registry_generated.py`
  fails on any difference.
- Differences that are intentional (a rule only the sidecar should run, a field
  the sidecar must not send) live in `scripts/sidecar_registry_overlay.json`,
  each with a reason. The server scans every `env` rule in `registry.json` (and the
  Runway-owned file rules), so a rule the server must not even list goes in the overlay.
- Use `service_name` for keychain rules. Single-cookie providers (kimi_coding,
  ollama) map their cookie to `session_cookie`.
- **Mapping keys** (`mapping` in a `file` rule) are dotted paths into the parsed
  file. A key may itself contain dots (gh's `github.com`), so the longest
  matching prefix is tried first and shorter ones are retried. `a|b` tries
  alternatives left to right (first truthy wins). A `*` in a prefix is a glob over
  that level's keys: it matches any characters, dots and colons included, within
  one key (`github.com:*.oauth_token`); matches are tried in sorted order and the
  first truthy value wins. Never end a wildcard path on an object: the value is
  shipped as the credential. The sidecar (`GenericCollector.get_nested`) and the
  server (`CredentialProvider._resolve_mapping_value`) implement this twice;
  `tests/unit/test_mapping_resolver_parity.py` runs one table against both.
- A `file` rule's `format` is `json` (default), `yaml`, or `jsonc` (JSON with
  `//` and `/* */` comments, read BOM-tolerant). `keyless_ok: true` silences the
  "file read but no credential keys found" log for a file that normally holds no
  token.

### Standard definitions

**`data_source` (origin of payload):**
- **`api`** — official API / OAuth endpoints.
- **`web`** — unofficial / cookie-based / scraped web endpoints.
- **`local`** — local log files / CLI statuslines / fast-path caches.
- **`error`** — the card reports a failure instead of a quota.
- **`lsp`** — a legacy value the ingest rule still recognises as the one
  exception to the sidecar-enrichment-only rule, but which **no shipped
  collector emits**.

The canonical list, and what each value means, is in
[statistics.md](statistics.md#data-source-data_source).

**`input_source` (origin of credentials):**
- **`config`** — entered via the Runway Dashboard UI (stored in DB).
- **`server`** — found by the server process itself: environment variables, or Runway's own `github_oauth.json`. Never a CLI login file, local detection or browser scraping (those run in the sidecar).
- **`sidecar`** — discovered by a remote agent and pushed to the server.

Collection pipeline: server quota collection → merge with sidecar-ingested
enrichment → single card per
`(provider_id, account_id, window_type, variant, model_id)` tuple in
`latest_usage`. See [collection logic](collection_logic.md) for the full
bucket model, the per-collector strategy map, and `_merge_enrichment`
semantics.

## Service modules

The service layer is larger than the table map above suggests. These are the
subsystems a reader would otherwise have to find by grepping.

### Read views — `app/services/queries/`

The read layer was split out of what used to be a single 1979-line
`event_query.py`. Each module is one read view:

| Module | View |
|---|---|
| `anomaly.py` | anomalous usage readings |
| `cumulative.py` | cumulative per-model / per-project series |
| `events.py` | the raw event feed |
| `forecast.py` | forecast series |
| `global_stats.py` | cross-provider totals |
| `heatmap.py` | the activity heatmap |
| `history.py` | the historical chart |
| `sessions.py` | session rows |
| `snapshots.py` | quota snapshot series |
| `source_apps.py` | which client app spent the quota |
| `top_hosts.py`, `top_models.py`, `top_projects.py`, `top_sessions.py`, `top_tools.py` | the rankings |

### Repairs and health

| Module | Purpose |
|---|---|
| `app/services/maintenance/` | The repair implementations. 18 modules; see the note on plan/apply above. |
| `app/services/data_health/` | 18 read-only checks, a dependency-ordered registry (`blocked_by` gating) and a single-flight job registry. |
| `app/services/source_controls.py` | The durable device/source consent model — per-device consent state, revocation and encrypted credential contributions. It is the authorisation backbone of the sidecar credential path, and the model behind the six `sidecar_source_*` tables. |
| `app/services/account_merges.py` | Merge and unmerge execution, and the merge journal. |

### Collection

| Module | Purpose |
|---|---|
| `app/services/smart_collector.py` | The per-provider cache, the 429 backoff, and the staleness ceiling that decides when a card is stale. |
| `app/services/collector_manager.py` | The collector registry and the per-provider TTL table. |
| `app/services/poller.py` | The background poll loop, and `reconcile_latest_usage_snapshot` on a completed collection. |
| `app/services/token_auto_refresher.py` | Background OAuth auto-refresh (`TOKEN_AUTO_REFRESH_THRESHOLD_SECONDS`). |
| `app/services/refresh_policy.py` | "Who may refresh this login" — the rules shared by the token cache and the refresher. |
| `app/services/token_cache.py` | The server-side token cache. 30-minute TTL by default. |
| `app/services/token_health.py` | Expiry parsing and the status classification the credential alerts consume. |
| `app/services/token_refresher.py` | The explicit refresh path. |

### Events and identity

| Module | Purpose |
|---|---|
| `app/services/event_ingestor.py` | The ingest path: dedup by `(provider_id, event_id)`, cost resolution, rollup updates. |
| `app/services/event_identity_migration.py` | `migrate_to_provider_event_identity` — retagging rows onto the provider-scoped event identity. |
| `app/services/identity_lookup.py` | Resolving the account behind a credential. |
| `app/services/accumulator.py` | The live card view: merge, ownership, staleness. |
| `app/services/period_rollups.py` | The rollup aggregation. |
| `app/services/project_label.py` | The host-independent project derivation. |
| `app/services/account_canonicalization.py`, `account_identity.py` | Canonical provider ids and account identity. |
| `app/services/forecast.py` | Theil-Sen median regression over `quota_snapshots`. |
| `app/services/cost_calculator.py` | The pricing rules behind `provider_pricing`. |

### Alerting and fleet

| Module | Purpose |
|---|---|
| `app/services/webhooks.py` | The Discord/Slack threshold alerts. |
| `app/services/credential_alerts.py` | Credential-health alerts, and the re-arm hysteresis (`healthy_since`). |
| `app/services/blocked_collection.py`, `blocked_sources.py` | Which unmapped credential is silently stopping a provider's collection. |
| `app/services/fleet_registry.py` | The fleet registry. |
| `app/services/sidecar_devices.py`, `sidecar_version_checker.py`, `sidecar_downloads.py` | Device identity, the GitHub Releases version check, and the download plumbing. |
| `app/services/release_metadata.py` | The release metadata the version check consumes. |
| `app/services/probe_mode.py`, `source_probe.py`, `source_outcome.py` | The operator-triggered probes and their outcomes. |

### Claude verification cluster

`app/services/claude_cloud_probe.py`, `claude_oauth_usage.py`,
`claude_oauth_verification.py` and `claude_verification.py` implement the
operator-triggered server diagnostic for the Claude cloud accounting path.
See [evidence](evidence/README.md) for the acceptance records.

`app/services/claude_cloud_support.py` holds the pieces the Claude cloud
diagnostics share: the single-flight worker gate, the read-only source-authority
read and the strict JSON/UUID helpers. The cloud probe, the chat fixture
(`claude_chat_fixture.py`), the verification and list-boundary services, the
collector and the verification endpoint all take the **same** gate, so one
diagnostic in flight makes the others report `busy` instead of starting their
own work. These are public names on purpose: no module in the cluster reads
another module's private member.

`app/services/claude_cloud_collector.py` is the one server-side API collector that writes
`usage_events` itself (all other collectors return cards): for Claude Code cloud-runner
sessions only, behind `CLAUDE_CLOUD_COLLECTOR_ENABLED` (default off). It subclasses the
probe's read-only client, so it inherits the exact-source, no-refresh and account checks, and
keeps its per-session state in `claude_cloud_session_usage` and
`claude_cloud_collector_state`. See [Claude cloud-runner sessions](collection_logic.md#claude-cloud-runner-sessions).

`app/services/claude_cloud_events.py` is the pure half of the replacement collector described in
[the cloud event contract](claude-cloud-event-contract.md): no network and no database, it turns a
cloud session's ordered history records into `UsageEventPush` objects (one message event per assistant
message, plus a per-turn, per-model remainder so each turn's totals equal what the result record
reports). The history sync that feeds it is a separate change.

## CI/CD and releases

The workflows, the gates they enforce and the release choreography live in
[ci-cd.md](ci-cd.md).

- [ci-cd.md — CI/CD](ci-cd.md)
- [ci-cd.md — Releases](ci-cd.md#releases)
