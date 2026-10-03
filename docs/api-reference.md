# API Reference

All API routes are under `/api/v1/`.

## Usage & history

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/usage/limits` | All current quota cards (instant, from in-memory registry) |
| `GET` | `/api/v1/usage/sources` | Distinct sidecar IDs with message usage events |
| `GET` | `/api/v1/usage/fleet` | Fleet HUD: critical gauge + secondary limits per (provider, account), with `window_aggregations.longest` per-model/per-sidecar splits |
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
| `POST` | `/api/v1/fleet/sidecars/{id}/resume` | Resume collection on a sidecar (admin) |
| `POST` | `/api/v1/fleet/sidecars/{id}/update` | Queue a one-shot self-update for the sidecar; applies on its next heartbeat (admin) |
| `GET` | `/api/v1/fleet/config` | Active collection config the sidecar should poll; unsigned/non-loopback callers get only the `enabled`/`strategies` view, no account data |
| `POST` | `/api/v1/fleet/pairing-codes` | Mint a one-time pairing code + `runway-sidecar://pair` deep link for a new sidecar (admin) |
| `POST` | `/api/v1/fleet/pair` | Unauthenticated one-time-code redeem: exchanges a pairing code for `api_url` + ingest key (10/min/IP) |
| `POST` | `/api/v1/fleet/credentials/manifest` | Sidecar reports credential origins found this cycle (an entry may carry `reason: "token_withheld"` when the token stayed on the machine because the server cannot verify that provider by source); Claude OAuth entries may include a claimed `account_id`, which Runway matches to an enabled account or surfaces for assignment |
| `GET` | `/api/v1/fleet/credentials/tags` | List every resolved credential tag (deployment-wide and per-sidecar scopes) |
| `POST` | `/api/v1/fleet/credentials/tags` | Resolve a pending credential origin to a configured account (admin). `scope: "deployment"` (all machines) is refused with 422 for `cookie:` and `keychain:` origins, which exist on one machine only; `path:` and `env:` origins can still be tagged deployment-wide (a shared home directory is one origin). A tag written against a plain origin also applies to its key-scoped form (`env:ZAI_API_KEY` covers `env:ZAI_API_KEY#<fp>`) |
| `DELETE` | `/api/v1/fleet/credentials/tags` | Remove one resolved tag in a given scope (admin) |
| `GET` | `/api/v1/fleet/credentials/tags/pending` | List credential origins awaiting operator resolution, with a claimed Claude account ID when available, safe quota previews, and staleness status |
| `GET` | `/api/v1/fleet/events/pending` | Paginated queue of events held back under an unresolved `default` identity (admin) |
| `GET` | `/api/v1/fleet/events/pending/sessions` | Paginated pending events grouped by provider, host, and session; supports `sidecar_id`, `provider_id`, and session/model `search` filters (admin) |
| `POST` | `/api/v1/fleet/events/pending/assign` | Assign up to 1000 pending events to one active account, promoting them into `usage_events` and creating provider/host mappings (admin) |
| `POST` | `/api/v1/fleet/events/pending/assign-batch` | Assign up to 10000 pending events across provider-specific active accounts; validates the full batch before promotion and returns the created provider/host mappings (admin) |

The grouped pending-events response reports `total_events` across the full queue and `matching_events` for the current filters; `total_groups` is the filtered group count used for pagination.

