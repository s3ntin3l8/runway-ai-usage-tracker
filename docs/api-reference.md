# API Reference

All server API routes are under `/api/v1/`. The one exception is the sidecar's
own loopback settings API, which is served by the sidecar process on the
machine that holds the credentials — it is not a server route and has no
`/api/v1/` prefix. See
[Local sidecar Keychain source settings](#local-sidecar-keychain-source-settings).

## Auth

Every route is protected by one of the schemes below. The `Auth` column of
every table names the scheme a route accepts.

| Scheme | Applies to | Carried as |
|--------|------------|-----------|
| `Admin` | every route marked `Admin` | `X-Admin-Key` header, the `runway_session` cookie, or a trusted proxy's identity header |
| `HMAC` | the sidecar-facing fleet routes | `X-Signature` + `X-Timestamp` |
| `Device HMAC` | `POST /api/v1/fleet/source-sync` | the same headers, signed with an enrolled device key and carrying `X-Runway-Device-Id` |
| `Optional HMAC` | `GET /api/v1/fleet/config` | the same headers; an unsigned caller gets the redacted view |
| `Public` | routes marked `Public` | nothing |

### Admin authentication

`require_admin_key` accepts any one of:

- `X-Admin-Key: <ADMIN_API_KEY>` — the header path, for scripts and API clients;
- the `runway_session` cookie minted by `POST /api/v1/auth/session`;
- an identity header asserted by a forward-auth proxy (`X-Forwarded-User` by
  default, `Remote-User` as a fallback) — honoured only when the request's
  source IP is in `TRUSTED_PROXY_IPS` and the asserted user passes
  `FORWARD_AUTH_ALLOWED_USERS` / `FORWARD_AUTH_ALLOWED_GROUPS`;
- localhost trust: a loopback client against a loopback-bound server is an
  administrator without any credential;
- an instance with no `ADMIN_API_KEY` configured and a loopback bind, which is
  open by design.

Anything else is `403` ("Invalid or missing admin key"). See
[SECURITY.md](SECURITY.md) and [forward-auth.md](forward-auth.md).

### Fleet ingest authentication

`POST /api/v1/fleet/ingest`, `POST /api/v1/fleet/credentials/manifest` and
`POST /api/v1/fleet/source-sync` are authenticated with an HMAC-SHA256
signature over the timestamp and the raw body, sent as `X-Signature` and
`X-Timestamp`. `GET /api/v1/fleet/config` accepts the same scheme optionally:
an unsigned caller gets the redacted public view, a signed caller gets the
account data for its own `sidecar_id`. A device-enrolled sidecar signs with its
own device key and adds `X-Runway-Device-Id`; a shared ingest key can neither
authenticate a device header nor claim a reserved `device-<32 lowercase hex>`
registry identity.

### Forward auth / SSO

Runway ships no built-in OIDC client. A forward-auth proxy in front of it does
the real authentication and asserts the identity in a header; Runway trusts
that header only from an IP in `TRUSTED_PROXY_IPS`, which is the actual
security boundary. See [forward-auth.md](forward-auth.md) for the Authentik
walkthrough.

### Which routes need administrator authentication

Every `/api/v1/usage/*` route, the fleet sidecar inventory and detail routes,
the system state and provider configuration routes, and the GitHub OAuth
status routes require administrator authentication in network mode. Localhost
trust remains available with valid local `Host` and browser `Origin` headers.

The public bootstrap endpoints are `/api/v1/system/settings`,
`/api/v1/system/health` and `/api/v1/system/sidecar-downloads`; fleet
`/api/v1/fleet/config` retains its redacted public view and signed sidecar
behavior.

### Admin session (browser)

The dashboard authenticates via an HttpOnly, `SameSite=Strict` session cookie.
Scripts and
API clients can keep sending the `X-Admin-Key` header instead. See [SECURITY.md](SECURITY.md)
for cookie flags, `SESSION_SECRET`, and session lifetime.

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `POST` | `/api/v1/auth/session` | Public | Validate `ADMIN_API_KEY` and set the session cookie (`remember` extends lifetime). Rate-limited 10/min. When no admin key is configured the instance is open |
| `POST` | `/api/v1/auth/logout` | Public | Clear this browser's session cookie (other sessions unaffected) |
| `POST` | `/api/v1/auth/revoke-all` | Admin | Rotate `SESSION_SECRET` — invalidates every session everywhere (6/min) |

### GitHub Device Flow

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/auth/github/init` | Admin | Begin GitHub OAuth Device Flow — starts a device-flow login that ends with this server storing a GitHub credential |
| `POST` | `/api/v1/auth/github/poll` | Admin | Poll for completion — on success this persists a GitHub access token to disk |
| `GET` | `/api/v1/auth/github/status` | Admin | Current GitHub auth state |
| `POST` | `/api/v1/auth/github/logout` | Admin | Discard stored GitHub token |

See [Sidecar Documentation](sidecar.md) for ingest authentication and payload format.

#### Session names

`/usage/sessions` and `/usage/sessions/paginated` return `name` per session
(`string | null`): the `agent` name (set by the user or a launcher) if one is
stored, else the `generated` one (produced by the tool), else `null`. The
lookup covers only the returned page's session ids and is keyed by the row's
`provider_id`.

The ingest payload takes an optional `session_names` list, at most 2000
entries per request (surplus and malformed entries are dropped; they never fail
the events in the same payload):

```json
{ "session_names": [
  { "provider_id": "anthropic", "session_id": "<id>", "source_app": "claude_code",
    "name": "<text>", "kind": "agent" }
] }
```

`provider_id` is the provider the sidecar stamps on that session's events;
`kind` is `agent` or `generated`. The server cleans the name (control
characters removed, whitespace collapsed, 200 characters at most), drops empty
names, keeps the latest value per `(provider_id, session_id, kind)` and writes
nothing when neither the name nor its `source_app` changed. The ingest response adds `names_upserted` (rows
written; `0` when none were sent, all were unchanged, or every entry was
dropped as invalid or empty after cleaning; the sidecar counts any `0` as sent,
so dropped names are not retried) and `names_error`
(`true` only when the name write raised after its retries, which never fails
event ingestion; the sidecar then keeps the names unsent and retries them next
cycle, like `events_error` for events). Names are free text: the server never logs
them. An older server ignores the field and an older sidecar omits it.

## Usage & history

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/usage/limits` | Admin | All current quota cards, read from the `LatestUsage` table (an empty table falls back to an in-memory collection) |
| `GET` | `/api/v1/usage/sources` | Admin | Distinct sidecar IDs (`sidecar_ids`) and client apps (`source_apps`) with message usage events |
| `GET` | `/api/v1/usage/fleet` | Admin | Fleet HUD: critical gauge + secondary limits per (provider, account), with `window_aggregations.longest` per-model/per-sidecar splits, plus `blocked_sources` (credential sources a machine could not read) and `merged_accounts` (the accounts that were merged away, and the account each of them became — a merged-away account holds no credential and has no quota card, so it is not an entry of its own) |
| `GET` | `/api/v1/usage/cumulative` | Admin | Usage totals across sidecars (lifetime/year/month/day); `since`/`until` narrow to a window and optional `sidecar_id` scopes to one source |
| `GET` | `/api/v1/usage/forecast` | Admin | Theil-Sen projection to reset; `include_series=true` returns drill-down points |
| `GET` | `/api/v1/usage/history/windows` | Admin | Paginated closed quota windows |
| `GET` | `/api/v1/usage/history/snapshots` | Admin | Paginated snapshot rows with per-series delta |
| `GET` | `/api/v1/usage/history/chart` | Admin | Time-series data for percent/tokens/cost visualisations; optional `sidecar_id` scopes token/cost bars, while `group=provider` collapses bars to one segment per provider |
| `GET` | `/api/v1/usage/history/window-detail` | Admin | Fill-up series + by-model breakdown for one window |
| `GET` | `/api/v1/usage/history/deltas` | Admin | Event-sourced consumption deltas; optional `sidecar_id` scopes to one source |
| `GET` | `/api/v1/usage/events` | Admin | Recent event tail for (provider, account) |
| `GET` | `/api/v1/usage/events/range` | Admin | Earliest/latest event timestamps for (provider, account), optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/window-history` | Admin | Closed-window history with per-model & per-sidecar splits; optional `series_model_id` and `series_variant` scope to a quota series and retain legacy unscoped rows; for scoped requests, an omitted/empty variant means `default` |
| `GET` | `/api/v1/usage/heatmap` | Admin | 7×24 hour-of-day activity grid; optional `sidecar_id` and `exclude_cache` filters |
| `GET` | `/api/v1/usage/sessions` | Admin | Top-N sessions (`sort_by=tokens` or `recent`), optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/sessions/paginated` | Admin | Paginated session browser with server-side sort (`sort_by=recent\|tokens\|duration\|messages\|cost`, `sort_dir=asc\|desc`) and optional event-level filters that intersect: `project` (`__none__` selects events with no project label), `sidecar_id`, `model` (normalized model id), `branch` (git branch), `subagents=exclude` (drops subagent events from the totals and breakdowns). Totals, breakdowns and `total` all describe only the matching events |
| `GET` | `/api/v1/usage/session-models` | Admin | Top models by event count for `provider_id` + `account_id` in the `since`/`until` window (optional `sidecar_id`, `project` to follow the active project filter, `q` substring search, `limit` 1-200, default 50); feeds the Sessions Model filter. Response `{"models": [...]}` |
| `GET` | `/api/v1/usage/session-branches` | Admin | Same for git branches; response `{"branches": [...]}` |
| `GET` | `/api/v1/usage/projects` | Admin | Distinct project names seen in events, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-projects` | Admin | Top projects ranked by `metric=tokens\|cost\|sessions`, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-tools` | Admin | Top tools by invocation volume, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-models` | Admin | Cross-provider Top Models ranked by `metric=tokens\|cost`, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-sessions` | Admin | Cross-provider Top Sessions ranked by `metric=tokens\|cost`, optionally scoped by `provider_id`/`sidecar_id` — the sessions behind a usage spike, across every provider |
| `GET` | `/api/v1/usage/top-hosts` | Admin | Cross-provider Top Hosts ranked by `metric=tokens\|cost`, optionally scoped by `provider_id`/`sidecar_id` — the machines that pushed the events |
| `GET` | `/api/v1/usage/source-apps` | Admin | Client apps that produced events, ranked by `metric=tokens\|cost`; several apps can share one provider, so this is what says which app spent the quota. Optionally scoped by `provider_id`/`account_id`/`sidecar_id` |
| `GET` | `/api/v1/usage/global-stats` | Admin | Global cross-provider snapshot; optional `sidecar_id` and `exclude_cache` filters |
| `GET` | `/api/v1/usage/cost-forecast` | Admin | MTD cost + 7-day burn extrapolated to EOM; optional `exclude_cache` |
| `GET` | `/api/v1/usage/anomalies` | Admin | Z-score spike detection vs. historical mean; omitted/empty `sidecar_id` selects the all-source rollup, while a non-empty ID selects that source |
| `GET` | `/api/v1/usage/archived-providers` | Admin | Lifetime stats for archived providers, kept out of the main fleet view |
| `POST` | `/api/v1/usage/reset/{provider}` | Admin | Clear terminal failure state for a provider |
| `POST` | `/api/v1/usage/collect/{provider}` | Admin | Force immediate re-collection for one provider |

For `/usage/cumulative`, `by_sidecar` normally contains per-source totals alongside
the all-source total. When `sidecar_id` is supplied, the top-level bucket is
scoped to that source and `by_sidecar` contains that source as a single-key map.
Lifetime token totals and `cache_hit_ratio` in `/usage/global-stats` retain the
cache-inclusive components; `exclude_cache` applies to session averages and peak
token metrics, while the UI adjusts the lifetime token/cost tiles from their
component fields. Cache-specific metrics remain cache-inclusive by definition.

`/usage/top-sessions` and `/usage/top-hosts` rank across every provider and
account unless `provider_id` says otherwise, and resolve their window the way
the other rankings do: an explicit `since`/`until` pair (upper bound
exclusive) wins, else the last `days` — `days=1` is the rolling last 24
hours — else the current month on the user's local calendar. A session row
carries the session's `name` (the stored display name, `null` when none; a session spanning providers takes the name of the alphabetically first provider that has one), `project`, `cwd` and pushing host (`sidecar_id`), its
first and last event, and the distinct `providers`/`models` behind it; a host
row carries the number of distinct `sessions` that ran on it. Events with no
`session_id` — or, for the host ranking, no pushing host — are not
attributable and are left out of the ranking.

Every ranking response (`/usage/top-models`, `/usage/top-projects`,
`/usage/top-sessions`, `/usage/top-hosts`, `/usage/top-tools`) also carries
`total` — the number of ranked groups before `limit` truncated the result —
and the `limit` that was applied, so a capped ranking can still say how many
groups it left out.

## Fleet / Ingestion

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `POST` | `/api/v1/fleet/ingest` | HMAC | Push metrics + events from a sidecar (HMAC-SHA256 signed, 600/min/IP) |
| `GET` | `/api/v1/fleet/sidecars` | Admin | List all registered sidecars; an enrolled device also carries `source_sync_state` / `source_sync_last_at` (see [Source sync state](#source-sync-state)) |
| `GET` | `/api/v1/fleet/sidecars/{sidecar_id}` | Admin | Single sidecar details |
| `PATCH` | `/api/v1/fleet/sidecars/{sidecar_id}` | Admin | Update custom name or tags |
| `DELETE` | `/api/v1/fleet/sidecars/{sidecar_id}` | Admin | Remove sidecar from registry |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/pause` | Admin | Pause collection on a sidecar |
| `PUT` | `/api/v1/fleet/sidecars/{sidecar_id}/settings` | Admin | Per-sidecar overrides, body `{"auto_update": bool\|null, "update_channel": "stable"\|"beta"\|"edge"\|null, "log_level": "DEBUG"\|"INFO"\|"WARNING"\|"ERROR"\|null}` plus optional `claude_config_dirs` / `codex_home` ([login directories](#fleet-login-directory-additions)). A key left out stays; `null` clears the override so the sidecar follows the fleet again; an unknown channel or level is `422`. Delivered on the sidecar's next ingest response. The list adds the `*_desired` overrides, the `effective_*` values and what the sidecar reports (`null` = older sidecar) |
| `PUT` | `/api/v1/fleet/sidecars/{sidecar_id}/keep-alive` | Admin | Turn keep-alive on or off remotely, body `{"enabled": true\|false\|null}` (`null` defers to the sidecar's own flag and `sidecar_keep_alive_default` in app-config). An optional `provider` (`antigravity`, `anthropic`, `chatgpt`, `xai`) targets one login: `enabled` then sets or (`null`) clears that login's override, and the response lists `keep_alive_desired_providers`. Applied by the sidecar without a restart |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/adopt` | Admin | Fold a legacy shared-key sidecar into the enrolled `device-<hex>` that replaced it after a re-pair. Body `{"legacy_sidecar_id", "dry_run"}` (`dry_run` defaults to `true`). Moves its events, unassigned events, machine-scoped tags and per-source `enabled`/`priority` choices (and, where the device has none, its name, tags and settings), rebuilds the derived rollups and windows, then removes the legacy row. Returns the moved counts; a dry run adds `from` / `into` / `pushes_after`. `404` unknown sidecar; `409` unless the legacy row is among the device's `adoptable_legacy` candidates (listed on the sidecar list, with the inverse `replaced_by`) |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/resume` | Admin | Resume collection on a sidecar |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/update` | Admin | Queue a one-shot self-update for the sidecar; applies on its next heartbeat |
| `GET` | `/api/v1/fleet/config` | Optional HMAC | Active collection config the sidecar should poll; unsigned/non-loopback callers get only the `enabled`/`strategies` view, no account data |
| `POST` | `/api/v1/fleet/pairing-codes` | Admin | Mint a one-time pairing code + `runway-sidecar://pair` deep link for a new sidecar |
| `POST` | `/api/v1/fleet/pair` | Public | Unauthenticated one-time-code redeem: exchanges a pairing code for `api_url` + ingest key (10/min/IP). A forward-auth proxy must let it through unauthenticated ([forward-auth.md](forward-auth.md#5-production-hardening-split-the-traefik-router)) |
| `POST` | `/api/v1/fleet/credentials/manifest` | HMAC | The sidecar reports credential origins found this cycle (an entry may carry `reason: "token_withheld"` when the token stayed on the machine; Claude OAuth entries may carry a claimed `account_id`). `hint_providers` names the providers whose hints the sidecar holds (at most 256 ids). The response's `resolved` map is **complete per provider**, with an empty map for a provider that resolves nothing, so the sidecar replaces rather than merges what it holds and a retracted hint disappears on the next manifest |
| `GET` | `/api/v1/fleet/credentials/tags` | Admin | List every resolved credential tag (deployment-wide and per-sidecar). Each carries `origin_label`, `origin_app` (the owning tool, e.g. `Codex CLI`), `origin_path` (home collapsed to `~`) and `origin_detail` (for a cookie, the browser and profile; `null` for an Anthropic cookie origin), so the UI never parses raw origin strings, plus `last_matched_at` (`null` = never) and `stale` (the `stale_credential_rules` check would list it) |
| `POST` | `/api/v1/fleet/credentials/tags` | Admin | Resolve a pending credential origin to an account. `scope: "deployment"` is refused with `422` for `cookie:` and `keychain:` origins (one machine only); `path:` and `env:` origins can be deployment-wide. A tag on a plain origin also covers its key-scoped form (`env:ZAI_API_KEY` covers `env:ZAI_API_KEY#<fp>`). Returns `{"status": "ok", "target_archived": bool}`; `true` means the mapping is [parked](#parked-mappings) |
| `DELETE` | `/api/v1/fleet/credentials/tags` | Admin | Remove one resolved tag in a given scope |
| `GET` | `/api/v1/fleet/credentials/tags/pending` | Admin | Credential origins awaiting resolution, with a claimed Claude account id when available, safe quota previews and staleness. Each item carries `origin_label`, `origin_app` and `origin_detail` (see the tags list above) |
| `GET`/`POST` | `/api/v1/fleet/credentials/{source_id}/claude-organizations` | Admin | Inspect eligible organizations or select one using an exact pending sidecar source (no credential values returned) |
| `GET` | `/api/v1/fleet/events/pending` | Admin | Paginated queue of events held back under an unresolved `default` identity |
| `GET` | `/api/v1/fleet/events/pending/sessions` | Admin | Paginated pending events grouped by provider, host, and session; supports `sidecar_id`, `provider_id`, and session/model `search` filters |
| `POST` | `/api/v1/fleet/events/pending/assign` | Admin | Assign up to 1000 pending events to one account, promoting them into `usage_events` and creating provider/host mappings. Archived accounts are accepted, disabled ones are not. An optional `target_provider_id` re-homes the events to a related provider's account (allow-list: `gemini` → `antigravity`; anything else is `422`) and later events from the host follow. `mappings` lists every group as `{provider_id, target_provider_id, account_id, assigned}` |
| `POST` | `/api/v1/fleet/events/pending/assign-batch` | Admin | Assign up to 10000 pending events across provider-specific accounts (each assignment takes the same optional `target_provider_id`), validating the whole batch before promotion. One provider on one host cannot map to two targets in a batch. The Fleet UI splits larger selections into requests of at most 10000 ids |

The grouped pending-events response reports `total_events` across the full queue and `matching_events` for the current filters; `total_groups` is the filtered group count used for pagination.

## System: status and configuration

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/health` | Public | Liveness check |
| `GET` | `/api/v1/system/status` | Admin | Collector cache states and error counts (`collectors`), plus `claude_cloud`: `{enabled, accounts: [{account_id, last_outcome, last_cycle_at, last_events, consecutive_failures, backlog, healthy}]}` (`healthy` is false after 3 failed cycles in a row or no cycle for 3 intervals) |
| `GET` | `/api/v1/system/settings` | Public | Non-sensitive runtime configuration: `version`, `build_sha` (the commit the running build came from; `null` if none), `latest_version` / `update_available`, `app_host` / `app_port`, `encryption_enabled`, `admin_auth_required`, `auth_methods`, `user_context`, `is_authenticated` |
| `GET` | `/api/v1/system/audit-log` | Admin | Append-only admin-mutation trail |
| `GET` | `/api/v1/system/sidecar-downloads` | Public | Cached GitHub release assets for the Fleet page's *Add sidecar* card; `?channel=stable\|beta\|edge` |
| `GET` | `/api/v1/system/app-config` | Admin | Global app config: `sidecar_update_channel`, `sidecar_auto_update`, `sidecar_keep_alive_default` (the fleet-wide keep-alive default), `claude_cloud_collector_enabled` (`true` / `false`, or `null` so `CLAUDE_CLOUD_COLLECTOR_ENABLED` decides; a `PUT` applies without a restart and deletes no events) with the read-only `claude_cloud_collector_effective`, and `worktree_patterns` (`string[]`; `PUT` replaces the list, `[]` clears it, an invalid entry is `400`) with the read-only `worktree_builtin_patterns` |
| `PUT` | `/api/v1/system/app-config` | Admin | Update the global app config; omitted fields in a `PUT` keep their stored value |
| `GET` | `/api/v1/system/dashboard-layout` | Admin | Persisted dashboard layout |
| `PUT` | `/api/v1/system/dashboard-layout` | Admin | Update the persisted dashboard layout |
| `POST` | `/api/v1/system/force-collect` | Admin | Trigger immediate collection cycle, fan out to sidecars |
| `POST` | `/api/v1/system/cleanup` | Admin | Prune stale records and inactive sidecars |
| `POST` | `/api/v1/system/wake` | Admin | Reset dormancy, restore normal polling |
| `POST` | `/api/v1/system/check-updates` | Admin | Force an immediate GitHub release poll for server + sidecars, refreshing the update-banner cache |

## System: credentials

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/token-health` | Admin | **Legacy** — the webapp no longer reads it; use `GET /system/credentials` for new clients. Health of every credential: `status` is `valid` / `expiring` / `expired` / `invalid` (provider rejected it) / `failing` (collections kept failing for over an hour) / `stale` (sidecar stopped reporting it) / `unknown`, by the same rules as the inventory. A retired account's credential is not listed. Credential alerts still build on it because webhook scoping uses its synthetic account ids (`server`, `config:<account>`). Values are never returned |
| `GET` | `/api/v1/system/credentials` | Admin | Credential inventory: provider → account → source. The response never contains secret values. The fields of a source, an account and the inventory itself are listed in [Credential inventory fields](#credential-inventory-fields) |
| `POST` | `/api/v1/system/credentials/migration-notice/dismiss` | Admin | Dismiss the credential-file migration notice for this installation; returns `{"status": "ok"}`. The dismissal is idempotent, persists for the installation, and is not reset by later removals |
| `POST` | `/api/v1/system/credentials/{provider}/{account_id}/{source_id}/refresh` | Admin | Refresh one source's OAuth token (replaces the removed per-account `/system/token-health/refresh` and `DELETE /system/token-health/...`; `409` for a login a machine's CLI owns) and write it back into that source's bundle |
| `DELETE` | `/api/v1/system/credentials/{provider}/{account_id}/{source_id}` | Admin | Forget one machine-reported credential; it returns on the machine's next report if still present. `409` for config / server-env credentials |
| `POST` | `/api/v1/system/credentials/{provider}/{account_id}/remove` | Admin | Forget several machine-reported credentials of one account at once (10/min). Body `{"source_ids": [...]}` (1–50). Returns `{removed, skipped}`; config/server-managed or unknown sources are reported under `skipped` (`managed_elsewhere`/`not_found`) instead of failing the batch. A removed login's token is kept in the account cache while another remaining source still holds it |

### Credential inventory fields

`GET /api/v1/system/credentials` answers with `providers` (provider → account
→ source), `machines`, `unmapped_count`, `rule_count`,
`pending_usage_events`, `blocked_collection`, `blocked_sources`,
`merged_accounts` and `credential_file_migration_notice`.

One source carries:

| Field | Meaning |
|-------|---------|
| `source_id` | The source's identity |
| `provider_id`, `account_id` | The account the source belongs to |
| `origin_kind` | `machine` / `config` / `server` |
| `origin_type` | What the credential is: `file`/`env`/`cookie`/`keychain`, or `sidecar` for an origin the server doesn't recognise |
| `origin_app`, `origin_path` | The tool that owns a machine's credential file and its `~`-collapsed path |
| `origin_detail` | For a cookie, the browser and profile it came from (e.g. `Chrome · Profile 1`); `null` when the machine could not name the jar (null for an Anthropic cookie origin, whose private browser profile is never exposed) |
| `login_hint` | How to re-authenticate that tool |
| `machine_stale` | The machine stopped checking in, by Fleet's rule |
| `shared_with` | The other machines reporting the same secret, matched by fingerprint (a rotating login copied between machines signs the others out; the fingerprint itself is never returned) |
| `shared_with_stale` | The subset of `shared_with` on machines that stopped checking in, which the UI doesn't warn about; each `machines[]` entry carries the same `stale` flag |
| `mapping` | Why it maps to its account: `local`/`verified`/`claim`/`operator`/`config`/`server`/`pending` |
| `status` | `valid`/`expiring`/`expired`/`invalid`/`failing`/`stale`/`unknown` |
| `expires_at`, `expires_in_seconds` | Expiry of the credential |
| `token_types` | The token types the source carries |
| `can_refresh` | The server can refresh it |
| `refreshed_by` | `server`, or `machine` for a rotating provider's login that a sidecar's CLI owns and the server must not refresh |
| `keep_alive` | `on`/`off`/`unknown` for a machine-sourced agy, Claude Code, Codex or xAI login that carries a refresh credential, live or not: whether that sidecar runs with `--keep-alive`; `unknown` = an older sidecar that does not report it; `null` when not applicable |
| `keep_alive_desired` | The operator's server-side override for that login's sidecar, `null` = none; differs from `keep_alive` while a request is pending |
| `health` | Collection provenance: `untried` for a credential that has never been attempted, else `healthy`/`degraded`/`auth_failed`/`unavailable` |
| `is_active` | The source behind the account's latest successful collection |
| `rejected` | True when the provider rejected the credential |
| `redundant` | True when it is expired and unrefreshable but another healthy credential for the same account can stand in for it (the Home banners skip those, exactly as Token Health's `redundant` does) |
| `unused_reason` | Why a server env/file credential that is present isn't feeding any collection: `provider_disabled`/`default_disabled`/`account_keyed_config`/`shadowed_by_config_key`. Such credentials are read from the host at request time, so they appear even before a first collection |

Sources within an account are ordered active → healthy → dead. Both keep-alive
values are resolved per login: a per-login report/override (see the keep-alive
`PUT` above) wins over the sidecar-level one.

One account carries:

| Field | Meaning |
|-------|---------|
| `archived` | The account's `provider_configs` row is archived: the account is retired, nothing is collected for it, and a credential mapped onto it is *parked* (see [Parked mappings](#parked-mappings)) |

The inventory itself carries:

| Field | Meaning |
|-------|---------|
| `blocked_collection` | Unmapped, token-withheld origins whose provider has no collection from any enabled source in the last 6 hours (the Home "credential unmapped" banner) |
| `unmapped_count` | Counts only origins without an effective hint, like the Untagged list |
| `blocked_sources` | The credential sources a machine found but could not read (a browser cookie store the browser holds open, or one whose values are app-bound encrypted). Each entry names the provider, the store (`source_store`), the reason (`reason`: `source_locked`/`source_unsupported`/`source_unreadable`), the detail (`detail`) and the fix (`remedy`), and says whether the provider is still collecting from another source (`collecting`) — those are the "Unavailable" tab, the Home "cookie store unavailable" banner, and the provider page's empty state |
| `merged_accounts` | The accounts that were merged away (`provider_id`, `account_id`, `destination_account_id`, `merged_at`) and where each of them went. A merged-away account holds no credential at all, so it is not among the accounts above — this is the difference between "this account no longer exists" and "this account never existed" (see [Account lifecycle](#account-lifecycle)) |
| `credential_file_migration_notice` | `null`, or `{providers: [{provider_id, name}], removed_count, recorded_at}` for confirmed legacy server credential-file removals. It contains no credential values or file paths (see [Credential-file migration notice](#credential-file-migration-notice)) |

## System: provider configuration

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/provider-configs` | Admin | Provider configuration listing: provider → account. Every account names the provider it belongs to (`accounts[].provider_id`), the merges per provider are listed under `merged_accounts`, and every merged-away account entry carries `merged_into` (see [Account lifecycle](#account-lifecycle)) |
| `PUT` | `/api/v1/system/provider-config/{provider_id}/{account_id}` | Admin | Per-account provider configuration write (`enabled`, `archived`, `api_key`, `session_cookie` and the explicit clear flags). `{"archived": true}` retires the account and `{"archived": false}` restores it; a write that would resurrect a **merged-away** account is refused with `409`. See [Account lifecycle](#account-lifecycle) |
| `DELETE` | `/api/v1/system/provider-config/{provider_id}/{account_id}` | Admin | Soft-archive an account and clear any matching `credential_tags` hints. See [Account lifecycle](#account-lifecycle) |
| `PATCH` | `/api/v1/system/provider-config/{provider_id}/{account_id}/credential-sources` | Admin | Set enabled state and zero-based order for every known source on this account; duplicate priorities are allowed and ties sort by `source_id`; optional `all_machines` applies each listed source preference to matching sidecar origins across hosts; values are never returned |
| `PUT` | `/api/v1/system/provider-account-label/{provider_id}/{account_id}` | Admin | Save the display label of one account independently of its collection settings (20/min). Body `{"account_label": "<name>"}` — at most 120 characters, `null` clears it. Returns `{"status": "saved", "provider_id": ..., "account_id": ...}`; `404` for an unknown provider. This is the write path behind the account-label behaviour described in [Automatically discovered Claude accounts](#automatically-discovered-claude-accounts) |
| `POST` | `/api/v1/system/provider-account-merge/preview` | Admin | Report what a merge would move and how many rows collide. Body `{"provider_id", "source_account_id", "destination_account_id"}` plus optional `confirm_collisions` and `confirm_shared_default`. Does not change anything. See [Account lifecycle](#account-lifecycle) |
| `POST` | `/api/v1/system/provider-account-merge/apply` | Admin | Merge one account into another — the usage history, quota cards, credential-source rows, operator tags and credential move to the destination, and the source id is retired. Body `{"provider_id", "source_account_id", "destination_account_id"}`; `confirm_collisions` accepts the deduplication, `confirm_shared_default` merges with the shared `default` identity. Refused when either side was already merged away. See [Account lifecycle](#account-lifecycle) |
| `POST` | `/api/v1/system/provider-account-unmerge/preview` | Admin | Report what an un-merge would send back, what cannot go back, and the rows that got in the way. Body `{"provider_id", "source_account_id"}` plus optional `confirm_collisions`. Does not change anything. See [Account lifecycle](#account-lifecycle) |
| `POST` | `/api/v1/system/provider-account-unmerge/apply` | Admin | Undo one merge. Body `{"provider_id", "source_account_id"}`; `confirm_collisions` and `previewed_collisions` acknowledge the preview. `409` when the merge has no journal, when the destination was merged away too, or when the rows in the way changed since the preview; `404` when no merge is recorded for the account. See [Account lifecycle](#account-lifecycle) |
| `POST` | `/api/v1/system/provider-config/preview-account` | Admin | Suggest an `account_id`/label for a new credential before saving it; `detect_billing: true` additionally proves how the account bills and returns it as `suggested_billing_type` (a proof costs a round trip to Claude, so it is asked for, not done on every keystroke) |
| `POST` | `/api/v1/system/provider-config/console-organizations` | Admin | Discover API organizations using a supplied Console `session_cookie` (cookie is not persisted) |

Every account in the provider-configs listing names the provider it belongs
to (`accounts[].provider_id`). The Claude family groups several providers
under one card, so an account listed under the family is still addressed by
its own provider — a rename, an archive or a billing model never lands on the
family's representative.

The archive / merge / un-merge semantics of an account are described once,
under [Account lifecycle](#account-lifecycle). A credential mapping onto an
archived account is *parked* — see [Parked mappings](#parked-mappings).

## System: webhooks

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/webhooks` | Admin | List configured threshold-alert webhooks |
| `POST` | `/api/v1/system/webhooks` | Admin | Create a threshold-alert webhook |
| `PATCH` | `/api/v1/system/webhooks/{webhook_id}` | Admin | Update a threshold-alert webhook |
| `DELETE` | `/api/v1/system/webhooks/{webhook_id}` | Admin | Delete a threshold-alert webhook |
| `POST` | `/api/v1/system/webhooks/{webhook_id}/test` | Admin | Send a test alert to one webhook |

An alert's optional `account_id` scopes it to one account; `credential_alerts`
(default true) also fires the same webhook when a matching credential's Token
Health goes expired/invalid.

Webhook URLs must use the supported HTTPS endpoint hosts: Slack
(`hooks.slack.com`) or Discord (`discord.com` and the legacy `discordapp.com`).
Webhook list entries include optional `validation_error` text for saved URLs
blocked by the current destination policy. Configured encryption failures
return HTTP 503 and refuse credential persistence.

## System: diagnostics

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `POST` | `/api/v1/system/debug/sources/{provider_id}` | Admin | Try each credential source of one account (`account_id`, default `default`) once, live (`5/minute`), and report per source `outcome` (`healthy` / `degraded` / `auth_failed` / `unavailable`, or `waiting_on_machine` / `disabled` / `pending` / `over_limit` for a source deliberately not called; at most 12 per request, `truncated` says when more were left out), `http_status`, `error_type`, a redacted `message`, `cards` and `duration_ms`. Read-only: no health, promotion, backoff or cookie-tag change, no token refresh, no secret values |
| `POST` | `/api/v1/system/debug/claude-cloud` | Admin | Operator-only sample of one cached Claude source (`2/minute`, one in flight per worker). Strict body: `account_id`, `source_id`, optional `organization_id` / `session_id`, `max_history_pages` 1–3 (default 2), `report_version` 1 or 2 (default 1); at most 8 KiB. Verifies holder and organization, then samples the first 20 sessions and at most 150 events of one session within 30 s, 1 MiB per response, 4 MiB and five GETs. Returns fixed outcomes and structural counts only; v2 adds counts at declared paths and an independent SDK-style audit. See [Claude cloud diagnostics](#claude-cloud-diagnostics-operator-only) and the [operator diagnostic](evidence/acceptance/claude-cloud-accounting-669.md#operator-triggered-server-diagnostic) |
| `POST` | `/api/v1/system/debug/claude-chat-fixture` | Admin | Operator-only observation of one ordinary Chat target (`2/minute`, shares the cloud diagnostic worker gate). Strict private inputs `account_id`, `source_id`, `organization_id`, `conversation_id`; three fixed GETs, 20 s, 3 MiB. Returns shape counts only. See [Ordinary Chat fixture diagnostic](#ordinary-chat-fixture-diagnostic) |
| `GET` | `/api/v1/system/debug/raw/{provider_id}` | Admin | Run collector and return raw HTTP exchanges (debug; secrets redacted best-effort); optional `account_id` selects that account's collector. Omitting it preserves provider-wide lookup and captures the first active match (or creates the registered collector if none is active) |

### Ordinary Chat fixture diagnostic

`POST /api/v1/system/debug/claude-chat-fixture` checks whether one ordinary
claude.ai Chat conversation is visible to Runway, separately from the Code cloud
protocols. All four body fields are required strings (organization and
conversation ids are UUIDs); unknown fields, duplicate keys and bodies over 8 KiB
are rejected. Keep target ids in private operator input — never in command
arguments, issue comments or retained evidence.

The source must already be cached, enabled, account-scoped and manually
configured; there is no environment, file, browser, Keychain or alternate-source
fallback. The three GETs go only to `https://claude.ai` (`/api/account`,
`/api/organizations`, then the conversation), and account, organization and
conversation identity must match. Responses stay in memory; redirects, proxies,
compression, retries, token renewal and model execution are disabled. Eligibility
and the exact cache instance are rechecked before every dispatch and before
publishing, so replacing the source invalidates an in-flight result. An HTML
response or `cf-mitigated: challenge` reports `challenge` and stops the attempt.

The report (`format_version: 1`) has a fixed `outcome` and `stage`, request and
status counts, identity booleans, message-role and message-id shape counts, and
usage-shape histograms (only `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens` and `token_count` shape;
at most 500 messages). No text, identifiers, credentials, raw errors or counter
values are returned. `observed` shows visibility of this target only;
`history_complete`, `exact_token_accounting_verified`, `provenance_verified`,
`cloud_contract_verified` and `ready_for_ingestion` are always `false`.

The standard-library evaluator
`python -I -S scripts/verify_claude_chat_fixture.py < sanitized-envelope.json`
checks an envelope (`format_version: 1`, `source_commit`, `image` digest,
`observed_at`, `deployment` of `selected-production` or `isolated-test`, and
`report`). Exit status **2** and `status: stop` are expected even for a visible
fixture, because accounting, completeness and provenance stay unverified.

## Data health (admin)

See [docs/data-health.md](data-health.md) for what each check finds and fixes.

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/data-health/` | Admin | Cached scan report; starts the initial background scan. Returns `scan_error` and keeps any prior report stale after a failure; retry with `POST /rescan` |
| `POST` | `/api/v1/system/data-health/rescan` | Admin | Start a fresh scan (`202`; no-op if one is already running) |
| `POST` | `/api/v1/system/data-health/{check_id}/preview` | Admin | Read-only preview of a fix for one finding group — body: `{group_key, params}`; may include safe samples and a required `confirmation_text` |
| `POST` | `/api/v1/system/data-health/{check_id}/apply` | Admin | Apply a fix (`202`, returns `job_id`) — body: `{group_key, params, confirm: true}`. `400` when the `confirm` field is missing or when no scan has run yet; `404` for an unknown `check_id`; `409` if the latest scan failed, a scan/job is running, or the check is blocked by an upstream finding |
| `GET` | `/api/v1/system/data-health/jobs/{job_id}` | Admin | Poll a started fix job's status/result. A failed job carries a plain-language `error` (never the raw exception), an `error_kind` (`db_busy` when the database was busy with other writes, `unknown` otherwise) and a `retryable` flag that says whether running the same fix again could succeed |

## LimitCard Schema

```typescript
interface TokenUsage {
  input?: number;
  output?: number;
  reasoning?: number;
  cache_read?: number;
  cache_create?: number;
  total?: number;
}

interface LimitCard {
  // Core display fields
  service_name: string;     // Provider name (e.g., "Claude Pro")
  icon: string;             // Unicode emoji
  remaining: string;        // Remaining quota (e.g., "85%", "$12.50")
  unit: string;             // Unit description (e.g., "tokens", "/ 100")
  reset: string;            // Human-readable reset (e.g., "in 4h 23m")
  health: string;           // "good" | "warning" | "critical" | "unknown"
  pace: string;             // "Stable" | "Sustainable" | "Moderate Burn" | "Fast Burn" | "Pending Reset" | "Exhausted"
  detail: string;           // Additional context

  // Identity & routing
  provider_id?: string;     // Platform key (e.g., "anthropic", "gemini")
  account_id?: string;      // Unique account hash/ID
  account_label?: string;   // Human-readable identity (email, org)
  sidecar_id?: string;      // Originating host; null = local collection
  model_id?: string;        // Specific model; null = aggregate snapshot

  // Usage data
  used_value?: number;      // Raw used amount
  limit_value?: number;     // Raw limit amount
  is_unlimited?: boolean;
  unit_type?: string;       // "currency" | "tokens" | "requests" | "minutes" | "percent" | "generic"
  currency?: string;      // "USD" | "EUR" | "CNY"
  credit_breakdown?: Array<{ // Optional purchased/promotional balance explanation
    kind: 'purchased' | 'subscription' | 'promotion';
    label: string;
    currency: string;
    remaining_value: number;
    granted_value?: number | null;
    expires_at?: string | null; // Expiry, not a quota reset or next grant
    products: string[];
  }> | null;
  window_type?: string;    // "daily" | "weekly" | "monthly" | "session" | "rolling" | "unknown"

  // Token breakdown (when available)
  token_usage?: {          // Token count breakdown
    input: number;        // Input tokens
    output: number;       // Output tokens
    reasoning?: number;    // Reasoning tokens (if available)
    cache_read?: number;    // Cache read tokens (if available)
    cache_create?: number;  // Cache creation tokens (if available)
    total: number;       // Total tokens (input + output + reasoning; cache excluded)
  };
  by_model?: Record<string, {  // Per-model breakdown
    cost: number;           // Cost for this model
    msgs: number;           // Messages from this model
    tokens?: TokenUsage;    // Token breakdown for this model (an object, not a number)
  }>;
  msgs?: number;            // Total message count
  pct_used?: number;      // Percentage used based on cost

  // Metadata
  reset_at?: string;      // ISO 8601 timestamp for tooltip
  data_source?: string;   // "api" | "web" | "local" (origin of payload)
  input_source?: string;  // "config" | "server" | "sidecar" (origin of credentials)
  variant?: string;       // Disambiguates multiple windows of the same type (e.g. "sonnet" vs "opus" weekly)
  quota_pool_id?: string; // Cards sharing this non-null id draw from one physical quota bucket
  error_type?: string;    // Populated when collection fails — surfaces as an Error Card
  stale?: boolean;        // Cache served past STALE_CEILING — real but old data (since the stale ceiling was added)
  collection_failing?: boolean;  // Collection is failing; set alongside `stale`, drives frontend cardStale()
  tier?: string;          // "Free" | "Pro" | "Premium" | "Team" | "Enterprise" — the schema's vocabulary; the value is derived at runtime from the provider's plan
  billing_evidence?: string | null;  // "placeholder" | "prepaid_credit" - evidence that decides no billing model
  usage_url?: string;     // Link to provider usage page
  updated_at?: string;    // ISO 8601 timestamp
  metadata?: Record<string, unknown>;  // Free-form, provider-specific extras

  // Added by /usage/fleet's critical-card view, not stored on the card itself
  fetched_at?: string;      // ISO 8601 timestamp of the collector's last successful poll
  next_poll_at?: string;    // ISO 8601 timestamp of the next scheduled poll (fetched_at + cache_ttl_seconds)
  cache_ttl_seconds?: number; // The collector's cache TTL used to derive next_poll_at
}
```

The `by_model` entry's `tokens` is an object, not a number — a per-model token
breakdown:

```json
{
  "by_model": {
    "claude-sonnet-4": {
      "cost": 1.25,
      "msgs": 42,
      "tokens": {
        "input": 12000,
        "output": 3400,
        "reasoning": 0,
        "cache_read": 9000,
        "cache_create": 0,
        "total": 15400
      }
    }
  }
}
```

See `../app/models/schemas.py` for the authoritative Pydantic definition and
`../webapp/src/api/types/usage.ts` for the TypeScript mirror. Token breakdown semantics, the `data_source`/`input_source` taxonomy, and the event-sourced data model are
documented in [architecture.md](architecture.md) and [collection logic](collection_logic.md#card-provenance-labels).

## Account lifecycle

An account is **archived** (retired, restorable), **merged away** (retired, not
restorable) or **parked** (a credential mapping onto an archived account — see
[Parked mappings](#parked-mappings)). The semantics are in
[architecture](architecture.md#account-lifecycle); this section is the API
contract.

- **Archive.** `PUT /api/v1/system/provider-config/{provider_id}/{account_id}`
  with `{"archived": true}` forces `enabled=false` and evicts the account's
  cached credential. The stored `api_key` / `session_cookie` stay, so
  `{"archived": false}` restores it at once. `DELETE` on the same path
  soft-archives the account and clears its `credential_tags` hints.
- **Merge.** `POST /api/v1/system/provider-account-merge/apply` moves one
  account's history, cards, credential sources, tags and credential to a
  destination and retires the source id. `preview` reports what would move and
  how many rows collide; `apply` takes `confirm_collisions` (accept
  deduplication) and `confirm_shared_default` (merge the shared `default`
  identity). It is refused when either side was already merged away.
- **Merged-away accounts.** The leftover row stays in `accounts` (archived, no
  credential) and carries `merged_into`; a provider's `merged_accounts` lists
  each as `{account_id, destination_account_id, merged_at}` (a chain reports its
  end). Any write that would make one an account again — `{"archived": false}`,
  a credential write, or a PUT that creates the row — is refused with `409`
  naming the destination.

### Un-merge

`POST /api/v1/system/provider-account-unmerge/apply` undoes a journalled merge:
moved rows, credential sources, tags and contributions go back, the provider row
is restored, and the merge record is deleted. `preview` reports what goes back,
what cannot (duplicates the merge dropped, the stored credential it wiped) and
the rows in the way. `apply` takes `confirm_collisions` and `previewed_collisions`
(the count the preview showed; it must still match). It answers `409` when the
merge has no journal, the destination was merged away too (unwind a chain from
the end), the rows in the way changed since the preview, or a collision was not
confirmed, and `404` when no merge is recorded.

The account display label and billing model are described under
[Automatically discovered Claude accounts](#automatically-discovered-claude-accounts).

## Behaviour notes

### Source sync state

`GET /api/v1/fleet/sidecars` (and `…/{id}`) report, for an enrolled
`device-<hex>` entry, whether it completes `POST /api/v1/fleet/source-sync`:

| `source_sync_state` | Meaning |
| --- | --- |
| `ok` | An accepted sync in the last hour. |
| `pending` | Enrolled under 10 minutes ago, not synced yet. |
| `never` | Older than that and never synced. |
| `stale` | Synced before, but not for over an hour. |

`source_sync_last_at` is the last accepted sync (`null` for `pending` and
`never`); both fields are `null` for a legacy sidecar or a revoked device. Only
an accepted `source-sync` counts, so a proxy that redirects the route shows up
as `never` or `stale` (see [forward-auth.md](forward-auth.md#verify-the-bypass)).

### Signed fleet request retries

Authenticated ingest and signed config requests consume a durable single-use
signature receipt before any handler effect. Reusing an envelope returns `409`
(`detail.error: "replayed_request"`), even after a validation error; a receipt
storage failure returns `503`. Sign every retry with a fresh timestamp (at most
five minutes old or one minute ahead). Event ids still deduplicate retried
events.

### Fleet login-directory additions

`PUT /api/v1/fleet/sidecars/{sidecar_id}/settings` accepts optional
`claude_config_dirs` and `codex_home` arrays (at most 32 paths of 4096
characters each). Omission keeps the field; `null` or `[]` clears it. Absolute
and home-relative paths are accepted; control characters, globs, relative paths
and known credential-file names are rejected. Only the sidecar evaluates them.

Fleet rows expose `login_dirs_desired`, check-in responses carry `login_dirs`,
and the first ingest batch may include `login_dirs_report`; inventory machine
rows expose it (`null` = the sidecar reports no support):

```json
{
  "applied": {"claude_config_dirs": ["~/work-claude"], "codex_home": []},
  "searched": {"anthropic": ["/home/me/.claude", "/home/me/work-claude"], "chatgpt": ["/home/me/.codex"]}
}
```

`applied` acknowledges Fleet settings; `searched` lists resolved candidates,
including missing ones. With custom roots active, events carry an optional
`credential_origin` (for example `path:/home/me/work-claude/projects`); each
root has its own identity and watermark, and an unidentified root's events use
only an exact origin mapping.

### Credential-file migration notice

`GET /system/credentials` includes `credential_file_migration_notice`: `null`, or
`{providers: [{provider_id, name}], removed_count, recorded_at}` after confirmed
removals of legacy server credential files (no values or paths).
`POST /system/credentials/migration-notice/dismiss` (admin) dismisses it
idempotently and permanently.

### Parked mappings

An archived account cannot collect, but the machine that reported its credential
keeps reporting the origin. Mapping that origin onto the archived account
**parks** it. A parked mapping:

- **resolves the origin**: the credential leaves
  `GET /api/v1/fleet/credentials/tags/pending`, `unmapped_count` and
  `blocked_collection`, and the next manifest does not reopen it;
- **is inert**: it is not shipped in `GET /fleet/config`'s `account_tag_hints` or
  the manifest's `resolved` map, stamps the archived account with no card or
  event, and does not move the cached credential. An event only the parked
  mapping could attribute is held in `GET /api/v1/fleet/events/pending`;
- **revives on restore**: parking is derived from `provider_configs.archived`,
  not stored on the tag, so restoring the account ships the hint again with no
  re-tagging.

A parked credential stays in `GET /api/v1/system/credentials` under its account
(`archived: true`) but holds no secret. A *redirect* tag (`target_provider_id`)
is parked according to its **target** provider's account.
`POST /api/v1/fleet/credentials/tags` reports the kind it wrote as
`target_archived`.

### Automatic Claude browser connections

A pending sidecar browser source for `anthropic` (Subscription) or
`anthropic_console` (Console API credits) is verified against its own cookie.
Subscription identity is the account email; Console identity is the organization
UUID. A sole eligible organization is selected automatically only after a
complete verification; several require a choice unless the account already has a
saved binding. An organization with a JSON permission denial is excluded;
challenges, timeouts, rate limits and malformed responses prevent automatic
selection. Disabled or archived accounts stay so.

`GET /api/v1/fleet/credentials/tags/pending` items for Claude providers add
`source_id` (derived from the exact sidecar and origin), `selection_required`
and `organization_count` (0 before verification), next to `origin_label`,
`origin_app` and `origin_detail`.

Admin `GET /api/v1/fleet/credentials/{source_id}/claude-organizations?provider_id=anthropic|anthropic_console`
rechecks the pending source:

```json
{"organizations":[{"id":"11111111-1111-4111-8111-111111111111","name":"Personal"}],"complete":true,"selection_required":false}
```

Only organizations with usable quotas or a valid Console balance appear;
`complete:false` means one could not be evaluated, so a single returned
organization is not an automatic choice. Admin `POST` to the same path takes
`{"provider_id", "organization_id"}`, verifies membership and quota/billing
access using that pending source, saves `ProviderConfig.claude_organization_id`,
promotes the source and returns `{"status":"ok"}`. Errors: `404` missing or
mismatched source/provider, `422` invalid provider or UUID, `403` inaccessible
organization, `409` changed or already assigned source. Messages are sanitized.

### Claude Console connections

Settings groups Subscription quotas (`anthropic`) and Console API credits
(`anthropic_console`) under Claude, each with its own credentials, health and
card. The Console total includes purchased and promotional credits;
`credit_breakdown` explains them without changing the total, and the total has
no quota percentage.

`POST /api/v1/system/provider-config/console-organizations` takes
`{"session_cookie": "<sessionKey or Cookie header>"}` and returns the
API-capable organizations as `{"organizations": [{"id", "name"}]}` (empty means
none accessible, not a zero balance). To preview, send `provider_id:
"anthropic_console"`, `session_cookie` and `organization_id` to
`/api/v1/system/provider-config/preview-account` (it reports `label_source:
"organization"`), then save with
`PUT /api/v1/system/provider-config/anthropic_console/{organization UUID}`.
Identity stays stable across cookie and label changes; responses expose only
whether a credential is configured. Clear it with `clear_session_cookie: true`.

### Automatically discovered Claude accounts

An account the server discovers is named after what it is rather than
"Default": a subscription by its member's email, a Console account by its
organization's name. The name goes to `ProviderConfig.account_label` when the row
is created, and an unset label is filled in on a later poll. A label the operator
chose is never overwritten; set one with
`PUT /api/v1/system/provider-account-label/{provider_id}/{account_id}`. A card's
label is the account's name and keys the card to its account; the identity a card
proved (an organization-scoped one marks it `org: `) lives in the card detail.

The billing model comes from the shape of the published cards: a percentage of a
limit means subscription, a currency balance means pay-as-you-go, and a quota
card wins when both exist. A card may name its evidence kind — `"placeholder"`
for a window the collector invented, `"prepaid_credit"` for a claude.ai prepaid
balance — and such a card decides nothing. It is written to
`ProviderConfig.billing_type` on creation and filled in later if unset;
`billing_type_source` says who wrote it (`"operator"`, `"detected"`, or empty).
For an account added by hand the preview returns `suggested_billing_type` (a
Console connection while the organization is picked; a subscription with
`detect_billing: true`). The operator's choice always wins.

### Native Claude OAuth token metadata

Sidecar token cards may carry `oauth_read_only: "1"` and `oauth_account_uuid` in
`metadata`, next to `oauth_token` and `expiry_date` (epoch milliseconds as a
string), through the existing signed ingest endpoint. The UUID is a claim to
verify against the holder profile, not an assignment. Read-only native sources
cannot refresh; machine-bound origins identify each Desktop cache slot or CLI
Keychain service, changed inferred credentials re-verify their holder and quota,
and the new Keychain discovery rules never forward refresh secrets. Kimi Coding
CLI bundles may also carry `metadata.cli_device_id` (printable ASCII, at most 256
characters); it is device metadata, never an authentication value, and is not
returned by the inventory.

## Local sidecar Keychain source settings

**These are the sidecar's own loopback routes**, served by the sidecar on the
machine that holds the credentials. They have no `/api/v1/` prefix and the server
never exposes them; they are described here because the server protocol below
interacts with them. See [sidecar.md](sidecar.md).

The loopback settings UI exposes `GET /sources` and `POST /sources`, with exact
Host and same-origin checks. `GET` is inert and returns catalog labels,
capability revisions, read intent, state, renewal support, dependency and
announcement state — never credentials. `POST` takes an action and `source_id`
(enable, disable, dismiss, authorize; `enable_all` / `disable_all` act on known
sources; the renewal action takes an exact boolean and needs Claude Code read
consent first). Authorize is an explicit local, bounded, single-flight setup
attempt. Unknown actions or ids fail without credential IO.

Local config stores `keychain_sources` entries (exact-boolean read, catalog
revision, monotonic generation, approved destination), a `claude_keychain_renewal`
permission that defaults off, `keychain_announcements` (dismissed revisions) and
`keychain_withdrawals` (pending generations), replaced atomically and owner-only
on macOS. File and env collection are unaffected; Windows Credential Manager is
outside this macOS permission model. For the consent model, withdrawal and
remote control see [source-consent.md](source-consent.md).

## Fleet Keychain source lifecycle (protocol 1)

A sidecar redeeming a pairing code sends `device_protocol=1` and
`platform=Darwin|Linux|Windows`; the platform is bound to the enrolled identity
and is a device assertion, not OS attestation. With `DB_ENCRYPTION_KEY`
configured the single-use response also carries `device_id`, `device_key`,
`device_api_url` and `source_protocol=1`; the random device key is stored
encrypted and saved in the sidecar's owner-only config. Without server
encryption pairing keeps its legacy response and Keychain forwarding stays
limited. Re-pairing creates a new device and does not claim a hostname's earlier
sources. An authenticated `source-sync` 401 surfaces a re-pair notice; the
sidecar never falls back to the shared key.

Linux and Windows browser cookies need no macOS Keychain consent but keep their
scraper identity and server-owned generation, so pause and revoke still apply and
replay cannot restore withdrawn credentials. The known Windows Credential Manager
origins (`github` / `keychain:git:https://github.com`, `antigravity` /
`keychain:gemini:antigravity`) have stable lifecycle ids
(`github.windows-credential`, `antigravity.windows-credential`) with pause,
resume, revoke and replay enforcement; they are excluded from local Keychain
opt-ins, and reconnecting a revoked one requires re-pairing. Unknown native
origins are skipped.

Device requests send `X-Runway-Device-Id`, `X-Timestamp` and `X-Signature`:
HMAC-SHA256 over `DEVICEv1|device_id|timestamp|purpose|` plus the raw body (raw
query bytes for `GET` config). Purposes are `ingest`, `manifest`, `config` and
`source-sync`, which binds the operation without relying on a proxy preserving
the public path. Timestamp limits, body caps and replay protection apply. The
shared ingest key cannot authenticate a device header or claim a reserved
`device-<32 hex>` identity.

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `POST` | `/api/v1/fleet/source-sync` | Device HMAC | Device status and ordered command acknowledgements; a forward-auth proxy must let it through or a paired Mac withholds its Keychain credentials ([forward-auth.md](forward-auth.md#verify-the-bypass)). The body has `protocol=1`, `sources` (id, provider, label, revision, generation, literal-boolean `read`, state, `new`, renewal flags, optional `last_read_at` / `last_renewed_at`) and acknowledgements `{id, status: applied\|denied\|superseded}`. The response lists accepted scraper ids, withdrawals `{source_id, generation}` and up to 256 pending commands `{id, source_id, generation, action}` in order. Acknowledged commands are kept 30 days, always keeping each scraper's latest ordering barrier. Reports from an older generation update contact time only. |
| `GET` | `/api/v1/fleet/sidecars/{sidecar_id}/sources` | Admin | Inert status: `protocol=1`, `sources` and pending commands. Each source extends its report with `remote_paused`, `revoked`, `report_at`, `report_stale`, `announcement`, `renewal_capable`, `collection_health` (`withdrawn`\|`paused`\|`healthy`\|`action_required`\|`untried`) and `last_collection_at`. A stale report never expires an eligible server credential, and `report_at` is independent of the device's `last_seen`. macOS sources report periodically; Windows Credential Manager sources update contact only when credentials are submitted, so their report can be stale while the device is online and collection healthy. |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/sources/{source_id}/commands` | Admin | `{action}`: `pause`, `resume`, `revoke`, `request_setup` or `dismiss`; returns `{id, status: pending}`. Pause and revoke withdraw that source's server credentials at once. Resume needs current local read consent and a matching non-revoked generation, and clears a server pause only after acknowledgement. Setup creates a local notice only. Dismissal is scoped to the current catalog revision. |

Native credential cards and manifest entries carry `keychain_consent`
(`source_id`, `revision`, `generation`). The server checks device, provider,
origin, revision and live generation before accepting a contribution.
Withdrawal persists a tombstone and removes only the matching encrypted payloads,
so late ingest cannot recreate them; re-enabling needs a newer local generation.
Rejected or unknown cards and manifest entries are skipped without discarding the
rest of the upload; ingest returns a sanitized `credentials_rejected` card count,
and a skipped manifest source cannot retire last-good origins. Bundles stay
separate across devices and origins, even for identical tokens. Enrolled
contributions survive a server restart and an offline Mac, provider expiry or
rejection stays authoritative, and usage history and manual or file contributions
are kept. Older servers never receive newly opted-in Keychain credentials.
Pausing keeps the encrypted bundle so an acknowledged, locally permitted resume
reuses it without a new read; revocation and local opt-out erase it and keep the
generation tombstone.

## Claude cloud diagnostics (operator only)

The diagnostics below are admin-only, protected by Host and CSRF checks, reject
unknown or duplicate keys, and never return identifiers, credentials, cursors,
content or raw errors. `GET` on each is inert. All acceptance and ingestion flags
(`cloud_contract_verified`, `ready_for_ingestion`, `live_acceptance_passed`,
`provenance_verified`) are always `false`; none activates a collector.

- `POST /api/v1/system/debug/claude-cloud` — sample one cached source (see the
  table above).
- `POST /api/v1/system/debug/claude-chat-fixture` — see
  [Ordinary Chat fixture diagnostic](#ordinary-chat-fixture-diagnostic).
- `POST /api/v1/system/debug/claude-cloud-verification` — a resumable,
  bounded verification job (below).
- `POST /api/v1/system/debug/claude-cloud-list-boundary` — a one-shot re-read of
  a failed list page (below).

### Resumable verification

Body at most 256 KiB, six actions per minute. One current encrypted job exists
at a time; server encryption is required (no plaintext fallback).

| Action | Effect |
|---|---|
| `start` | `{account_id, source_id}` plus optional `organization_id`, `session_id`, `declared_category`, `job_key`, `expected_local`. Records a job against the exact eligible cached source; no provider request. A `session_id` matches a listed id with or without its `cse_` / `session_` prefix (an exact match wins). `expected_local` holds at most 256 reduced assistant records with strict numeric usage and identities HMACed with the supplied 32-byte hex `job_key` (new key per job). Never submit transcripts, tokens or raw message ids. |
| `step` | `{job_handle}`. Advances one batch: at most five GETs, 30 s, 4 MiB total and 1 MiB per response, re-checking profile, account, organization and local source authority. The session list is exhausted before the selected session's history. No sweep, retry, fallback, refresh or collector activation. |
| `status` | Reads the current job (or `job_handle`) without provider I/O, re-checking local authority first. `{"state":"absent"}` with no job; `{"state":"unavailable","job_handle"}` if the checkpoint is unreadable (cancel it explicitly). |
| `cancel` | Removes the checkpoint between steps; an active lease returns `409`. |
| `archive_status` / `archive` | `archive` `{job_handle, revision}` preserves an exact terminal checkpoint and frees the current-job slot, idempotently; a mismatched revision, nonterminal job, active lease or full archive returns `409`. |

Every provider attempt is charged and journaled before dispatch. A clean batch
boundary resumes; an ambiguous in-flight intent stops as interrupted and cannot
replay. A job is limited to 512 attempts, 256 MiB, 20,000 listed sessions,
100,000 history records and 30 minutes. At most 64 terminal archives (64 MiB of
ciphertext) are kept, with no automatic deletion; each checkpoint holds at most
16 MiB plaintext / 32 MiB ciphertext. Pruning an archive is an owner-only
maintenance task: stop Runway, take and verify an owner-only SQLite backup that
contains the identical revision, delete the exact row with a parameterised
`DELETE FROM claude_cloud_verification_archives WHERE job_handle=? AND revision=?`
in one transaction that must affect exactly one row, and never touch the current
`claude_cloud_verification` table.

Complete-history verification follows non-empty continuation cursors even on
short pages; an omitted `next_cursor` ends the traversal like an explicit null,
`has_more` must be boolean and agree with continuation (a contradiction stops as
`unsupported_contract`), and completion is never inferred from a record count.
This does not establish a published API contract or coverage of every Claude
surface. A rejected session-list page is described by an optional `rejected_list`
(`boundary`, `response_encoding`, `json_state`, `duplicate_json_keys`,
`nonfinite_json_values`) that never enables ingestion.

The strict report (`format_version: 1`) carries the opaque `job_handle`, timing,
`phase`, `outcome`, request and byte counters, verification booleans, list and
history counters, numeric `sequence` diagnostics, the SDK-envelope `audit`, the
input/cache `comparison` and `subagent_comparison`, fixed `scope_observations`,
`declared_category`, `execution_kind` with its lower-snake `environment_kind`
(read from the list row's top-level field, falling back to a nested
`environment.kind`; execution metadata is an observation, not proven
provenance), and `rejected_list`. A checkpoint written with a newer field cannot
be resumed by an older build, so finish or cancel a job before rolling the image
back. The standalone `scripts/verify_claude_cloud.py` accepts the report in an
envelope version 2 (with `source_commit`, `image`, `observed_at`, `deployment`)
and always answers `stop` with the remaining accounting, coverage and provenance
checks; the build metadata is not authenticated.

### Failed list-boundary diagnostic

`POST /api/v1/system/debug/claude-cloud-list-boundary` takes only
`{"job_handle":"…"}` for a checkpoint stopped with `unsupported_contract` during
listing (body at most 8 KiB, two requests per minute). It re-reads the recorded
failed cursor with at most two GETs (profile and that page) in 20 s, 1 MiB per
response, and requires the exact cached source authority; there is no alternate
source, refresh, discovery, sweep, retry or checkpoint mutation, and the original
budget is not reset. The report returns `outcome`, `stage`, `requests`,
`http_status`, verification booleans, `original_list_pages`,
`original_listed_sessions` (`null` rather than 0 when unreadable) and, only for
outcome `observed`, `boundary` evidence: type categories, record and ID counts,
the cursor category (`missing`, `null`, `empty_string`, `nonempty_string`,
`invalid`), `cursor_supported`, optional `cursor_repeated`, `has_more_state`,
`response_encoding` and `json_state`. `cursor_supported` describes the current
parser, not a provider contract. The validator accepts the report in evidence
envelope version 3 and returns STOP with `list_contract_requires_review`.
