# API Reference

All API routes are under `/api/v1/`.

## Usage & history

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/usage/limits` | All current quota cards (instant, from in-memory registry) |
| `GET` | `/api/v1/usage/sources` | Distinct sidecar IDs with message usage events |
| `GET` | `/api/v1/usage/fleet` | Fleet HUD: critical gauge + secondary limits per (provider, account), with `window_aggregations.longest` per-model/per-sidecar splits, plus `blocked_sources` (credential sources a machine could not read) |
| `GET` | `/api/v1/usage/cumulative` | Usage totals across sidecars (lifetime/year/month/day); `since`/`until` narrow to a window and optional `sidecar_id` scopes to one source |
| `GET` | `/api/v1/usage/forecast` | Theil-Sen projection to reset; `include_series=true` returns drill-down points |
| `GET` | `/api/v1/usage/history/windows` | Paginated closed quota windows |
| `GET` | `/api/v1/usage/history/snapshots` | Paginated snapshot rows with per-series delta |
| `GET` | `/api/v1/usage/history/chart` | Time-series data for percent/tokens/cost visualisations; optional `sidecar_id` scopes token/cost bars, while `group=provider` collapses bars to one segment per provider |
| `GET` | `/api/v1/usage/history/window-detail` | Fill-up series + by-model breakdown for one window |
| `GET` | `/api/v1/usage/history/deltas` | Event-sourced consumption deltas; optional `sidecar_id` scopes to one source |
| `GET` | `/api/v1/usage/events` | Recent event tail for (provider, account) |
| `GET` | `/api/v1/usage/events/range` | Earliest/latest event timestamps for (provider, account), optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/window-history` | Closed-window history with per-model & per-sidecar splits; optional `series_model_id` and `series_variant` scope to a quota series and retain legacy unscoped rows; for scoped requests, an omitted/empty variant means `default` |
| `GET` | `/api/v1/usage/heatmap` | 7×24 hour-of-day activity grid; optional `sidecar_id` and `exclude_cache` filters |
| `GET` | `/api/v1/usage/sessions` | Top-N sessions (`sort_by=tokens` or `recent`), optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/sessions/paginated` | Paginated session browser with server-side sort (`sort_by=recent\|tokens\|duration\|messages\|cost`, `sort_dir=asc\|desc`) and optional `project` / `sidecar_id` filters |
| `GET` | `/api/v1/usage/projects` | Distinct project names seen in events, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-projects` | Top projects ranked by `metric=tokens\|cost\|sessions`, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-tools` | Top tools by invocation volume, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/top-models` | Cross-provider Top Models ranked by `metric=tokens\|cost`, optionally scoped by `sidecar_id` |
| `GET` | `/api/v1/usage/source-apps` | Client apps that produced events, ranked by `metric=tokens\|cost`; several apps can share one provider, so this is what says which app spent the quota. Optionally scoped by `provider_id`/`account_id`/`sidecar_id` |
| `GET` | `/api/v1/usage/global-stats` | Global cross-provider snapshot; optional `sidecar_id` and `exclude_cache` filters |
| `GET` | `/api/v1/usage/cost-forecast` | MTD cost + 7-day burn extrapolated to EOM; optional `exclude_cache` |
| `GET` | `/api/v1/usage/anomalies` | Z-score spike detection vs. historical mean; omitted/empty `sidecar_id` selects the all-source rollup, while a non-empty ID selects that source |
| `GET` | `/api/v1/usage/archived-providers` | Lifetime stats for archived providers, kept out of the main fleet view |
| `POST` | `/api/v1/usage/reset/{provider}` | Clear terminal failure state for a provider |
| `POST` | `/api/v1/usage/collect/{provider}` | Force immediate re-collection for one provider |

For `/usage/cumulative`, `by_sidecar` normally contains per-source totals alongside
the all-source total. When `sidecar_id` is supplied, the top-level bucket is
scoped to that source and `by_sidecar` contains that source as a single-key map.
Lifetime token totals and `cache_hit_ratio` in `/usage/global-stats` retain the
cache-inclusive components; `exclude_cache` applies to session averages and peak
token metrics, while the UI adjusts the lifetime token/cost tiles from their
component fields. Cache-specific metrics remain cache-inclusive by definition.

