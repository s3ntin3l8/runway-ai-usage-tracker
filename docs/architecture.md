# Architecture

Runway is a local-first monitor for AI provider quotas with SQLite-backed
history. This is the reference behind [`AGENTS.md`](../AGENTS.md): topologies,
environments, the event-sourced data model, credentials and accounts, the
collectors, and the service modules.

## Topologies

- **Two topologies:** local (server and sidecar on one host) and multi-host or
  Docker (a server and one or more remote sidecars). The server never performs
  local detection — LSP probes, browser cookies, keychains and IDE or file
  introspection all live in the sidecar.
- **Docker rule:** the server container has no desktop UI or keychains;
  credentials arrive through environment variables or sidecar payloads.
- **Cookie collectors** (Claude, ChatGPT, Ollama, Kimi Coding, OpenCode) need
  browser cookies, so the sidecar extracts them and ships them to the server.

## Environments (dev vs prod)

Dev and prod run side by side with **separate data dirs**: SQLite is
single-writer, so two processes must never write one `runway.db`.

- **Dev:** `make dev-all` (hot reload). The database and sidecar config live in
  the gitignored `./data` (the Makefile defaults `RUNWAY_CONFIG_DIR` there).
- **Prod:** Docker. The database lives in the container's config dir
  (`/home/runway/.config/runway`), persisted by a host bind-mount. Attach to an
  existing reverse proxy with a gitignored `docker-compose.override.yml`
  (template: `docker-compose.override.example.yml`); `docker-compose.yml` stays
  a generic blueprint and `docker-compose.traefik.yml` bundles Traefik. A
  non-localhost bind trips the [startup gates](#multi-host-startup-gates).

**Updates.** The SPA is baked into the server image, so UI and server changes
ship by pulling a new image: `:edge` (every push to `main`), then `:latest` /
`:vX.Y.Z` on a stable release or `:beta` / `:vX.Y.Z-beta.N` on a prerelease. The
sidecar edge channel updates only the collector binary, never the UI. Schema
upgrades are forward-safe (no Alembic): `init_db` runs `create_all` plus
idempotent `ALTER TABLE ADD COLUMN`, so bumping the image never breaks an
existing database.

## Data model

Runway is **event-sourced**. `usage_events` is authoritative — one row per
assistant message — and everything else is a derived view. The models live in
`app/models/db_usage.py`, `db_credentials.py`, `db_sidecar.py`, `db_system.py`
and `db_claude_cloud.py`; `app/models/db.py` re-exports them.

### Usage

| Table | Role |
|---|---|
| `usage_events` | Per-message events, unique on `(provider_id, event_id)`. `kind` is `message` or `error`. Carries `cwd`, an indexed `project`, `git_branch`, `tool_names`, `session_id`, `subagent_type`, the client app `source_app` and, for per-request providers, `turn_id`. See [project labels](collection_logic.md#project-labels). |
| `usage_period_rollup` | Hour / day / month / year / lifetime rollups by model and sidecar, updated incrementally on ingest. |
| `usage_windows` | Closed-window archive: totals frozen at each authoritative `reset_at` by `app/services/window_closer.py`. |
| `latest_usage` | Live gauge cards (`pct_used`, `limit_value`, `reset_at`), merged by `merge_card_json` in `app/services/accumulator.py`. |
| `latest_usage_contributions` | Per-source last-good payload behind a merged card, so one producer cannot retire another's. See [reconciliation](collection_logic.md#current-card-reconciliation). |
| `quota_snapshots` | Append-only `pct_used` / `reset_at` series; backs the history chart and the Theil-Sen forecast. |
| `provider_pricing` | Time-versioned per-model prices used by `cost_calculator.py`, so historical cost is stable across price changes. A row may carry a time-of-day schedule (`rate_schedule`, `rate_schedule_multiplier`; windows in `pricing_schedule.py`, UTC): inside the window every rate is multiplied, outside it the base rates apply. DeepSeek direct (2x peak) and Ollama `deepseek-v4*` (0.5x off-peak) use it; CN holidays are not modelled, so DeepSeek peak is slightly overcounted on those days. The Python calculator and `maintenance/sql_cost.py` apply the same window; re-cost history with the Data Health `stale_priced_models` fix. |
| `session_names` | Display name per `(provider_id, session_id, kind)`; `agent` for a user-set name, `generated` for a tool's. Free text, never logged. |
| `pending_usage_events` | Events held back until an account is assigned. |

An event's `source_app` is the client app that made the call (`opencode`,
`claude_code`, `minimax_code`, …) as distinct from the upstream `provider_id`;
it survives a canonical-provider retag and falls back to `provider_id` for older
sidecars. Session and project message counts count distinct turns
(`COALESCE(NULLIF(turn_id, ''), event_id)`), so a session row and its Agents card
share a unit.

### Credentials and accounts

| Table | Role |
|---|---|
| `provider_configs` | Per-account settings: label, poll intervals, strategy toggles, archive flag, and three Fernet-encrypted secrets (`api_key_encrypted`, `session_cookie_encrypted`, `oai_sc_cookie_encrypted`). Unique on `(provider_id, account_id)`. |
| `credential_sources` | One row per credential *source*: a secret in one place — a sidecar's file, env var or cookie, a key pasted in Settings (`config:`), a server env var, or the server's own `github_oauth.json` (`server:`). Holds the operator's `enabled` / `priority` failover order, health and collection provenance, and the `verified_subject` behind a cookie. Backs `GET /system/credentials`. |
| `provider_account_labels` | Operator display-name override for a discovered account. |
| `credential_tags` | Operator mapping from a host-side credential origin to a server account, deployment-wide or per sidecar. `target_provider_id` redirects a tag to a related provider's account (Gemini usage onto an Antigravity account). |
| `pending_credential_tags` | Origins a sidecar reported that no tag resolves yet, with the reason (`token_withheld`, or a `source_*` reason for a store the sidecar could not read). |
| `provider_account_merges` | The merge and unmerge journal. |

### Fleet and sidecars

| Table | Role |
|---|---|
| `sidecar_registry` | Known sidecars: hostname, name, tags, last seen, version, OS, recent log lines, `collection_enabled`. |
| `sidecar_devices` | Enrolled identities with an encrypted random per-device HMAC secret. Hostnames are labels, not identity. |
| `sidecar_pairing_codes` | One-time, short-lived pairing codes, stored as SHA-256 only. See [SECURITY](SECURITY.md). |
| `sidecar_request_receipts` | Claimed ingest signatures, so a replayed payload is rejected. |
| `sidecar_source_states`, `sidecar_source_contributions`, `sidecar_source_commands`, `sidecar_source_revision` | Per-device, per-source consent: state and generation, what each source contributed, ordered idempotent commands, and the capability revision a source is bound to. |

### System and alerting

| Table | Role |
|---|---|
| `system_config` | Single-row global config: poll interval, dashboard layout, timezone, the encrypted `SESSION_SECRET`. |
| `webhook_configs` | Discord / Slack threshold alerts, with an opt-in `credential_alerts` flag. |
| `webhook_credential_alerts` | Dedup and re-arm state for credential alerts (a `healthy_since` hysteresis). |
| `audit_log` | Append-only record of admin mutations. Diagnostic, not legal-grade. |

### Claude cloud

| Table | Role |
|---|---|
| `claude_cloud_session_checkpoint` | One row per cloud session: cursor and reducer state for the collector. |
| `claude_cloud_collector_state` | The collector's per-account health record. |
| `claude_cloud_verification`, `claude_cloud_verification_archives` | The operator verification job and its archived terminal checkpoints. |
| `claude_cloud_session_usage` | Left over from the first cloud collector; no longer written. |

### Ingest path

A sidecar batches up to 1000 events per push to `POST /api/v1/fleet/ingest`
(HMAC-signed, 600 per minute per IP). `EventIngestor` deduplicates by
`(provider_id, event_id)` (a same-sidecar account change re-attributes the row
and moves its rollups; another sidecar cannot take it), resolves cost through
`app/services/maintenance/event_cost.py::resolve_event_cost`, and updates the
rollups. A quota-window boundary is closed by `accumulator.upsert_latest_usage`,
which triggers `window_closer._maybe_close_previous_window`, not by the ingestor.

### Reads and mutations

The endpoint catalog is the [API reference](api-reference.md). Worth knowing:

- `/usage/fleet` adds `window_aggregations.longest`: per-model and per-sidecar
  splits aligned to the provider's longest window, computed on demand from
  `usage_events`.
- `/usage/source-apps` ranks client apps in a range, like `/usage/top-models`
  but grouped by `source_app`.
- `history/chart` takes `group=provider` for a cross-provider stack; snapshot
  bucketing runs in SQL via `ix_quota_snapshots_series_ts` (not a covering index).
- Forecasts: `/usage/forecast` (Theil-Sen over `quota_snapshots`) and
  `/usage/cost-forecast` (month-to-date plus a 7-day burn).
- Mutating endpoints go through `require_admin_key`. Not all write an
  `audit_log` row: `usage/reset`, `usage/collect`, the `/system/` cleanup, wake
  and force-collect, webhook CRUD and `PUT /system/app-config` do not; fleet and
  provider-config mutations do.

### Admin auth

The dashboard logs in through `POST /api/v1/auth/session` (validates
`ADMIN_API_KEY`, sets an HttpOnly `SameSite=Strict` cookie, 10 per minute);
`POST /auth/logout` clears it and `POST /auth/revoke-all` rotates
`SESSION_SECRET`, invalidating every session. `SESSION_SECRET` is generated,
stored Fernet-encrypted in `system_config`, and separate from
`DB_ENCRYPTION_KEY`. Scripts can use the `X-Admin-Key` header. A blank
`ADMIN_API_KEY` or `DB_ENCRYPTION_KEY` counts as unset, and a malformed
`DB_ENCRYPTION_KEY` fails fast at startup instead of running plaintext. See
[SECURITY](SECURITY.md).

### Multi-host startup gates

When `APP_HOST != 127.0.0.1` the server refuses to start without
`DB_ENCRYPTION_KEY`, `TLS_TERMINATED=1`, an explicit `CORS_ORIGINS` list, and
either `ADMIN_API_KEY` or `TRUSTED_PROXY_IPS`. Sidecar payloads carry tokens
(HMAC is not confidentiality), and the "no key configured" bypass opens only on a
loopback bind, so at least one real admin gate must exist off-loopback. See
[SECURITY](SECURITY.md).

### Account identity

`app/services/account_identity.py::resolve_account_id` picks, in order: an
email-shaped label, the canonical id, a PBKDF2 hash of the credential (64 hex),
then `"default"`. The sidecar hostname is never part of a unique constraint on
`(provider_id, account_id)`. Every write path stores ids through
`canonical_account_id` (emails lowercased, opaque ids verbatim); the sidecar
mirrors it in `scripts/sidecar_pkg/identity.py`, and
`app/services/account_canonicalization.py` repairs legacy rows at startup.

### Repairs and Data health

All data-repair logic (retagging events, rekeying a `provider_configs` row,
merging gauge series, recosting, rebuilding rollups and windows) lives in
`app/services/maintenance/`. Most repairs expose a `plan_*` / `apply_*` pair, and
those that touch many rows use the chunked helpers in `chunked_sql.py` so they
never hold SQLite's writer lock in one giant transaction; a few
(`event_cost`, `legacy_providers`, `rollups`, `windows`) have no such pair.
`app/services/data_health/checks/` decides *when* to offer a repair: each check
owns its `detect()` and its repair, usually through the shared helpers. They are
served by `/api/v1/system/data-health/*` (a single-flight job registry in
`data_health/jobs.py`) and Settings → Data health. Host-run scripts are not all
thin wrappers. See [data health](data-health.md) and
[offline repair scripts](data-health.md#offline-repair-scripts).

## Credentials and accounts

### Unidentified credentials

A sidecar that cannot tell whose credential it found (no email in the id token,
no configured identity) reports it as `identity_pending` and sends the secret
only for providers the server can verify: antigravity, anthropic, chatgpt,
gemini, github and opencode. The server runs a dedicated
`<provider>:default:identity-pending` collector pinned to each pending source in
turn; when its API call reveals the account, `CollectorManager` binds the
identity to that exact source and moves it. Gemini and Antigravity ask Google's
userinfo endpoint; Claude asks `claude.ai/api/account` for a cookie and
`api.anthropic.com/api/oauth/profile` for an OAuth token (the holder only; a
token without `user:profile` is refused and stays "Needs mapping", and an
organization contact, who can be an admin, is never adopted); ChatGPT reads the
email from the usage endpoint. A pinned run reads only its own bundle, never the
server's environment or Settings credential for another account. Anything
unverifiable stays "Needs mapping" and is retried with exponential backoff
(`pending_credential_tags.next_verify_at`: 15 minutes doubling to 6 hours, five
sources per provider per cycle, oldest due first); a new secret for the same
source resets it. A ChatGPT token whose JWT already carries the email is
identified by the sidecar and never needs verifying.

### Credential origins and operator tags

A credential's *origin* (`env:ZAI_API_KEY`, `path:/…/auth.json`,
`cookie:kimi_coding/session`) is what an operator tag is keyed on.

- **Static keys** (opencode siblings, and the env-key providers kimi_api, kimi_k2
  and zai): the sidecar suffixes a fingerprint of the value (`env:ZAI_API_KEY#<fp>`),
  so a rotated key is a new origin and a tag cannot silently follow it. A tag on
  the plain origin still applies to the key-scoped one, and the superseded
  plain-origin row is retired when its successor first reports.
- **xAI** issues a new refresh token on every refresh, so neither token can
  identify the login. The sidecar fingerprints a stable claim of the access JWT
  instead (`principal_id`, else `sub`; `jwt_stable_subject`), falling back to the
  refresh then access token for a non-JWT bearer. The server's `provider:xai#<fp>`
  hint for a pasted bearer hashes the same claim. When an xAI origin first
  reports, the same machine's earlier `base#<fp>` rows, cached bundles and
  `rotation` tags are deleted (`retire_superseded_keyed_origins`; operator tags
  stay). Providers that can hold several keys under one path are excluded, or they
  would delete each other every cycle.
- **Cookies, keychain entries and OAuth bundles** stay unfingerprinted: their
  secret changes with every login or refresh, so a fingerprint would orphan the
  tag each time. A tag on a cookie or keychain origin therefore cannot be
  deployment-wide (`422`); on a path or env origin it can, and the Credentials
  view warns that such a tag follows an account switch. When the base location is
  unchanged after a CLI re-login and exactly one account was ever tagged there,
  the manifest carries the binding to the new origin (`set_by=rotation`).

**Account-switch detection.** Collectors whose provider reports the account's
email (Claude via `/api/account`, ChatGPT via the usage endpoint, Ollama via the
page header) set `verified_identity`. If it resolves to a different account than
a machine-scoped **cookie** tag names, `CollectorManager` drops the tag, takes the
source out of the old account (nothing from that poll is published there), and the
sidecar's next push files it as pending for the verifier. Providers that never
reveal an email may set `verified_subject`, a stable opaque id (Kimi: the `sub` of
the `kimi-auth` JWT; opencode is not watched, because its `subscriberUserId`
names the workspace's subscriber); it is recorded on the source and a different
subject for the same account counts as a switch. The first sighting is recorded,
not compared. Non-email accounts, deployment-wide tags and sources with no email
or subject are never second-guessed. OAuth and keychain origins need none of this:
the sidecar re-claims the account from the login on every push.

### Who refreshes an OAuth login

A refresh exchanges the refresh token for a new one. Anthropic, ChatGPT, xAI and
MiniMax Code **rotate** it, so a refresh invalidates the copy in the CLI's own
credentials file and signs that CLI out. Google does not rotate Gemini's.

- **Machine-owned** (a sidecar discovered it, or any sidecar bundle holds the same
  refresh secret) and the provider rotates: the server **never** refreshes it. The
  machine's CLI renews it and the sidecar re-pushes. The inventory shows
  `refreshed_by: "machine"`, there is no Refresh action, and the endpoints answer
  `409`.
- **Server-owned** (pasted key, env var, the server's own `github_oauth.json`) and
  **Gemini** everywhere: the server refreshes it (`TokenAutoRefresher`,
  collectors, the Refresh action).
- The server reads no CLI login file from its own host, so a rotating provider's
  CLI login always arrives as a machine-owned bundle, and the sidecar
  [keep-alive](sidecar.md) (xAI, Claude Code, Codex, MiniMax Code, agy) renews it.
- **MiniMax Code** is machine-owned and the sidecar's keep-alive renews it: the
  desktop app owns `~/.minimax/auth/<env>/<region>/mcode-public/auth.json` and
  renews the token in place (the stored `generation` counter increments) whenever it
  runs, and the sidecar renews it when the app does not — 90 minutes before the
  twelve-hour access token lapses, writing the result back into the app's own store
  (`scripts/sidecar_pkg/minimax_code_renewer.py`). The write updates only the token
  and the expiry: the app's two files carry matching `generation` counters and
  answer "login error" when they disagree, so the sidecar never bumps one of them.
  There is no MiniMax entry in `REFRESH_ENDPOINTS`
  (`app/services/token_refresher.py`), so the server cannot rotate it by accident.
  `minimax` is in `ROTATING_REFRESH_PROVIDERS` so the ownership is explicit in the
  inventory.
- A machine-renewed login that stays expired for more than 3 days raises the
  credential alert. While a machine's access token is expired and its CLI is idle,
  collection skips that source (no API call, so no false "revoked") and fails
  over; the row reads `expired`, not `invalid`.

### Credential discovery rules (`registry.json`)

Where each provider's credentials live (env vars, files, keychain entries,
cookies) is declared once in `app/core/registry.json`. The server evaluates the
`env` rules and only the `file` rules under its own config dir
(`{{CONFIG_DIR:runway}}`, i.e. `github_oauth.json`;
`CredentialProvider._server_may_read` checks the raw path before any `~`
expansion or glob), so it never touches the host home. Every other `file` rule is
sidecar-only; the sidecar runs them from a **generated copy** of the registry
between the `INJECTED REGISTRY` markers in `scripts/sidecar.py`.

- Edit `app/core/registry.json`, then run `make sidecar-registry`. Never edit the
  baked block; `tests/unit/test_sidecar_registry_generated.py` fails on any
  difference.
- Intentional differences (a rule only the sidecar runs, a field it must not send)
  live in `scripts/sidecar_registry_overlay.json`, each with a reason.
- Use `service_name` for keychain rules; single-cookie providers (kimi_coding,
  ollama) map their cookie to `session_cookie`.
- **Mapping keys** are dotted paths into the parsed file. A key may itself contain
  dots (`github.com`), so the longest matching prefix is tried first. `a|b` tries
  alternatives left to right (first truthy wins). `*` is a glob over one level's
  keys, dots and colons included (`github.com:*.oauth_token`), matches tried in
  sorted order. Never end a wildcard path on an object: the value is shipped as
  the credential. The sidecar (`GenericCollector.get_nested`) and server
  (`CredentialProvider._resolve_mapping_value`) implement this twice, and
  `tests/unit/test_mapping_resolver_parity.py` runs one table against both.
- A `file` rule's `format` is `json` (default), `yaml` or `jsonc`.
  `keyless_ok: true` silences the "no credential keys found" log for a file that
  normally holds none.

## Account lifecycle

Accounts can be **archived**, **merged away** or **parked**; the endpoint
contract is in the [API reference](api-reference.md#account-lifecycle).

**Archived.** Archiving (`provider_configs.archived`) closes an account: the row
is forced `enabled=False`, a provider whose accounts are all archived runs no
collector, and the account leaves the dashboard and fleet view. It **evicts the
account's cached credential** (token cache entry and every source bundle) but
leaves the stored `api_key` / `session_cookie`, so Archive is reversible and
Remove is destructive. A retired account holds no credential at all: the machine
keeps pushing, but a credential resolved to a retired account is *recorded but not
stored* — the source row stays, marked `archived`, while the secret is kept
nowhere, not in the cache and not at rest. Archiving also retires the durable
contributions filed under it, and restoring reverses it on the machine's next
push. The credential inventory lists the retired account's credential; the
legacy token-health view does not.

The token auto-refresher also leaves a retired account alone (it collects
nothing, so a refresh can only fail). The rule is keyed on the account, and on the
provider when every configured account is archived. It is deliberately narrower
than the collector's `inactive_providers` rule, which also counts *disabled* rows:
a disabled account is reversible and its credential should stay fresh.
`archived_account_pairs()` in `app/services/credential_tags.py` reads the archived
rows in one place (canonicalising ids), shared with `ParkedMapping`.

**Parked mappings.** The machine that found an archived account's credential keeps
reporting it, so the operator must resolve the origin anyway, and a retired
account is a legal target. Such a mapping is *parked*: it resolves the origin
(the credential leaves the pending list, the "needs an account" card and the home
banner) but stays inert — no hint ships to the sidecar, no card or event is
stamped on the archived account, and the cached bundle does not move. Parking is
derived from `provider_configs.archived`, never stored on the tag, so restoring
the account revives the mapping. `ParkedMapping` answers it in one place, looking
a *redirect* tag up under its **target** provider. A parked mapping is also not
shipped to the sidecar: the manifest's `resolved` map is **complete per
provider** (an empty map for a provider with nothing resolved), the sidecar
replaces what it holds for the providers answered, and `hint_providers` (at most
256 ids, truncated by the sidecar) lets the server answer for a provider whose
tag was deleted. See [Parked mappings](api-reference.md#parked-mappings).

**Merged away.** `POST /api/v1/system/provider-account-merge/apply` moves one
account's history, cards, credential sources, tags and credential to another and
retires the source id. The merge is recorded in `provider_account_merges`, and the
ingest resolves a claim of a merged-away id through that record (following a chain
alice → bob → carol; a merge whose source or destination was already merged away
is refused), so a machine that keeps pushing the old id files its credential under
the destination instead of resurrecting the source. A merged-away account is not
an account any more, and the read surfaces say where it went:

- `provider-configs` lists `merged_accounts` per provider and marks each merged
  entry `merged_into`; `system/credentials` carries the same list;
- Settings → Providers lists them under "Merged accounts", inert and without a
  Restore action;
- `usage/fleet` carries the merges beside its entries and Home lists them under
  "Merged". A merged-away account is not a fleet entry — inventing a gauge would
  feed the risk rail a card that was never scraped.

Restoring one would resurrect a phantom, so the API refuses it (`409`, naming the
destination).

**Un-merge.** The merge journals what it moved (`provider_account_merges.moved`):
the re-filed rows **by id** — once a row sits under the destination it cannot be
told apart from the destination's own — plus the credential sources, tags and
contributions and the fate of the source's `provider_configs` row. A merge made
before the journal cannot be reversed, and what a merge destroyed (deduplicated
rows, the wiped provider credential) is not journalled. `provider-account-unmerge`
puts the moved rows back and deletes the record. It does **not** take the
destination's own rows back — what it had, and what it collected afterwards, stays
(so it loses the quota card that came from the source and rebuilds it on the next
collection). The one row it drops is one that got in the way (an account recreated
under the same id during the merge), only after the operator confirms the *count*
the preview showed. A chain is unwound from the end.

## Collectors

Server collectors only do `api` and `web`; anything `local` (CLI, statusline or
log scraping) runs in the sidecar. Each collector declares an ordered strategy
list and the first that succeeds wins; the sidecar is a separate producer whose
contribution is reconciled with the server's. The strategy model, the
`data_source` / `input_source` labels, and reconciliation are in
[collection logic](collection_logic.md); each provider's own behaviour is in
[the collector docs](collectors/README.md).

## Service modules

### Read views — `app/services/queries/`

Each module is one read view.

| Module | View |
|---|---|
| `anomaly.py`, `forecast.py` | anomalous readings; forecast series |
| `chart_bars.py`, `chart_percent.py`, `history.py`, `snapshots.py` | the history charts and quota snapshot series |
| `cumulative.py`, `global_stats.py`, `heatmap.py` | cumulative series, cross-provider totals, the activity heatmap |
| `events.py`, `sessions.py`, `windows.py` | the event feed, session rows, closed windows |
| `source_apps.py` | which client app spent the quota |
| `top_hosts.py`, `top_models.py`, `top_projects.py`, `top_sessions.py`, `top_tools.py` | the rankings |
| `shared.py` | helpers shared by the views |

### Repairs and health

| Module | Purpose |
|---|---|
| `app/services/maintenance/` | The repair implementations. |
| `app/services/data_health/` | The read-only checks, a dependency-ordered registry (`blocked_by` gating) and the single-flight job registry. |
| `app/services/source_controls.py` | The durable device and source consent model: per-device consent state, revocation and encrypted credential contributions. The authorisation backbone of the sidecar credential path. |
| `app/services/account_merges.py` | Merge and unmerge execution and the journal. |

### Collection

| Module | Purpose |
|---|---|
| `app/services/smart_collector.py` | The per-provider cache, the 429 backoff, and the staleness ceiling. |
| `app/services/collector_manager.py` | The collector registry and the per-provider TTL table. |
| `app/services/poller.py` | The poll loop, and `reconcile_latest_usage_snapshot` on a completed collection. |
| `app/services/token_auto_refresher.py`, `refresh_policy.py`, `token_refresher.py` | Background and explicit OAuth refresh, and "who may refresh this login". |
| `app/services/token_cache.py`, `token_health.py` | The server-side token cache (30-minute TTL by default); expiry parsing and status classification for credential alerts. |

### Events and identity

| Module | Purpose |
|---|---|
| `app/services/event_ingestor.py` | The ingest path: dedup, cost resolution, rollup updates. |
| `app/services/event_identity_migration.py` | Retagging rows onto the provider-scoped event identity. |
| `app/services/identity_lookup.py` | Resolving the account behind a credential. |
| `app/services/accumulator.py` | The live card view: merge, ownership, staleness. |
| `app/services/period_rollups.py` | Rollup aggregation. |
| `app/services/project_label.py` | The host-independent project derivation. |
| `app/services/account_canonicalization.py`, `account_identity.py` | Canonical provider ids and account identity. |
| `app/services/forecast.py`, `cost_calculator.py` | Theil-Sen regression over `quota_snapshots`; the pricing rules behind `provider_pricing`. |

### Alerting and fleet

| Module | Purpose |
|---|---|
| `app/services/webhooks.py`, `credential_alerts.py` | Discord / Slack threshold alerts; credential-health alerts and re-arm hysteresis. |
| `app/services/blocked_collection.py`, `blocked_sources.py` | Which unmapped credential is silently stopping a provider's collection. |
| `app/services/fleet_registry.py`, `sidecar_devices.py` | The fleet registry and device identity. |
| `app/services/sidecar_version_checker.py`, `sidecar_downloads.py`, `release_metadata.py` | The GitHub Releases version check and download plumbing. |
| `app/services/probe_mode.py`, `source_probe.py`, `source_outcome.py` | The operator-triggered probes and their outcomes. |

### Claude cloud

`app/services/claude_cloud_probe.py`, `claude_oauth_usage.py`,
`claude_oauth_verification.py` and `claude_verification.py` implement the
operator-triggered diagnostics for the Claude cloud path (see the
[API reference](api-reference.md#claude-cloud-diagnostics-operator-only) and the
[evidence](evidence/README.md)). `claude_cloud_support.py` holds what they share:
a single-flight worker gate that the probe, the chat fixture, the verification and
list-boundary services, the collector and the endpoint all take (so one
diagnostic in flight makes the others report `busy`), the read-only
source-authority read, and the strict JSON and UUID helpers.

`claude_cloud_collector.py` is the one server-side API collector that writes
`usage_events` itself (every other collector returns cards), for Claude Code
cloud-runner sessions only, behind `CLAUDE_CLOUD_COLLECTOR_ENABLED`. It subclasses
the probe's read-only client, so it inherits the exact-source, no-refresh and
account checks, and keeps its progress in `claude_cloud_session_checkpoint` and
its health in `claude_cloud_collector_state`. `claude_cloud_events.py` is its
pure half, specified in [the cloud event contract](claude-cloud-event-contract.md):
no network and no database, it turns ordered history records into
`UsageEventPush` objects. See
[Claude cloud-runner sessions](collection_logic.md#claude-cloud-runner-sessions).

## CI/CD and releases

See [ci-cd.md](ci-cd.md) for the workflows and gates, and
[its releases section](ci-cd.md#releases) for how a release is cut.
