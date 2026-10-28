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
| `GET` | `/api/v1/usage/sessions/paginated` | Admin | Paginated session browser with server-side sort (`sort_by=recent\|tokens\|duration\|messages\|cost`, `sort_dir=asc\|desc`) and optional `project` / `sidecar_id` filters |
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
carries the session's `project`, `cwd` and pushing host (`sidecar_id`), its
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
| `PUT` | `/api/v1/fleet/sidecars/{sidecar_id}/settings` | Admin | Override the fleet-wide auto-update flag and/or update channel for one sidecar, or set its log level — body `{"auto_update": true\|false\|null, "update_channel": "stable"\|"beta"\|"edge"\|null, "log_level": "DEBUG"\|"INFO"\|"WARNING"\|"ERROR"\|null}` (the level is case-insensitive; an unknown one is `422`; delivered as `log_level` on the ingest response, `null` = the sidecar's own level, applied without a restart; the list adds `log_level_desired` and the sidecar-reported `log_level`). A key that is left out stays as it is; `null` clears that override so the sidecar follows the fleet again. An unknown channel is `422`. Delivered as `sidecar_auto_update` / `sidecar_update_channel` on its next ingest response; the response echoes the overrides and the effective values. The sidecar list adds `auto_update_desired` / `update_channel_desired` (the overrides), `effective_auto_update` / `effective_update_channel` (what it is told) and `auto_update` / `update_channel` (what the sidecar reports; `null` = older sidecar) |
| `PUT` | `/api/v1/fleet/sidecars/{sidecar_id}/keep-alive` | Admin | Turn a sidecar's keep-alive (renewal of the agy, Claude Code, Codex and xAI logins) on or off remotely — body `{"enabled": true\|false\|null}`, `null` defers to the sidecar's own flag and the fleet-wide default (`sidecar_keep_alive_default` in app-config). An optional `provider` (one of `antigravity`, `anthropic`, `chatgpt`, `xai`) targets just that login: `enabled` then sets or (`null`) clears that login's override on top of the sidecar-level setting, and the response lists `keep_alive_desired_providers`. Delivered as `keep_alive_desired` / `keep_alive_desired_providers` on its next ingest response; the sidecar applies it without a restart |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/adopt` | Admin | Fold a legacy (shared-key) sidecar into the enrolled `device-<hex>` that replaced it after a re-pair — body `{"legacy_sidecar_id": "desktop", "dry_run": true}` (`dry_run` defaults to `true`). Moves its `usage_events`, unassigned events, machine-scoped credential tags, per-source enabled/priority choices and (where the device has none) its name, tags and desired settings, rebuilds the derived per-sidecar rollups and windows, then removes the legacy row. Returns `{dry_run, events, pending_events, credential_tags, source_prefs, settings}` (`source_prefs` = per-source `enabled`/`priority` choices copied from the old entry's credential sources onto the device's, matched on provider, account and origin; a source the device has not discovered yet is not created); a dry run adds `from` and `into` (each `{sidecar_id, name, os_platform, first_seen, last_seen, ingest_count}`, so the preview can say which entry goes into which) and `pushes_after` (the device's push count once the old entry's is added); a real run adds `pushes_carried` and `errors_carried`. The device also takes the earlier `first_seen`. `404` for an unknown sidecar, `409` unless the legacy row is one of the device's `adoptable_legacy` candidates. A real run also returns `rollup_pairs_rebuilt` and `windows_rebuilt`. The sidecar list adds `adoptable_legacy` to an enrolled device (the legacy rows with the same hostname and OS that stopped reporting when the device appeared) and the inverse, `replaced_by`, to such a legacy row (the device(s) that replaced it); both are lists of `{sidecar_id, name, os_platform, first_seen, last_seen, ingest_count}` |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/resume` | Admin | Resume collection on a sidecar |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/update` | Admin | Queue a one-shot self-update for the sidecar; applies on its next heartbeat |
| `GET` | `/api/v1/fleet/config` | Optional HMAC | Active collection config the sidecar should poll; unsigned/non-loopback callers get only the `enabled`/`strategies` view, no account data |
| `POST` | `/api/v1/fleet/pairing-codes` | Admin | Mint a one-time pairing code + `runway-sidecar://pair` deep link for a new sidecar |
| `POST` | `/api/v1/fleet/pair` | Public | Unauthenticated one-time-code redeem: exchanges a pairing code for `api_url` + ingest key (10/min/IP). A forward-auth proxy must let it through unauthenticated ([forward-auth.md](forward-auth.md#5-production-hardening-split-the-traefik-router)) |
| `POST` | `/api/v1/fleet/credentials/manifest` | HMAC | Sidecar reports credential origins found this cycle (an entry may carry `reason: "token_withheld"` when the token stayed on the machine because the server cannot verify that provider by source); Claude OAuth entries may include a claimed `account_id`, which Runway matches to an enabled account or surfaces for assignment. `hint_providers` names the providers whose hints the sidecar holds (at most 256 ids — the sidecar truncates to that cap, because an oversized list is rejected and a rejected manifest drops the cycle's `completed_providers` with it), so the server answers for those too. The response's `resolved` map is **complete per provider**: every origin the server resolves for a provider it answered for, and an empty map for a provider it resolves none for — so a sidecar may replace what it holds for those providers instead of merging into it, and a hint the server stopped holding (the account was archived, the tag was deleted) is dropped on the next manifest instead of surviving until the next `/fleet/config`. A provider answered with an empty map is dropped from the sidecar's cache as well, so it leaves `hint_providers` and is not asked about again |
| `GET` | `/api/v1/fleet/credentials/tags` | Admin | List every resolved credential tag (deployment-wide and per-sidecar scopes); each carries `origin_label`, `origin_app` (the tool that owns the file, e.g. `Codex CLI`), `origin_path` (home collapsed to `~`) and `origin_detail` (for a cookie: the browser and profile it came from, e.g. `Chrome · Profile 1` (null for an Anthropic cookie origin, whose private browser profile is never exposed)) so the UI doesn't parse raw origin strings; also `last_matched_at` (freshest machine-reported credential the rule resolved, `null` = never) and `stale` (the `stale_credential_rules` data-health check would list it) |
| `POST` | `/api/v1/fleet/credentials/tags` | Admin | Resolve a pending credential origin to a configured account. `scope: "deployment"` (all machines) is refused with 422 for `cookie:` and `keychain:` origins, which exist on one machine only; `path:` and `env:` origins can still be tagged deployment-wide (a shared home directory is one origin). A tag written against a plain origin also applies to its key-scoped form (`env:ZAI_API_KEY` covers `env:ZAI_API_KEY#<fp>`). The response is `{"status": "ok", "target_archived": false}`; `target_archived` is `true` when the target account is archived — the mapping is then *parked*: it resolves the origin (the credential stops being pending and the operator is not warned any more) but stays inert while the account is archived (no hint ships to the sidecar, no new usage is stamped on the archived account, no collector runs for the provider). Restoring the account revives the mapping automatically — see [Parked mappings](#parked-mappings) |
| `DELETE` | `/api/v1/fleet/credentials/tags` | Admin | Remove one resolved tag in a given scope |
| `GET` | `/api/v1/fleet/credentials/tags/pending` | Admin | List credential origins awaiting operator resolution, with a claimed Claude account ID when available, safe quota previews, and staleness status. Each item also carries `origin_label` (what the credential is, e.g. `Browser cookie`), `origin_app` (the tool that owns it, e.g. `Chrome` for a native Claude cookie or `Codex CLI` for a file) and `origin_detail` (for a cookie: the browser and profile it came from, e.g. `Chrome · Profile 1`; `null` when the machine could not name the jar (null for an Anthropic cookie origin, whose private browser profile is never exposed)) so the UI can say "browser cookie" instead of showing the raw `cookie:<provider>/session` origin |
| `GET`/`POST` | `/api/v1/fleet/credentials/{source_id}/claude-organizations` | Admin | Inspect eligible organizations or select one using an exact pending sidecar source (no credential values returned) |
| `GET` | `/api/v1/fleet/events/pending` | Admin | Paginated queue of events held back under an unresolved `default` identity |
| `GET` | `/api/v1/fleet/events/pending/sessions` | Admin | Paginated pending events grouped by provider, host, and session; supports `sidecar_id`, `provider_id`, and session/model `search` filters |
| `POST` | `/api/v1/fleet/events/pending/assign` | Admin | Assign up to 1000 pending events to one account, promoting them into `usage_events` and creating provider/host mappings. Archived accounts are accepted (disabled ones are not). An optional `target_provider_id` assigns the events to an account of a related provider instead (allow-list: `gemini` → `antigravity`; anything else is a 422); the events are re-homed to that provider and later events from the host follow. The response echoes `target_provider_id`; `provider_id`/`target_provider_id`/`assigned` describe the first provider group only (back-compat), while `mappings` lists every group as `{provider_id, target_provider_id, account_id, assigned}` |
| `POST` | `/api/v1/fleet/events/pending/assign-batch` | Admin | Assign up to 10000 pending events across provider-specific accounts (each assignment takes the same optional `target_provider_id`); validates the full batch before promotion and returns the created provider/host mappings, each with its `target_provider_id`. One provider on one host can't be mapped to two different targets in a single batch. A larger selection is not an error: the Fleet UI splits it into requests of at most 10000 event ids and sends them one after another |

The grouped pending-events response reports `total_events` across the full queue and `matching_events` for the current filters; `total_groups` is the filtered group count used for pagination.

## System: status and configuration

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/health` | Public | Liveness check |
| `GET` | `/api/v1/system/status` | Admin | Collector cache states and error counts |
| `GET` | `/api/v1/system/settings` | Public | Non-sensitive runtime configuration: `version` (the release this build belongs to), `build_sha` (the commit the running build was built from, `null` when the build carries none — every `:edge` image ships the same `version`, so this is what identifies one exact build), `latest_version`/`update_available`, `app_host`/`app_port`, `encryption_enabled`, `admin_auth_required`, `auth_methods`, `user_context`, `is_authenticated` |
| `GET` | `/api/v1/system/audit-log` | Admin | Append-only admin-mutation trail |
| `GET` | `/api/v1/system/sidecar-downloads` | Public | Cached GitHub release assets for the Fleet page's *Add sidecar* card; `?channel=stable\|beta\|edge` |
| `GET` | `/api/v1/system/app-config` | Admin | Global app config; includes `sidecar_update_channel`, `sidecar_auto_update` and `sidecar_keep_alive_default` (fleet-wide keep-alive default: on for every sidecar without its own override) |
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
| `GET` | `/api/v1/system/token-health` | Admin | Health of every credential (cache, dashboard-saved, server env/file): `status` is `valid`/`expiring`/`expired`/`invalid` (provider rejected it)/`failing` (not rejected, but its collections have kept failing for over an hour)/`stale` (sidecar stopped re-reporting it)/`unknown`; `removable` is false for config/server-managed rows. Status uses the same rules as `GET /system/credentials` (expiry, a provider rejection, and staleness), so the two views agree. A **retired** account's credential is not listed at all: nothing live is held for it, so there is nothing to collect with and nothing to refresh. Values are never returned — only types and origin. **Legacy:** the webapp no longer reads this; credential alerts still build on it because webhook scoping depends on its synthetic account ids (`server`, `config:<account>`), so use `GET /system/credentials` for new clients |
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
| `POST` | `/api/v1/system/debug/sources/{provider_id}` | Admin | Try each credential source of one account (`account_id`, default `default`) once, live, and report per source: `outcome` (`healthy`/`degraded`/`auth_failed`/`unavailable`, or `waiting_on_machine`/`disabled`/`pending`/`over_limit` for a source that was deliberately not called; at most 12 sources are called per request and `truncated` says when more were left out), `http_status`, `error_type`, a redacted `message`, `cards`, `duration_ms` (`5/minute`). Writes nothing — no health/attempt update, promotion, backoff or cookie-tag change — and never refreshes a token; each source runs (a few at a time) on its own copy of the collector in a probe mode that also suppresses token-cache stores, the account's rejected flag and provider error events, pinned to that source. Never returns secret values |
| `POST` | `/api/v1/system/debug/claude-cloud` | Admin | Explicit admin-only diagnostic for one cached Claude source (`2/minute`, one in flight per worker). Strict body: `account_id`, `source_id`, optional `organization_id`/`session_id`, integer `max_history_pages` 1–3 (default 2), optional strict integer `report_version` 1 or 2 (default 1); maximum 8 KiB and no duplicate keys. Verifies holder/org, rechecks source and consent, then samples the first 20 sessions and at most 150 events for one listed session. Default v1 is unchanged; explicit v2 adds fixed structural counts at declared paths and an independent SDK-style audit at object-valued `payload`, without decoding strings or traversing arrays. Its observations may use `event`, `data`, `content`, or `payload`; v1 retains only the first three. Location reports are never combined. Fixed outcomes, numeric observations and separate unverified SDK-envelope audits only; never returns identifiers, credentials, cursors, content or raw errors. Bounded to 30s, 1 MiB/response, 4 MiB total, five GETs; no discovery, redirect, compression, retry, refresh, promotion, health or usage write. GET is inert. `sample_complete` applies only to the sampled page chain; `cloud_contract_verified` and `ready_for_ingestion` stay false. See [cloud diagnostic details and readiness](evidence/acceptance/claude-cloud-accounting-669.md#operator-triggered-server-diagnostic) |
| `POST` | `/api/v1/system/debug/claude-chat-fixture` | Admin | Explicit exact-target ordinary Chat observation (`2/minute`, shared cloud diagnostic worker gate). Strict private inputs: `account_id`, `source_id`, `organization_id`, `conversation_id`; no credential values. Cached manual/config cookie only; three fixed GETs maximum, 20s total, 5s/request, 1 MiB/response, 3 MiB total. Returns fixed shape counts only. GET is inert. See [Chat fixture diagnostic](#ordinary-chat-fixture-diagnostic) |
| `GET` | `/api/v1/system/debug/raw/{provider_id}` | Admin | Run collector and return raw HTTP exchanges (debug; secrets redacted best-effort); optional `account_id` selects that account's collector. Omitting it preserves provider-wide lookup and captures the first active match (or creates the registered collector if none is active) |

### Ordinary Chat fixture diagnostic

`POST /api/v1/system/debug/claude-chat-fixture` is an operator-triggered
visibility check, separate from the frozen Code cloud protocols. All four
body fields are required strings; organization and conversation IDs must be
UUIDs. Unknown fields, duplicate keys and bodies over 8 KiB are rejected without
echoing their contents. Existing admin authentication, Host, origin and rate
protections apply. Keep exact target identifiers in private operator input;
never put them in command arguments, issue comments or retained evidence.

The selected source must already be cached, enabled, account-scoped and manually
configured. There is no environment, file, browser, Keychain or alternate-source
fallback. The three GETs go only to `https://claude.ai`: `/api/account`,
`/api/organizations`, then the asserted organization's exact conversation.
Account identity, organization membership and the returned conversation UUID
must match. An existing organization binding must also match. Responses remain
in memory. Redirects, proxies, compressed responses, retries, token renewal,
model execution and credential/health/usage writes are disabled.

Before every dispatch and before publishing results (including failures), the
diagnostic rechecks durable eligibility and the exact cache instance. Removing, replacing or
restoring even an identical source invalidates an in-flight result. This is a
process-local race guard, not a durable source-generation protocol. Denials and
failures stop this attempt; another provider attempt requires explicit operator
intent. A worker gate shared with cloud diagnostics prevents overlap there.
Cancellation releases it; provider requests already dispatched cannot be undone.
An HTML response or an explicit `cf-mitigated: challenge` reports `challenge`,
separately from credential rejection and malformed JSON. It stops this attempt
without cookie re-entry, browser automation or a retry.

The report has `format_version: 1`, a fixed `outcome` and `stage`, request/status
counts, identity-verification booleans, message-role and message-ID shape counts,
and usage-shape histograms at the conversation and message scopes. It inspects
only four declared usage fields (`input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `cache_creation_input_tokens`) and `token_count` shape.
No text, names, identifiers, credentials, raw errors or counter values are
returned. At most 500 messages are inspected; unsupported structures stop rather
than becoming empty success. `observed` demonstrates visibility of this target
only. Unknown pagination is not traversed or treated as complete. Context/quota
fields are not consumption evidence, and SDK accounting rules are not assumed
to apply to ordinary Chat.

`history_complete`, `exact_token_accounting_verified`, `provenance_verified`,
`cloud_contract_verified` and `ready_for_ingestion` are always literal `false`.
A phone-only fixture also needs a separate operator witness; remote MCP
`get_sessions` success does not establish Runway visibility or phone provenance.
No collector activation follows from this report.

The independent, standard-library evaluator runs without application imports:
`python -I -S scripts/verify_claude_chat_fixture.py < sanitized-envelope.json`.
Its strict envelope requires `format_version: 1`, a full `source_commit`,
immutable Runway `image` digest, UTC `observed_at`, `deployment`
(`selected-production` or `isolated-test`) and `report`. It checks evidence
consistency but does not authenticate those receipts. Exit status **2** and
`status: stop` are expected even for a visible fixture: accounting, history
completeness and provenance remain unverified. Before live execution, review the
immutable packaged build, exact source/account/organization/target assertion,
request budgets, current deployment and rollback readiness. Target entry alone
must never initiate provider operations.

## Data health (admin)

See [docs/data-health.md](data-health.md) for what each check finds and fixes.

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `GET` | `/api/v1/system/data-health/` | Admin | Cached scan report; starts the initial background scan. Returns `scan_error` and keeps any prior report stale after a failure; retry with `POST /rescan` |
| `POST` | `/api/v1/system/data-health/rescan` | Admin | Start a fresh scan (`202`; no-op if one is already running) |
| `POST` | `/api/v1/system/data-health/{check_id}/preview` | Admin | Read-only preview of a fix for one finding group — body: `{group_key, params}`; may include safe samples and a required `confirmation_text` |
| `POST` | `/api/v1/system/data-health/{check_id}/apply` | Admin | Apply a fix (`202`, returns `job_id`) — body: `{group_key, params, confirm: true}`. `400` when the `confirm` field is missing or when no scan has run yet; `404` for an unknown `check_id`; `409` if the latest scan failed, a scan/job is running, or the check is blocked by an upstream finding |
| `GET` | `/api/v1/system/data-health/jobs/{job_id}` | Admin | Poll a started fix job's status/result |

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
  stale?: boolean;        // Cache served past STALE_CEILING — real but old data (since #293)
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
`../webapp/src/api/types.ts` for the TypeScript mirror. Token breakdown semantics, the `data_source`/`input_source` taxonomy, and the event-sourced data model are
documented in [architecture.md](architecture.md) and [statistics.md](statistics.md).

## Account lifecycle

An account is **archived** (retired but restorable), **merged away** (retired
and not restorable — the merge moved its history and its credential to another
account), or **parked** (a credential mapping that points at an archived
account — see [Parked mappings](#parked-mappings)). This section is the
canonical description of the first two; the endpoint tables above only point
here.

### Archived accounts

`PUT /api/v1/system/provider-config/{provider_id}/{account_id}` with
`{"archived": true}` archives the account (`archived=true`, forced
`enabled=false`) and **evicts its cached credential**, so a retired account
holds nothing live and nothing refreshable. The stored credential (`api_key`,
`session_cookie`) stays, so `{"archived": false}` makes the account usable again
immediately — a machine-reported credential returns with the machine's next
push.

A retired account holds no credential at all: the secret is kept nowhere — the
machine's next push records the source but stores no secret for a retired
account, and any copy at rest is retired with the account.

`DELETE /api/v1/system/provider-config/{provider_id}/{account_id}` soft-archives
the account and clears any matching `credential_tags` hints.

### Merged-away accounts

`POST /api/v1/system/provider-account-merge/apply` moves one
account's usage history, quota cards, credential-source rows, operator tags and
**its credential** to the destination, and retires the source id — a machine
that still claims it has its credential filed under the destination, not under a
resurrected account. `preview` reports what would move and how many rows
collide; `apply` performs the merge (`confirm_collisions` to accept the
deduplication, `confirm_shared_default` to merge with the shared `default`
identity). It is refused when either side was already merged away.

An account that was **merged away** is not an account any more: the merge
moved its history and its credential to the destination, so it holds nothing.
The row the merge left behind stays in `accounts` (archived, no credential) and
carries `merged_into`, so a client can tell a merged-away account from an
account that was merely archived; the provider's `merged_accounts` lists every
merged-away account with the account it became (`account_id`,
`destination_account_id`, `merged_at`; a chain of merges is reported at its
end). Settings → Providers lists them under "Merged accounts", where the
un-merge is offered: they cannot be restored by a plain write.

**A merged-away account cannot be restored or given a credential**: any write
that would make it an account again — `{"archived": false}`, a credential write,
or a PUT that creates the row — is refused with `409`, and the message names
the account it went to. Restoring it would resurrect a phantom (the merge
moved its history and its credential to the destination). A merge that kept
its journal of what it moved can be put back instead — see
[Un-merge](#un-merge).

### Un-merge

`POST /api/v1/system/provider-account-unmerge/apply` undoes one
merge: the rows the merge moved go back to the account they came from, the
credential sources, operator tags and durable contributions follow, the
provider row is restored and the merge record is deleted — the retired id
names an account again. `preview` reports what goes back, what cannot (the
duplicates the merge dropped, the stored credential it wiped) and the rows that
got in the way; `apply` performs the un-merge (`confirm_collisions` to accept
dropping a row that got in the way, `previewed_collisions` the count the
preview showed — the rows may not have changed since). It is refused (`409`)
when the merge has no journal, when the destination was merged away too (a
chain is unwound from the end), when the rows in the way changed since the
preview, or when a row is in the way and the collision was not confirmed;
`404` when no merge is recorded for the account.

The display label of an account — and the billing model derived from the cards
it publishes — is described under
[Automatically discovered Claude accounts](#automatically-discovered-claude-accounts).

## Behaviour notes

### Source sync state

`GET /api/v1/fleet/sidecars` (and `GET /api/v1/fleet/sidecars/{id}`) report, on an enrolled `device-<hex>` entry, whether the device is completing `POST /api/v1/fleet/source-sync`:

| `source_sync_state` | Meaning |
| --- | --- |
| `ok` | An accepted sync in the last hour. |
| `pending` | Enrolled under 10 minutes ago and not synced yet. |
| `never` | Older than that and no accepted sync, ever. |
| `stale` | Synced before, but not for over an hour. The sidecar syncs every push cycle, so an online device in this state usually means the route started failing afterwards. |

`source_sync_last_at` is the time of the last accepted sync (`null` for `pending` and `never`). Both fields are `null` for a legacy sidecar and a revoked device. Only an accepted `source-sync` request counts; ingest does not. A proxy redirecting that route therefore shows up as `never` or `stale` (see [forward-auth.md](forward-auth.md#verify-the-bypass)). The Fleet card shows a notice for an online device in either state.

### Signed fleet request retries

Authenticated fleet ingest and signed config requests consume a durable, single-use signature receipt before handler effects. Reusing the same envelope returns HTTP `409`
(`detail.error: "replayed_request"`), including after handler validation errors. Receipt storage failures return `503`. Generate a fresh timestamp and HMAC for every retry; the signing
format is unchanged. Timestamps must be finite and no more than five minutes in the past or one minute in the future. Event identifiers continue to deduplicate retried usage events.

### Fleet login-directory additions

`PUT /api/v1/fleet/sidecars/{sidecar_id}/settings` accepts optional
`claude_config_dirs` and `codex_home` arrays (maximum 32 directories each, 4096
characters per path). Omission preserves the field; `null` or `[]` clears only
that Fleet addition. Absolute POSIX/Windows and home-relative paths are accepted;
control characters, glob patterns, relative paths and known credential-file
names are rejected. Paths are evaluated only by the sidecar.

Fleet rows expose `login_dirs_desired`. Check-in responses carry `login_dirs`:

```json
{"claude_config_dirs": ["~/work-claude"], "codex_home": ["C:\\Users\\me\\codex"]}
```

The first ingest batch may include `login_dirs_report`; omitted reports preserve
the previous value. Fleet and credential inventory machine rows expose this
report (`null` means the sidecar has not reported support):

```json
{
  "applied": {"claude_config_dirs": ["~/work-claude"], "codex_home": []},
  "searched": {"anthropic": ["/home/me/.claude", "/home/me/work-claude"], "chatgpt": ["/home/me/.codex"]}
}
```

`applied` acknowledges Fleet settings; `searched` lists resolved directory
candidates, including missing locations. Acknowledgement does not imply that a
credential exists or that the directory is readable.

When custom Claude/Codex roots are active, events include optional
`credential_origin` (for example `path:/home/me/work-claude/projects`). Each
root's identity and watermark are independent. Unidentified root events use only
an exact origin mapping, never a legacy provider-wide mapping. Pending-event
assignment saves the root mapping; older events without this field keep the
existing provider-wide behavior.

### Credential-file migration notice

`GET /system/credentials` includes `credential_file_migration_notice`, either
`null` or `{providers: [{provider_id, name}], removed_count, recorded_at}` for
confirmed legacy server credential-file removals. It contains no credential
values or file paths. `POST /system/credentials/migration-notice/dismiss`
requires administrator access and returns `{status: "ok"}`. The dismissal is
idempotent, persists for the installation, and is not reset by later removals.

### Kimi CLI device identity

Kimi Coding sidecar token cards may include `metadata.cli_device_id` alongside
`cli_access_token`. It is trimmed printable ASCII, at most 256 characters,
and is accepted only for a `kimi_coding` CLI bundle. It is device metadata,
not an authentication value, and is never returned by the credential inventory.


### Parked mappings

An archived account can no longer collect — a provider whose accounts are all
archived runs no collector — but the machine that reported its credential keeps
reporting the origin. The operator can map that credential onto the archived
account; the mapping is then **parked**.

A parked mapping:

- **resolves the origin** — the credential stops appearing in
  `GET /api/v1/fleet/credentials/tags/pending`, drops out of
  `unmapped_count` and out of `blocked_collection`, and is not re-opened as
  pending by the next sidecar manifest;
- **is never acted on** — it is not shipped in `GET /fleet/config`'s
  `account_tag_hints` nor in the manifest's `resolved` map, it never stamps
  the archived account with incoming usage (neither with a card nor with a
  usage event), and it never moves the cached credential into the retired
  account's slot. A usage event that only the parked mapping could attribute
  is held for assignment in `GET /api/v1/fleet/events/pending` instead of
  being booked on the retired account;
- **comes back to life on restore** — parking is derived from
  `provider_configs.archived`, never stored on the tag, so restoring the
  account (`PUT /api/v1/system/provider-config/{provider}/{account}` with
  `{"archived": false}`) ships the hint again on the next `/fleet/config` and
  collection resumes without re-tagging.

A parked credential stays visible in `GET /api/v1/system/credentials`, under the
account it is parked on; that account carries `archived: true`. The credential
is **inert**: a retired account holds no credential at all — the source record
stays (the operator still sees that a machine reports it), but the secret is
kept nowhere, not in the cache and not at rest, and Token Health does not list
it.

A *redirect* tag (one with `target_provider_id`) names an account of its
**target** provider, so whether it is parked is looked up under that target
provider — a retired target account stops receiving the redirected credential
as well.

`POST /api/v1/fleet/credentials/tags` reports which kind of mapping it wrote
as `target_archived`.

The archive / merge / un-merge semantics around a parked mapping are described
under [Account lifecycle](#account-lifecycle).


### Automatic Claude browser connections

A pending sidecar browser source for `anthropic` (Subscription) or
`anthropic_console` (Console API credits) is verified against its own cookie.
Subscription identity is the authenticated account email; Console identity is
the organization UUID. A sole eligible organization is selected automatically
only after a complete verification. Multiple eligible organizations require a
choice unless the verified account already has a saved organization binding.
An organization with a JSON permission denial is excluded independently;
challenges, timeouts, rate limits, and malformed responses prevent automatic
selection. Existing disabled or archived accounts stay disabled or archived.

`GET /api/v1/fleet/credentials/tags/pending` adds these safe fields to its items — `origin_label`, `origin_app` and `origin_detail` name the credential and the tool that owns it:

```json
{"source_id":"sidecar:<stable hash>","selection_required":true,"organization_count":2}
```

`source_id` is present for Claude providers and is derived from the exact
sidecar and credential origin, including the cookie fingerprint. Other providers
omit it. The organization count starts at zero before verification. Organization
metadata is nonsecret; cookies remain only in the existing source-pinned cache.

Admin `GET /api/v1/fleet/credentials/{source_id}/claude-organizations?provider_id=anthropic`
(or `provider_id=anthropic_console`) rechecks the pending source and returns:

```json
{"organizations":[{"id":"11111111-1111-4111-8111-111111111111","name":"Personal"}],"complete":true,"selection_required":false}
```

Only organizations with usable quotas or a valid Console credit balance appear.
`complete:false` means an organization could not be conclusively evaluated; a
single returned organization must not be treated as an automatic choice.

Admin `POST` to the same path accepts:

```json
{"provider_id":"anthropic","organization_id":"11111111-1111-4111-8111-111111111111"}
```

The server verifies membership and quota/billing access using that exact pending
source, saves `ProviderConfig.claude_organization_id`, promotes the source, and
returns `{"status":"ok"}`. This nullable configuration field is also returned
by the provider-config listing. Missing sources or a mismatched source/provider
return 404; invalid provider IDs or malformed UUIDs return 422; inaccessible
organizations return 403; changed or already assigned sources return 409.
Upstream failures use sanitized messages and never include cookie values. Both
organization routes require the normal admin authentication policy. Existing
manual Console setup remains available below.

### Claude Console connections

Settings groups Subscription quotas (`anthropic`) and Console API credits
(`anthropic_console`) under Claude. Each connection has its own credentials,
collection health, and dashboard card. Console balances use the existing PAYG
currency layout; the reported total includes purchased and promotional credits.
`credit_breakdown` explains those credits without increasing the total. Grant
usage is `granted_value - remaining_value`; the total has no quota percentage.

`POST /api/v1/system/provider-config/console-organizations` accepts
`{"session_cookie": "<Console sessionKey or Cookie header>"}` and returns
`{"organizations": [{"id": "<organization UUID>", "name": "<organization name>"}]}`.
Only API-capable organizations are returned. An empty list means no accessible
API organizations, not a zero credit balance.

For account preview, send `provider_id: "anthropic_console"`, `session_cookie`,
and `organization_id` to `/api/v1/system/provider-config/preview-account`.
Successful preview reports `label_source: "organization"`; membership and
billing access are verified. Save to
`PUT /api/v1/system/provider-config/anthropic_console/{organization UUID}` using
the usual `session_cookie` field. Organization identity remains stable across
cookie replacement and display-label changes. API responses expose only the
credential's configured status. Clear it using `clear_session_cookie: true`.


### Automatically discovered Claude accounts

A Claude account the server discovers for itself — a sidecar console whose
cookie proves which organization it belongs to — is named after what it
actually is instead of the placeholder "Default". A subscription belongs to one
member, so its email names it; a Console account *is* the organization, because
every member shares its credit, so the organization's name names it.

The name is written to `ProviderConfig.account_label` when the account row is
created, and an `account_label` nobody set is filled in on a later poll. A
label the operator chose is never overwritten — the operator's own label is
written with
`PUT /api/v1/system/provider-account-label/{provider_id}/{account_id}`.

A card's label is the account's *name* — the member's email for a claude.ai
account, the organization's name for a Console account — and it is what keys the
card to its account, so a name a human chose never files a card under another
account. The *identity* a card proved, which an organization-scoped one marks
with `org: `, lives in the card's detail, where the tagger reads it back.

The billing model is read off the shape of the cards the account publishes: a
card reporting a percentage of a limit describes a subscription, a card
reporting a currency balance describes pay-as-you-go, and a quota card wins
when an account publishes both. A card may also name the kind of evidence it
is: `"placeholder"` for a window the collector invented, `"prepaid_credit"`
for a claude.ai account's prepaid balance. Such a card decides nothing, so an
account that published only a balance proves no model at all.

The billing model is written to `ProviderConfig.billing_type` when the
account row is created, and a `billing_type` nobody set is filled in on a
later poll. A model the operator chose is never overwritten — change it in
Settings, and no later poll undoes that. `ProviderConfig.billing_type_source`
says who wrote it: `"operator"` for the operator's own choice, `"detected"`
for a proof the server reached, and empty when nobody has decided yet.

An account added **by hand** is priced the same way: the preview returns the
model it proved as `suggested_billing_type` (a Console connection is priced
while the organization is picked; a subscription is priced on request with
`detect_billing: true`, which takes either a Claude web session or an OAuth
token), and the wizard offers it as a suggestion. The operator's own choice
wins; a model nobody chose is left to the server's proof.

### Native Claude OAuth token metadata

Sidecar token cards may additionally carry `oauth_read_only: "1"` and
`oauth_account_uuid` in `metadata`, alongside `oauth_token` and `expiry_date`
(milliseconds since Unix epoch, encoded as a string). These fields use the existing
signed fleet ingestion endpoint; no new endpoint or database migration is required.
The UUID is a claim to verify against the holder profile, not an account assignment.
Read-only native sources cannot invoke OAuth refresh. Stable machine-bound origins
identify each Desktop cache slot or CLI Keychain service; changed inferred credentials
must re-verify their holder and quota. Refresh secrets are not forwarded by either
new Keychain discovery rule. Ordinary credential-file fallback remains supported.

## Local sidecar Keychain source settings

**These are the sidecar's own loopback routes**, served by the sidecar process
on the machine that holds the credentials. They are *not* server routes: they
have no `/api/v1/` prefix, the server never exposes them, and they are
documented here only because the server-side protocol below interacts with
them. See [Sidecar Documentation](sidecar.md) for the sidecar's full API.

The loopback sidecar settings UI exposes GET /sources and POST /sources, with the
existing exact Host and same-origin POST checks. These are local settings routes,
not server-side authorization endpoints. GET is inert and returns only catalog
labels, capability revisions, read intent, state, renewal support, dependency and
new-scraper announcement state. It never returns credentials or opens native UI.

POST accepts action plus source_id for enable, disable, dismiss or authorize;
enable_all and disable_all operate on currently known sources. The renewal
action accepts an exact boolean value and requires Claude Code read consent first.
Authorize is an explicit local, bounded, single-flight packaged-app setup attempt.
Unknown actions/IDs and malformed requests fail without credential IO.

Local config stores keychain_sources entries with exact-boolean read, catalog
revision, monotonic generation and approved destination. The separate
claude_keychain_renewal permission defaults off. keychain_announcements records
dismissed revisions; keychain_withdrawals records pending source generations.
Config replacement is atomic and owner-only on macOS. File/env collection is
unaffected; Windows Credential Manager is outside this macOS permission model.

Until #783's authenticated source lifecycle is implemented, newly opted-in
Keychain credential cards and matching manifest observations are withheld from
server forwarding. Existing server credentials are not silently revoked by a
local checkbox. See #780 and [source-consent.md](source-consent.md) for the full
consent, withdrawal, remote-control and acceptance contract.


## Fleet Keychain source lifecycle (protocol 1)

Updated sidecars request device_protocol=1 and platform=Darwin|Linux|Windows
when redeeming a pairing code. The platform is bound to the enrolled identity;
ordinary ingest OS labels cannot change it. Linux/Windows browser cookies do
not require macOS Keychain consent, but retain their canonical scraper identity
and server-owned generation so pause/revoke still applies and replay cannot
restore withdrawn credentials. An explicit proof, when present, is always
validated. The paired platform is a device assertion, not OS attestation;
local Keychain permission is enforced by the sidecar, and server lifecycle
eligibility is enforced independently of platform. Known Windows Credential Manager
origins (`github` / `keychain:git:https://github.com` and `antigravity` /
`keychain:gemini:antigravity`) retain existing collection on Windows without
macOS consent. They have stable Fleet lifecycle IDs (`github.windows-credential`
and `antigravity.windows-credential`), state rows, server-owned generations and
pause/resume/revoke/replay enforcement. These descriptors are excluded from local
Keychain opt-ins, authorization and announcements. Windows setup/dismiss commands
are unavailable; reconnecting a revoked Windows contribution requires re-pairing.
Unknown native origins are skipped.
Older enrollments without a platform remain
conservative; re-pair to declare their platform.
When DB_ENCRYPTION_KEY is configured, the single-use response additionally
contains device_id, device_key, device_api_url, and source_protocol=1.
The random device key is encrypted in server storage and saved in the sidecar's
owner-only config. Hostnames are labels. Without server encryption, pairing
retains its legacy response; Keychain forwarding remains Limited until enrollment.
Re-pairing creates a new device; it does not claim a hostname's previous sources.
An authenticated source-sync 401 surfaces a local re-pair-required notice. The
sidecar never downgrades that identity to shared-key authorization to bypass a
lost or revoked enrollment; Keychain egress remains withheld.

Device requests send X-Runway-Device-Id, X-Timestamp, and X-Signature.
HMAC-SHA256 signs DEVICEv1|device_id|timestamp|purpose| followed by raw body
bytes (raw query bytes for GET config). Purposes are ingest, manifest,
config, and source-sync. This binds the operation without relying on a
reverse proxy preserving the public path. Existing timestamp limits, body caps,
and durable replay protection apply. A shared ingest key cannot authenticate a
device header or claim a reserved device-<32 lowercase hex> registry identity.

| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| `POST` | `/api/v1/fleet/source-sync` | Device HMAC | Device-authenticated status and ordered command acknowledgment (a forward-auth proxy must let it through, or a paired Mac withholds its Keychain credentials: [forward-auth.md](forward-auth.md#verify-the-bypass)). The body contains protocol=1, sources, and acknowledgments ({id, status: applied\|denied\|superseded}). Source reports contain source_id, provider_id, label, revision, generation, literal boolean read, state, new, renewal_requested, renewal_supported, and optional last_read_at / last_renewed_at Unix timestamps. Labels are canonical; unknown scrapers are not accepted. The response lists accepted scraper IDs, withdrawals ({source_id, generation}), and pending commands ({id, source_id, generation, action}). Up to 256 pending commands are delivered in order per poll. Acknowledged commands are retained for 30 days, always keeping each scraper's latest ordering barrier; repeated acknowledgments after cleanup are harmless. Reports from an older generation update contact time without changing consent |
| `GET` | `/api/v1/fleet/sidecars/{sidecar_id}/sources` | Admin | Admin-authenticated, inert status. Returns protocol=1, sources (FleetSourceStatus), and pending commands. Source status extends the report with remote_paused, revoked, report_at, report_stale, announcement, renewal_capable, collection_health (withdrawn\|paused\|healthy\|action_required\|untried), and last_collection_at (ISO UTC or null). Stale reports never expire eligible server credentials. `report_at` / `report_stale` describe source reporting, independently of the device registry's `last_seen` / connectivity and server collection health. macOS sources send periodic consent/status reports. Windows Credential Manager sources update source contact when credentials are submitted; heartbeat-only ingest and empty source-sync requests do not refresh it. A Windows source report can therefore be stale while the device is online and server collection is healthy. This does not require another local read or invalidate its credentials |
| `POST` | `/api/v1/fleet/sidecars/{sidecar_id}/sources/{source_id}/commands` | Admin | Admin-authenticated {action}, one of pause, resume, revoke, request_setup, or dismiss. Returns {id, status: pending}. Pause/revoke immediately withdraw that source's server credentials. Resume requires current local read consent and a matching non-revoked generation; it only clears a server pause after acknowledgment. Setup requests create a local notice only. Dismissal is scoped to the current catalog capability revision |

Native credential cards and manifest entries carry keychain_consent with
source_id, revision, and generation. The server checks device, provider, origin,
revision, and live generation before accepting a contribution. Withdrawal persists
a tombstone and removes only matching encrypted credential payloads; late ingest
cannot recreate them. Re-enabling requires a newer local generation.
Rejected or unknown source cards/manifest entries are skipped before metadata
mutation, without discarding the rest of the upload. Ingest returns a sanitized
credentials_rejected card count (not a token count). Skipped manifest sources cannot retire last-good
origins through a completed-provider declaration.
Credential bundles remain separate across devices and origins, including identical
tokens. Enrolled contributions survive server restart and offline Macs without a
heartbeat lease. Provider expiry/rejection remains authoritative. Historical usage
and manual/file contributions are retained. An already admitted provider request
cannot be recalled; subsequent source selection observes committed withdrawal.

Older servers never receive newly opted-in Keychain credentials: forwarding requires
successful device-authenticated protocol negotiation. Legacy clients retain their
shared-key behavior and cannot control enrolled-device sources.

Pausing removes a contribution from selection but retains its encrypted bundle.
An acknowledged, locally permitted resume can reuse that known bundle without
a new credential read. Revocation and local opt-out erase the matching bundle
and retain the consent-generation tombstone.

### Resumable Claude cloud verification (operator only)

`POST /api/v1/system/debug/claude-cloud-verification` uses existing admin,
Host and CSRF protections. Maximum body: 256 KiB, no duplicate keys or unknown
fields; six actions per minute. GET is inert. This is a separate protocol from
the existing bounded `/debug/claude-cloud` sample; its v1/v2 behavior is unchanged.

Actions:

- `{"action":"start","account_id":"…","source_id":"…"}` records one job
  using the exact eligible cached source. Optional `organization_id`, `session_id`,
  `declared_category`, `job_key` and `expected_local` are strict inputs. Start
  performs no provider requests. A `session_id` matches the listed id with or
  without a `cse_` or `session_` prefix — an exact listed id wins over a
  prefix-equivalent one; the list returns `cse_…` and claude.ai shows `session_…`.
  This mirrors the primary client's observed behaviour, not an API guarantee.
  Expected local fixtures contain at most 256
  reduced assistant records, strict numeric usage, and identities HMACed with the
  supplied 32-byte hex job key. Never submit transcripts, tokens or raw message IDs.
- `{"action":"step","job_handle":"…"}` explicitly advances one batch:
  at most five GETs, 30 seconds, 4 MiB total and 1 MiB per response. Each step
  rechecks profile/account/org and local source authority. The required session
  list is exhausted before the selected session's history. No automatic history
  sweep, retries, fallback, token refresh or collector activation.
- `{"action":"status"}` reads the current durable job without provider I/O;
  optional `job_handle` selects the exact job. Local source/consent authority is
  rechecked before returning evidence, without changing an active checkpoint.
  `last_verified_at` is the last
  successful profile check, not a fresh status lookup. No job returns
  `{"state":"absent"}`. If a checkpoint is unreadable (including an unavailable
  encryption key), status returns `{"state":"unavailable","job_handle":"…"}`
  so an admin can explicitly cancel it; status never deletes or replaces it.
- `{"action":"cancel","job_handle":"…"}` removes the private checkpoint
  between steps and returns `{"state":"cancelled"}`. An active lease returns
  409 rather than claiming cancellation before the bounded step ends.

- `{"action":"archive_status","job_handle":"…"}` reads local administrative
  metadata only and returns `state` (`terminal`, `not_terminal`, or `archived`),
  exact transport `job_handle` and numeric `revision`. It performs no source
  credential operations, provider requests or mutation. Historical usage is not
  exported, even when the original source has become unavailable.
- `{"action":"archive","job_handle":"…","revision":7}` explicitly preserves
  that exact terminal checkpoint and releases the current-job slot atomically.
  It returns `{"state":"archived","job_handle":"…","revision":7}` only after
  verifying the unchanged encrypted copy and committing. A mismatched revision,
  nonterminal job, active lease, or full archive returns 409. Retrying the same
  archived handle/revision is idempotent and cannot affect a newer current job.
  Archiving performs no provider or source credential operations. It never runs
  implicitly during start/status/GET. Preserve a verified owner-only database
  backup before live archival. Transport handles stay out of published evidence.

Only one current encrypted job exists at a time; up to 64 terminal encrypted
archives, capped at 64 MiB of ciphertext, remain recoverable locally with no
automatic deletion. Both limits must permit a new archive. Each checkpoint accepts
at most 16 MiB of plaintext /32 MiB of ciphertext; the combined archive byte limit
prevents multiplying that worst case by 64. SQLite pages, WAL and backups add
physical storage overhead beyond the logical ciphertext cap. A fresh start generates a new handle and, by default, a new job key.
Operators supplying `job_key` for fixture HMACs must generate a new key per job.
Source binding and request/byte/time budgets are recomputed for the new job;
old intent/cursor state is never resumed or rebound.

Configured server encryption is required;
there is no plaintext fallback. Every provider attempt is charged and journaled
before dispatch. Clean batch boundaries resume; an ambiguous in-flight intent
stops as interrupted and cannot replay. A job is limited to 512 attempts, 256 MiB,
20,000 listed sessions, 100,000 history records and 30 minutes. Terminal failures
never start another exchange. Fixed 409/404/503 error categories expose no values.

Owner-only recovery and pruning are maintenance operations, not API exports:

1. Stop Runway and make an owner-only SQLite backup (directory mode 0700, file
   mode 0600). Verify `PRAGMA quick_check` and preserve the original encryption
   configuration securely; never print or send its key.
2. `CheckpointStore.archived_snapshot(handle)` is intentionally internal for
   local operator recovery/verification with that configuration. Keep decrypted
   state in memory; compare fixed counters or encrypted envelopes without
   serializing state, source identifiers, keys, cursors or transcript data.
   An archived checkpoint is historical evidence, never a resumable job.
3. If capacity is full, select the exact archive and revision locally. Before
   deletion, verify that the backup contains the identical revision and encrypted
   envelope. In one explicit SQLite transaction, use parameterized
   `DELETE FROM claude_cloud_verification_archives WHERE job_handle=? AND revision=?`
   and require exactly one affected row before committing. Roll back on mismatch.
   Never prune the current `claude_cloud_verification` table. The verified backup
   must remain retained and readable with the original encryption configuration.
4. SQLite can reuse freed pages without shrinking the file. Optional offline
   `VACUUM` needs adequate free disk space and a retained verified backup; it is
   never run automatically. Restart only after integrity checks pass.

The strict report contains `format_version: 1`, opaque transport `job_handle`,
`started_at`, `deadline`, `last_verified_at`, `phase`, `outcome`, `requests`,
`accepted_response_bytes`, `http_status`, account/org verification booleans,
`listed_sessions`, `list_pages`, `list_complete`, history page/event/completion
counts, numeric `sequence` diagnostics, SDK-envelope `audit`, `comparison`,
`subagent_comparison`, fixed `scope_observations`, `declared_category`,
`execution_kind` (`other` is an unrecognized label; `unknown` is absent) with its
lower-snake `environment_kind` label (read from the list row's top-level
`environment_kind` field, falling back to a nested `environment.kind` only when
that key is absent; null when neither is a valid label; absent in reports from
older builds, which the evaluator still accepts), optional `rejected_list`. Comparisons report input/cache overlap only; children remain
separate from main-agent totals. Execution metadata is an observation, not proven
provenance. No provider/account/message IDs, model names, source IDs, cursors,
HMACs, salt, transcript content or raw errors leave the report. A checkpoint written
with `environment_kind` cannot be resumed by a build without the field (the checkpoint
models reject unknown fields), so finish or cancel a job before rolling back to an
older image; the same holds for the `equivalent_session` field a job records when only a
prefix-equivalent id is listed for its target. A checkpoint from before the field resumes
here with the label taken from a known `execution_kind`.

All `cloud_contract_verified`, `ready_for_ingestion`, `live_acceptance_passed` and
`provenance_verified` flags remain false. Complete pagination is not complete
accounting or coverage acceptance. Remove `job_handle` before retaining evidence.
The standalone `scripts/verify_claude_cloud.py` accepts the resulting report in an
**envelope version 2**, alongside immutable `source_commit`, `image`, `observed_at`
and `deployment`. Existing envelope version 1 remains unchanged. Version 2 always
returns `stop` with remaining accounting/coverage/provenance checks; supplied
build metadata is not authenticated by the offline validator.

Complete-history verification follows nonempty continuation cursors even on short
pages, for both the session list and event history. A valid page with an omitted `next_cursor` ends the observable traversal,
just like an explicit null. When `has_more` is present it must be boolean and
agree with continuation; contradictions stop as `unsupported_contract`.
Completion is never inferred from record count. Empty strings, malformed cursors,
invalid records, duplicates and repeated cursors remain rejected. This accepts
the authenticated terminal shape observed under #976; it does not establish a
published global API contract, a stable snapshot or coverage of every Claude
surface. The legacy v1/v2 sample parser remains unchanged.

Diagnostic format1 retains its frozen `cursor_supported` sample-parser rule,
including false for omission. This preserves historical encrypted reports and
standalone evidence. That bit does not describe the verification adapter's
additional terminal handling or whether the whole response is valid.

The verification projection additionally reports optional `rejected_list` metadata
for a session-list response rejected by the current parser. It is null for older
checkpoints, successful pages, and unavailable or changed local authority. The
worker captures it from the response already in memory, without another GET or
changing parser acceptance, request budgets, or retry rules. It contains only
`boundary` (the fixed list categories below, or null when JSON cannot be decoded),
`response_encoding`, `json_state`, `duplicate_json_keys`, and
`nonfinite_json_values`. A verified rejected response remains
`unsupported_contract`; these observations never enable ingestion or establish
a supported provider contract. Historical checkpoint evidence stays encrypted
locally when a changed source suppresses the public projection.

See [the #956 design and acceptance boundaries](claude-complete-history-verification-956.md).

### Failed Claude list-boundary diagnostic (operator only)

`POST /api/v1/system/debug/claude-cloud-list-boundary` accepts only
`{"job_handle":"…"}` for an existing encrypted verification checkpoint stopped
with `unsupported_contract` during listing, with holder/org already bound and
no completed list or history pages. It inherits admin, Host and CSRF protections,
rejects duplicate/unknown keys, caps the body at 8 KiB and rate at two requests
per minute. GET and ordinary status polling never dispatch it.

The separate explicit diagnostic re-reads the recorded failed cursor. The original
verification remains stopped with its existing request count and budget. It
performs at most two GETs (profile and the recorded failed
list cursor) within 20 seconds, 1 MiB per response and 2 MiB total. It requires
the exact cached source/token/consent authority digest and holder/org, with
source and checkpoint checks before each provider operation and publication.
There is no alternate source, refresh, native discovery, history sweep, model
call, automatic retry, checkpoint mutation or reset of the original budget.
An unavailable/changed source or checkpoint stops before further operations.

The fixed `format_version: 1` report returns `outcome`, `stage`, `requests`,
`http_status`, verification booleans, `original_list_pages`,
`original_listed_sessions` and optional `boundary`. The original prefix counts
are historical observations from an eligible, readable checkpoint, including on
later source/provider failures; unreadable/ineligible counts remain `null` rather
than a misleading zero. An initial known token expiry reports `expired`; later
eligibility changes remain `source_changed`. Only outcome `observed` can
have boundary evidence. Its fields are top-level/data/ID type categories, total
and inspected record counts (at most 20 inspected), valid/invalid/duplicate and
previously seen ID counts, cursor category (`missing`, `null`, `empty_string`,
`nonempty_string`, `invalid`), `cursor_supported`, optional `cursor_repeated`,
and `has_more_state` (`missing`, `true`, `false`, `invalid`). `response_encoding`
reports `unknown`/`identity`/`unsupported`; `json_state` distinguishes unattempted,
decoded, duplicate keys, nonfinite constants, malformed JSON, encoding errors and
nesting limits, with bounded duplicate/nonfinite counts. The existing strict
transport/JSON rejection policy remains in force. Unknown evidence
remains distinct from false or empty. `cursor_supported` describes the current
parser, not an established provider contract. Unknown keys and values are never
returned; raw identifiers, cursors, HMACs, credentials and content remain private.

All acceptance, provenance and ingestion flags remain false. The standalone
validator accepts this report in **evidence envelope version 3**, with the same
immutable source/image/observation/deployment metadata as the earlier formats.
It returns STOP with `list_contract_requires_review`. Existing sample/history
evidence envelopes 1 and 2 remain unchanged. A caller must separately establish
concrete exact-build readiness and a bounded intent before any live diagnostic.

Verification `step` responses also perform the same local-only authority recheck
as status before publishing cached evidence, including terminal/limit/recovery
returns. Repeating a terminal step never makes another provider request.

A separate inspection observes the provider at the time of that request; it cannot
retroactively establish the exact contents or cause of an unretained failed page.