## Fleet / Ingestion

| Method | Route | Description |
|--------|-------|-------------|
| `POST` | `/api/v1/fleet/ingest` | Push metrics + events from a sidecar (HMAC-SHA256 signed, 600/min/IP) |
| `GET` | `/api/v1/fleet/sidecars` | List all registered sidecars |
| `GET` | `/api/v1/fleet/sidecars/{id}` | Single sidecar details |
| `PATCH` | `/api/v1/fleet/sidecars/{id}` | Update custom name or tags (admin) |
| `DELETE` | `/api/v1/fleet/sidecars/{id}` | Remove sidecar from registry (admin) |
| `POST` | `/api/v1/fleet/sidecars/{id}/pause` | Pause collection on a sidecar (admin) |
| `PUT` | `/api/v1/fleet/sidecars/{id}/settings` | Override the fleet-wide auto-update flag and/or update channel for one sidecar, or set its log level — body `{"auto_update": true\|false\|null, "update_channel": "stable"\|"beta"\|"edge"\|null, "log_level": "DEBUG"\|"INFO"\|"WARNING"\|"ERROR"\|null}` (the level is case-insensitive; an unknown one is `422`; delivered as `log_level` on the ingest response, `null` = the sidecar's own level, applied without a restart; the list adds `log_level_desired` and the sidecar-reported `log_level`). A key that is left out stays as it is; `null` clears that override so the sidecar follows the fleet again. An unknown channel is `422`. Delivered as `sidecar_auto_update` / `sidecar_update_channel` on its next ingest response; the response echoes the overrides and the effective values. The sidecar list adds `auto_update_desired` / `update_channel_desired` (the overrides), `effective_auto_update` / `effective_update_channel` (what it is told) and `auto_update` / `update_channel` (what the sidecar reports; `null` = older sidecar) (admin) |
| `PUT` | `/api/v1/fleet/sidecars/{id}/keep-alive` | Turn a sidecar's keep-alive (renewal of the agy, Claude Code, Codex and xAI logins) on or off remotely — body `{"enabled": true\|false\|null}`, `null` defers to the sidecar's own flag and the fleet-wide default (`sidecar_keep_alive_default` in app-config). An optional `provider` (one of `antigravity`, `anthropic`, `chatgpt`, `xai`) targets just that login: `enabled` then sets or (`null`) clears that login's override on top of the sidecar-level setting, and the response lists `keep_alive_desired_providers`. Delivered as `keep_alive_desired` / `keep_alive_desired_providers` on its next ingest response; the sidecar applies it without a restart (admin) |
| `POST` | `/api/v1/fleet/sidecars/{id}/resume` | Resume collection on a sidecar (admin) |
| `POST` | `/api/v1/fleet/sidecars/{id}/update` | Queue a one-shot self-update for the sidecar; applies on its next heartbeat (admin) |
| `GET` | `/api/v1/fleet/config` | Active collection config the sidecar should poll; unsigned/non-loopback callers get only the `enabled`/`strategies` view, no account data |
| `POST` | `/api/v1/fleet/pairing-codes` | Mint a one-time pairing code + `runway-sidecar://pair` deep link for a new sidecar (admin) |
| `POST` | `/api/v1/fleet/pair` | Unauthenticated one-time-code redeem: exchanges a pairing code for `api_url` + ingest key (10/min/IP) |
| `POST` | `/api/v1/fleet/credentials/manifest` | Sidecar reports credential origins found this cycle (an entry may carry `reason: "token_withheld"` when the token stayed on the machine because the server cannot verify that provider by source); Claude OAuth entries may include a claimed `account_id`, which Runway matches to an enabled account or surfaces for assignment |
| `GET` | `/api/v1/fleet/credentials/tags` | List every resolved credential tag (deployment-wide and per-sidecar scopes); each carries `origin_label`, `origin_app` (the tool that owns the file, e.g. `Codex CLI`), `origin_path` (home collapsed to `~`) and `origin_detail` (for a cookie: the browser and profile it came from, e.g. `Chrome · Profile 1` (null for an Anthropic cookie origin, whose private browser profile is never exposed)) so the UI doesn't parse raw origin strings; also `last_matched_at` (freshest machine-reported credential the rule resolved, `null` = never) and `stale` (the `stale_credential_rules` data-health check would list it) |
| `POST` | `/api/v1/fleet/credentials/tags` | Resolve a pending credential origin to a configured account (admin). `scope: "deployment"` (all machines) is refused with 422 for `cookie:` and `keychain:` origins, which exist on one machine only; `path:` and `env:` origins can still be tagged deployment-wide (a shared home directory is one origin). A tag written against a plain origin also applies to its key-scoped form (`env:ZAI_API_KEY` covers `env:ZAI_API_KEY#<fp>`). The response is `{"status": "ok", "target_archived": false}`; `target_archived` is `true` when the target account is archived — the mapping is then *parked*: it resolves the origin (the credential stops being pending and the operator is not warned any more) but stays inert while the account is archived (no hint ships to the sidecar, no new usage is stamped on the archived account, no collector runs for the provider). Restoring the account revives the mapping automatically — see [Parked mappings](#parked-mappings) |
| `DELETE` | `/api/v1/fleet/credentials/tags` | Remove one resolved tag in a given scope (admin) |
| `GET` | `/api/v1/fleet/credentials/tags/pending` | List credential origins awaiting operator resolution, with a claimed Claude account ID when available, safe quota previews, and staleness status. Each item also carries `origin_label` (what the credential is, e.g. `Browser cookie`), `origin_app` (the tool that owns it, e.g. `Chrome` for a native Claude cookie or `Codex CLI` for a file) and `origin_detail` (for a cookie: the browser and profile it came from, e.g. `Chrome · Profile 1`; `null` when the machine could not name the jar (null for an Anthropic cookie origin, whose private browser profile is never exposed)) so the UI can say "browser cookie" instead of showing the raw `cookie:<provider>/session` origin |
| `GET` | `/api/v1/fleet/events/pending` | Paginated queue of events held back under an unresolved `default` identity (admin) |
| `GET` | `/api/v1/fleet/events/pending/sessions` | Paginated pending events grouped by provider, host, and session; supports `sidecar_id`, `provider_id`, and session/model `search` filters (admin) |
| `POST` | `/api/v1/fleet/events/pending/assign` | Assign up to 1000 pending events to one account, promoting them into `usage_events` and creating provider/host mappings (admin). Archived accounts are accepted (disabled ones are not). An optional `target_provider_id` assigns the events to an account of a related provider instead (allow-list: `gemini` → `antigravity`; anything else is a 422); the events are re-homed to that provider and later events from the host follow. The response echoes `target_provider_id`; `provider_id`/`target_provider_id`/`assigned` describe the first provider group only (back-compat), while `mappings` lists every group as `{provider_id, target_provider_id, account_id, assigned}` |
| `POST` | `/api/v1/fleet/events/pending/assign-batch` | Assign up to 10000 pending events across provider-specific accounts (each assignment takes the same optional `target_provider_id`); validates the full batch before promotion and returns the created provider/host mappings, each with its `target_provider_id` (admin). One provider on one host can't be mapped to two different targets in a single batch |

The grouped pending-events response reports `total_events` across the full queue and `matching_events` for the current filters; `total_groups` is the filtered group count used for pagination.

## System

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/system/health` | Liveness check |
| `GET` | `/api/v1/system/status` | Collector cache states and error counts |
| `GET`/`POST` | `/api/v1/fleet/credentials/{source_id}/claude-organizations` | Inspect eligible organizations or select one using an exact pending sidecar source (admin; no credential values returned) |
| `POST` | `/api/v1/system/provider-config/console-organizations` | Discover API organizations using a supplied Console `session_cookie` (admin; cookie is not persisted) |
| `GET` | `/api/v1/system/settings` | Non-sensitive runtime configuration: `version` (the release this build belongs to), `build_sha` (the commit the running build was built from, `null` when the build carries none — every `:edge` image ships the same `version`, so this is what identifies one exact build), `latest_version`/`update_available`, `app_host`/`app_port`, `encryption_enabled`, `admin_auth_required`, `auth_methods`, `user_context`, `is_authenticated` |
| `GET` | `/api/v1/system/audit-log` | Append-only admin-mutation trail |
| `GET` | `/api/v1/system/token-health` | Health of every credential (cache, dashboard-saved, server env/file): `status` is `valid`/`expiring`/`expired`/`invalid` (provider rejected it)/`failing` (not rejected, but its collections have kept failing for over an hour)/`stale` (sidecar stopped re-reporting it)/`unknown`; `removable` is false for config/server-managed rows. Status uses the same rules as `GET /system/credentials` (expiry, a provider rejection, and staleness), so the two views agree. Values are never returned — only types and origin. **Legacy:** the webapp no longer reads this; credential alerts still build on it because webhook scoping depends on its synthetic account ids (`server`, `config:<account>`), so use `GET /system/credentials` for new clients |
| `GET` | `/api/v1/system/credentials` | Credential inventory (admin): provider → account → source. Each source carries its machine, origin (`origin_kind`: `machine`/`config`/`server`; `origin_type`: what the credential is: `file`/`env`/`cookie`/`keychain`, or `sidecar` for an origin the server doesn't recognise; `origin_app`/`origin_path`: the tool that owns a machine's credential file and its `~`-collapsed path; `origin_detail`: for a cookie, the browser and profile it came from (e.g. `Chrome · Profile 1`), `null` when the machine could not name the jar (null for an Anthropic cookie origin, whose private browser profile is never exposed); `login_hint`: how to re-authenticate that tool; `machine_stale`: the machine stopped checking in, by Fleet's rule; `shared_with`: the other machines reporting the same secret, matched by fingerprint (a rotating login copied between machines signs the others out; the fingerprint itself is never returned; `shared_with_stale` is the subset on machines that stopped checking in, which the UI doesn't warn about) — each `machines[]` entry has the same `stale`), sources within an account are ordered active → healthy → dead, why it maps to its account (`local`/`verified`/`claim`/`operator`/`config`/`server`/`pending`), `status` (`valid`/`expiring`/`expired`/`invalid`/`failing`/`stale`/`unknown`), expiry, token types, `can_refresh` (the server can refresh it), `refreshed_by` (`server`, or `machine` for a rotating provider's login that a sidecar's CLI owns and the server must not refresh), `keep_alive` (`on`/`off`/`unknown` for a machine-sourced agy, Claude Code, Codex or xAI login that carries a refresh credential, live or not: whether that sidecar runs with `--keep-alive`; `unknown` = an older sidecar that does not report it; `null` when not applicable) and `keep_alive_desired` (the operator's server-side override for that login's sidecar, `null` = none; differs from `keep_alive` while a request is pending). Both are resolved per login: a per-login report/override (see the keep-alive `PUT` above) wins over the sidecar-level one, and collection provenance (`health` is `untried` for a credential that has never been attempted, else `healthy`/`degraded`/`auth_failed`/`unavailable`); `is_active` marks the source behind the account's latest successful collection. `rejected` is true when the provider rejected the credential, and `redundant` when it is expired and unrefreshable but another healthy credential for the same account can stand in for it (the Home banners skip those, exactly as Token Health's `redundant` does). `blocked_collection` lists unmapped, token-withheld origins whose provider has no collection from any enabled source in the last 6 hours (the Home "credential unmapped" banner; `unmapped_count` counts only origins without an effective hint, like the Untagged list). `unused_reason` (`provider_disabled`/`default_disabled`/`account_keyed_config`/`shadowed_by_config_key`) explains why a server env/file credential that is present isn't feeding any collection; such credentials are read from the host at request time, so they appear even before a first collection. `blocked_sources` lists the credential sources a machine found but could not read (a browser cookie store the browser holds open, or one whose values are app-bound encrypted): each entry names the provider, the store (`source_store`), the reason (`reason`: `source_locked`/`source_unsupported`/`source_unreadable`), the detail (`detail`) and the fix (`remedy`), and says whether the provider is still collecting from another source (`collecting`) — those are the "Unavailable" tab, the Home "cookie store unavailable" banner, and the provider page's empty state. An account carries `archived` when its `provider_configs` row is archived: the account is retired, nothing is collected for it, and a credential mapped onto it is *parked*. Never returns secret values |
| `POST` | `/api/v1/system/credentials/{provider}/{account_id}/{source_id}/refresh` | Refresh one source's OAuth token (replaces the removed per-account `/system/token-health/refresh` and `DELETE /system/token-health/...`; `409` for a login a machine's CLI owns) and write it back into that source's bundle (admin) |
| `DELETE` | `/api/v1/system/credentials/{provider}/{account_id}/{source_id}` | Forget one machine-reported credential (admin); it returns on the machine's next report if still present. `409` for config / server-env credentials |
| `POST` | `/api/v1/system/credentials/{provider}/{account_id}/remove` | Forget several machine-reported credentials of one account at once (admin, 10/min). Body `{"source_ids": [...]}` (1–50). Returns `{removed, skipped}`; config/server-managed or unknown sources are reported under `skipped` (`managed_elsewhere`/`not_found`) instead of failing the batch. A removed login's token is kept in the account cache while another remaining source still holds it |
| `POST` | `/api/v1/system/force-collect` | Trigger immediate collection cycle, fan out to sidecars |
| `POST` | `/api/v1/system/cleanup` | Prune stale records and inactive sidecars (admin) |
| `POST` | `/api/v1/system/wake` | Reset dormancy, restore normal polling |
| `POST` | `/api/v1/system/check-updates` | Force an immediate GitHub release poll for server + sidecars, refreshing the update-banner cache (admin) |
| `POST` | `/api/v1/system/debug/sources/{provider_id}` | Try each credential source of one account (`account_id`, default `default`) once, live, and report per source: `outcome` (`healthy`/`degraded`/`auth_failed`/`unavailable`, or `waiting_on_machine`/`disabled`/`pending`/`over_limit` for a source that was deliberately not called; at most 12 sources are called per request and `truncated` says when more were left out), `http_status`, `error_type`, a redacted `message`, `cards`, `duration_ms` (admin, `5/minute`). Writes nothing — no health/attempt update, promotion, backoff or cookie-tag change — and never refreshes a token; each source runs (a few at a time) on its own copy of the collector in a probe mode that also suppresses token-cache stores, the account's rejected flag and provider error events, pinned to that source. Never returns secret values |
| `GET` | `/api/v1/system/debug/raw/{provider_id}` | Run collector and return raw HTTP exchanges (debug; secrets redacted best-effort, admin); optional `account_id` selects that account's collector. Omitting it preserves provider-wide lookup and captures the first active match (or creates the registered collector if none is active) |
| `GET`/`POST`/`PATCH`/`DELETE` | `/api/v1/system/webhooks[...]` | CRUD + test for Discord/Slack threshold alerts; optional `account_id` scopes an alert to one account; `credential_alerts` (default true) also fires the same webhook when a matching credential's Token Health goes expired/invalid (admin) |
| `GET`/`PUT`/`DELETE` | `/api/v1/system/provider-config[s]/{...}` | Per-provider config CRUD (admin write); DELETE soft-archives and clears any matching `credential_tags` hints |
| `PATCH` | `/api/v1/system/provider-config/{provider_id}/{account_id}/credential-sources` | Set enabled state and zero-based order for every known source on this account (admin); duplicate priorities are allowed and ties sort by `source_id`; optional `all_machines` applies each listed source preference to matching sidecar origins across hosts; values are never returned |
| `POST` | `/api/v1/system/provider-config/preview-account` | Suggest an `account_id`/label for a new credential before saving it; `detect_billing: true` additionally proves how the account bills and returns it as `suggested_billing_type` (a proof costs a round trip to Claude, so it is asked for, not done on every keystroke) |
| `GET`/`PUT` | `/api/v1/system/app-config` | Global app config (admin write); includes `sidecar_update_channel`, `sidecar_auto_update` and `sidecar_keep_alive_default` (fleet-wide keep-alive default: on for every sidecar without its own override; omitted fields in a `PUT` keep their stored value) |
| `GET`/`PUT` | `/api/v1/system/dashboard-layout` | Persisted dashboard layout (admin for reads and writes) |
| `GET` | `/api/v1/system/sidecar-downloads` | Cached GitHub release assets for the Fleet page's *Add sidecar* card; `?channel=stable\|beta\|edge` (public) |

Every account in the provider-configs listing names the provider it belongs
to (`accounts[].provider_id`). The Claude family groups several providers
under one card, so an account listed under the family is still addressed by
its own provider — a rename, an archive or a billing model never lands on the
family's representative.

### Data health (admin)

See [docs/data-health.md](data-health.md) for what each check finds and fixes.

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/system/data-health/` | Cached scan report; starts the initial background scan. Returns `scan_error` and keeps any prior report stale after a failure; retry with `POST /rescan` |
| `POST` | `/api/v1/system/data-health/rescan` | Start a fresh scan (`202`; no-op if one is already running) |
| `POST` | `/api/v1/system/data-health/{check_id}/preview` | Read-only preview of a fix for one finding group — body: `{group_key, params}`; may include safe samples and a required `confirmation_text` |
| `POST` | `/api/v1/system/data-health/{check_id}/apply` | Apply a fix (`202`, returns `job_id`) — body: `{group_key, params, confirm: true}`; `409` if the latest scan failed, a scan/job is running, or the check is blocked by an upstream finding |
| `GET` | `/api/v1/system/data-health/jobs/{job_id}` | Poll a started fix job's status/result |

## Auth

### Admin session (browser)

The dashboard authenticates via an HttpOnly, `SameSite=Strict` session cookie. Scripts and
API clients can keep sending the `X-Admin-Key` header instead. See [SECURITY.md](SECURITY.md)
for cookie flags, `SESSION_SECRET`, and session lifetime.

| Method | Route | Description |
|--------|-------|-------------|
| `POST` | `/api/v1/auth/session` | Validate `ADMIN_API_KEY` and set the session cookie (`remember` extends lifetime). Rate-limited 10/min. When no admin key is configured the instance is open |
| `POST` | `/api/v1/auth/logout` | Clear this browser's session cookie (other sessions unaffected) |
| `POST` | `/api/v1/auth/revoke-all` | Rotate `SESSION_SECRET` — invalidates every session everywhere (admin, 6/min) |

### GitHub Device Flow

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/auth/github/init` | Begin GitHub OAuth Device Flow |
| `POST` | `/api/v1/auth/github/poll` | Poll for completion |
| `GET` | `/api/v1/auth/github/status` | Current GitHub auth state |
| `POST` | `/api/v1/auth/github/logout` | Discard stored GitHub token |

See [Sidecar Documentation](sidecar.md) for ingest authentication and payload format.

## LimitCard Schema

```typescript
interface LimitCard {
  // Core display fields
  service_name: string;     // Provider name (e.g., "Claude Pro")
  icon: string;             // Unicode emoji
  remaining: string;        // Remaining quota (e.g., "85%", "$12.50")
  unit: string;             // Unit description (e.g., "tokens", "/ 100")
  reset: string;            // Human-readable reset (e.g., "in 4h 23m")
  health: string;           // "good" | "warning" | "critical"
  pace: string;             // "Stable" | "Moderate Burn" | "Fast Burn"
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
  unit_type?: string;       // "currency" | "tokens" | "requests" | "percent"
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
    tokens?: number;        // Tokens for this model (if available)
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
  tier?: string;          // "Free" | "Pro" | "Enterprise"
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

See `../app/models/schemas.py` for the authoritative Pydantic definition. Token breakdown semantics, the `data_source`/`input_source` taxonomy, and the event-sourced data model are documented in [architecture.md](architecture.md) and [statistics.md](statistics.md).


Private `/usage/*` reads, fleet sidecar inventory/detail, system state and
provider configuration reads, and GitHub OAuth status require administrator
authentication in network mode. Localhost trust remains available with valid
local Host and browser Origin headers. Public bootstrap endpoints are
`/system/settings`, `/system/health`, and `/system/sidecar-downloads`; fleet
`/config` retains its redacted public view and signed sidecar behavior.
Webhook URLs must use the supported Slack or Discord HTTPS endpoint hosts.

Webhook list entries include optional `validation_error` text for saved URLs
blocked by the current destination policy. Configured encryption failures return
HTTP 503 and refuse credential persistence.

### Signed fleet request retries

Authenticated fleet ingest and signed config requests consume a durable, single-use signature receipt before handler effects. Reusing the same envelope returns HTTP `409` (`detail.error: "replayed_request"`), including after handler validation errors. Receipt storage failures return `503`. Generate a fresh timestamp and HMAC for every retry; the signing format is unchanged. Timestamps must be finite and no more than five minutes in the past or one minute in the future. Event identifiers continue to deduplicate retried usage events.

### Fleet login-directory additions

`PUT /api/v1/fleet/sidecars/{id}/settings` accepts optional
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
account it is parked on; that account carries `archived: true`.

A *redirect* tag (one with `target_provider_id`) names an account of its
**target** provider, so whether it is parked is looked up under that target
provider — a retired target account stops receiving the redirected credential
as well.

`POST /api/v1/fleet/credentials/tags` reports which kind of mapping it wrote
as `target_archived`.


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
label the operator chose is never overwritten.

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
local checkbox. See #780 and docs/sidecar-source-authorization.md for the full
consent, withdrawal, remote-control and acceptance contract.


### Fleet Keychain source lifecycle (protocol 1)

Updated sidecars request device_protocol=1 and platform=Darwin|Linux|Windows
when redeeming a pairing code. The platform is bound to the enrolled identity;
ordinary ingest OS labels cannot change it. Linux/Windows browser cookies do
not require macOS Keychain consent, but retain their canonical scraper identity
and server-owned generation so pause/revoke still applies and replay cannot
restore withdrawn credentials. An explicit proof, when present, is always
validated. The paired platform is a device assertion, not OS attestation;
local Keychain permission is enforced by the sidecar, and server lifecycle
eligibility is enforced independently of platform. Known Windows Credential Manager
origins (`github` / `keychain:github.com` and `antigravity` /
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

- POST /api/v1/fleet/source-sync: device-authenticated status and ordered
  command acknowledgment. The body contains protocol=1, sources, and acknowledgments
  ({id, status: applied|denied|superseded}). Source reports contain source_id,
  provider_id, label, revision, generation, literal boolean read, state, new,
  renewal_requested, renewal_supported, and optional last_read_at / last_renewed_at
  Unix timestamps. Labels are canonical; unknown scrapers are not accepted.
  The response lists accepted scraper IDs, withdrawals ({source_id, generation}),
  and pending commands ({id, source_id, generation, action}).
  Up to 256 pending commands are delivered in order per poll. Acknowledged
  commands are retained for 30 days, always keeping each scraper's latest
  ordering barrier; repeated acknowledgments after cleanup are harmless.
  Reports from an older generation update contact time without changing consent.
- GET /api/v1/fleet/sidecars/{sidecar_id}/sources: admin-authenticated, inert status.
  Returns protocol=1, sources (FleetSourceStatus), and pending commands.
  Source status extends the report with remote_paused, revoked, report_at,
  report_stale, announcement, renewal_capable, collection_health
  (withdrawn|paused|healthy|action_required|untried), and last_collection_at
  (ISO UTC or null). Stale reports never expire eligible server credentials.
  `report_at` / `report_stale` describe source reporting, independently of the
  device registry's `last_seen` / connectivity and server collection health.
  macOS sources send periodic consent/status reports. Windows Credential Manager
  sources update source contact when credentials are submitted; heartbeat-only
  ingest and empty source-sync requests do not refresh it. A Windows source report
  can therefore be stale while the device is online and server collection is
  healthy. This does not require another local read or invalidate its credentials.
- POST /api/v1/fleet/sidecars/{sidecar_id}/sources/{source_id}/commands:
  admin-authenticated {action}, one of pause, resume, revoke, request_setup,
  or dismiss. Returns {id, status: pending}. Pause/revoke immediately withdraw
  that source's server credentials. Resume requires current local read consent
  and a matching non-revoked generation; it only clears a server pause after
  acknowledgment. Setup requests create a local notice only. Dismissal is scoped
  to the current catalog capability revision.

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