## System

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/system/health` | Liveness check |
| `GET` | `/api/v1/system/status` | Collector cache states and error counts |
| `GET` | `/api/v1/system/settings` | Non-sensitive runtime configuration |
| `GET` | `/api/v1/system/audit-log` | Append-only admin-mutation trail |
| `GET` | `/api/v1/system/token-health` | Health of every credential (cache, dashboard-saved, server env/file): `status` is `valid`/`expiring`/`expired`/`invalid` (provider rejected it)/`failing` (not rejected, but its collections have kept failing for over an hour)/`stale` (sidecar stopped re-reporting it)/`unknown`; `removable` is false for config/server-managed rows. Status uses the same rules as `GET /system/credentials` (expiry, a provider rejection, and staleness), so the two views agree. Values are never returned — only types and origin. **Legacy:** the webapp no longer reads this; credential alerts still build on it because webhook scoping depends on its synthetic account ids (`server`, `config:<account>`), so use `GET /system/credentials` for new clients |
| `GET` | `/api/v1/system/credentials` | Credential inventory (admin): provider → account → source. Each source carries its machine, origin (`origin_kind`: `machine`/`config`/`server`; `origin_type`: what the credential is: `file`/`env`/`cookie`/`keychain`, or `sidecar` for an origin the server doesn't recognise), why it maps to its account (`local`/`verified`/`claim`/`operator`/`config`/`server`/`pending`), `status` (`valid`/`expiring`/`expired`/`invalid`/`failing`/`stale`/`unknown`), expiry, token types, `can_refresh` (the server can refresh it), `refreshed_by` (`server`, or `machine` for a rotating provider's login that a sidecar's CLI owns and the server must not refresh), and collection provenance (`health` is `untried` for a credential that has never been attempted, else `healthy`/`degraded`/`auth_failed`/`unavailable`); `is_active` marks the source behind the account's latest successful collection. `rejected` is true when the provider rejected the credential, and `redundant` when it is expired and unrefreshable but another healthy credential for the same account can stand in for it (the Home banners skip those, exactly as Token Health's `redundant` does). `blocked_collection` lists unmapped, token-withheld origins whose provider has no collection from any enabled source in the last 6 hours (the Home "credential unmapped" banner; `unmapped_count` counts only origins without an effective hint, like the Untagged list). `unused_reason` (`provider_disabled`/`default_disabled`/`account_keyed_config`/`shadowed_by_config_key`) explains why a server env/file credential that is present isn't feeding any collection; such credentials are read from the host at request time, so they appear even before a first collection. Never returns secret values |
| `POST` | `/api/v1/system/credentials/{provider}/{account_id}/{source_id}/refresh` | Refresh one source's OAuth token (replaces the removed per-account `/system/token-health/refresh` and `DELETE /system/token-health/...`; `409` for a login a machine's CLI owns) and write it back into that source's bundle (admin) |
| `DELETE` | `/api/v1/system/credentials/{provider}/{account_id}/{source_id}` | Forget one machine-reported credential (admin); it returns on the machine's next report if still present. `409` for config / server-env credentials |
| `POST` | `/api/v1/system/force-collect` | Trigger immediate collection cycle, fan out to sidecars |
| `POST` | `/api/v1/system/cleanup` | Prune stale records and inactive sidecars (admin) |
| `POST` | `/api/v1/system/wake` | Reset dormancy, restore normal polling |
| `POST` | `/api/v1/system/check-updates` | Force an immediate GitHub release poll for server + sidecars, refreshing the update-banner cache (admin) |
| `POST` | `/api/v1/system/debug/sources/{provider_id}` | Try each credential source of one account (`account_id`, default `default`) once, live, and report per source: `outcome` (`healthy`/`degraded`/`auth_failed`/`unavailable`, or `waiting_on_machine`/`disabled`/`pending`/`over_limit` for a source that was deliberately not called; at most 12 sources are called per request and `truncated` says when more were left out), `http_status`, `error_type`, a redacted `message`, `cards`, `duration_ms` (admin, `5/minute`). Writes nothing — no health/attempt update, promotion, backoff or cookie-tag change — and never refreshes a token; each source runs (a few at a time) on its own copy of the collector in a probe mode that also suppresses token-cache stores, the account's rejected flag and provider error events, pinned to that source. Never returns secret values |
| `GET` | `/api/v1/system/debug/raw/{provider_id}` | Run collector and return raw HTTP exchanges (debug; secrets redacted best-effort, admin); optional `account_id` selects that account's collector. Omitting it preserves provider-wide lookup and captures the first active match (or creates the registered collector if none is active) |
| `GET`/`POST`/`PATCH`/`DELETE` | `/api/v1/system/webhooks[...]` | CRUD + test for Discord/Slack threshold alerts; optional `account_id` scopes an alert to one account; `credential_alerts` (default true) also fires the same webhook when a matching credential's Token Health goes expired/invalid (admin) |
| `GET`/`PUT`/`DELETE` | `/api/v1/system/provider-config[s]/{...}` | Per-provider config CRUD (admin write); DELETE soft-archives and clears any matching `credential_tags` hints |
| `PATCH` | `/api/v1/system/provider-config/{provider_id}/{account_id}/credential-sources` | Set enabled state and zero-based order for every known source on this account (admin); duplicate priorities are allowed and ties sort by `source_id`; optional `all_machines` applies each listed source preference to matching sidecar origins across hosts; values are never returned |
| `POST` | `/api/v1/system/provider-config/preview-account` | Suggest an `account_id`/label for a new credential before saving it |
| `GET`/`PUT` | `/api/v1/system/app-config` | Global app config (admin write) |
| `GET`/`PUT` | `/api/v1/system/dashboard-layout` | Persisted dashboard layout |
| `GET` | `/api/v1/system/sidecar-downloads` | Cached GitHub release assets for the Fleet page's *Add sidecar* card; `?channel=stable\|beta\|edge` (public) |

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
