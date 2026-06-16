# API Reference

All API routes are under `/api/v1/`.

## Usage & history

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/usage/limits` | All current quota cards (instant, from in-memory registry) |
| `GET` | `/api/v1/usage/fleet` | Fleet HUD: critical gauge + secondary limits per (provider, account), with `window_aggregations.longest` per-model/per-sidecar splits |
| `GET` | `/api/v1/usage/cumulative` | Authorized totals across sidecars (lifetime/year/month/day); `since`/`until` narrow to an arbitrary window |
| `GET` | `/api/v1/usage/forecast` | Theil-Sen projection to reset; `include_series=true` returns drill-down points |
| `GET` | `/api/v1/usage/history/windows` | Paginated closed quota windows |
| `GET` | `/api/v1/usage/history/snapshots` | Paginated snapshot rows with per-series delta |
| `GET` | `/api/v1/usage/history/chart` | Time-series data for percent/tokens/cost visualisations; `group=provider` collapses bars to one segment per provider (cross-provider stack) |
| `GET` | `/api/v1/usage/history/window-detail` | Fill-up series + by-model breakdown for one window |
| `GET` | `/api/v1/usage/history/deltas` | Event-sourced consumption deltas |
| `GET` | `/api/v1/usage/events` | Recent event tail for (provider, account) |
| `GET` | `/api/v1/usage/events/range` | Events within an explicit `since`/`until` window for (provider, account) |
| `GET` | `/api/v1/usage/window-history` | Closed-window history with per-model & per-sidecar splits |
| `GET` | `/api/v1/usage/heatmap` | 7×24 hour-of-day activity grid |
| `GET` | `/api/v1/usage/sessions` | Top-N sessions (`sort_by=tokens` or `recent`) |
| `GET` | `/api/v1/usage/sessions/paginated` | Paginated session browser with server-side sort (`sort_by=recent\|tokens\|duration\|messages\|cost`, `sort_dir=asc\|desc`) and optional `project` filter |
| `GET` | `/api/v1/usage/projects` | Distinct project names seen in events (for filter dropdowns) |
| `GET` | `/api/v1/usage/top-projects` | Top projects ranked by `metric=tokens\|cost\|sessions` |
| `GET` | `/api/v1/usage/top-tools` | Top tools by invocation/token volume over the window |
| `GET` | `/api/v1/usage/top-models` | Cross-provider Top Models ranked by `metric=tokens\|cost` |
| `GET` | `/api/v1/usage/global-stats` | Global cross-provider snapshot: lifetime totals, session economics, cache-hit ratio, busiest day/hour |
| `GET` | `/api/v1/usage/cost-forecast` | MTD cost + 7-day burn extrapolated to EOM |
| `GET` | `/api/v1/usage/anomalies` | Z-score spike detection vs. historical mean |
| `POST` | `/api/v1/usage/reset/{provider}` | Clear terminal failure state for a provider |
| `POST` | `/api/v1/usage/collect/{provider}` | Force immediate re-collection for one provider |

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
| `GET` | `/api/v1/fleet/config` | Active collection config the sidecar should poll |

## System

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/v1/system/health` | Liveness check |
| `GET` | `/api/v1/system/status` | Collector cache states and error counts |
| `GET` | `/api/v1/system/settings` | Non-sensitive runtime configuration |
| `GET` | `/api/v1/system/audit-log` | Append-only admin-mutation trail |
| `GET` | `/api/v1/system/token-health` | OAuth/cookie expiry status for all credentials |
| `POST` | `/api/v1/system/token-health/refresh/{provider}/{account_id}` | Trigger OAuth token refresh (admin) |
| `DELETE` | `/api/v1/system/token-health/{provider}/{account_id}` | Evict token from cache (admin) |
| `POST` | `/api/v1/system/force-collect` | Trigger immediate collection cycle, fan out to sidecars |
| `POST` | `/api/v1/system/cleanup` | Prune stale records and inactive sidecars (admin) |
| `POST` | `/api/v1/system/wake` | Reset dormancy, restore normal polling |
| `POST` | `/api/v1/system/check-updates` | Force an immediate GitHub release poll for server + sidecars, refreshing the update-banner cache (admin) |
| `GET` | `/api/v1/system/debug/raw/{provider_id}` | Run collector and return raw HTTP responses (debug) |
| `GET`/`POST`/`PATCH`/`DELETE` | `/api/v1/system/webhooks[...]` | CRUD + test for Discord/Slack threshold alerts (admin) |
| `GET`/`PUT` | `/api/v1/system/provider-config[s]/{...}` | Per-provider config CRUD (admin write) |
| `GET`/`PUT` | `/api/v1/system/app-config` | Global app config (admin write) |
| `GET`/`PUT` | `/api/v1/system/dashboard-layout` | Persisted dashboard layout |

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
    total: number;       // Total tokens
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
  tier?: string;          // "Free" | "Pro" | "Enterprise"
  usage_url?: string;     // Link to provider usage page
  updated_at?: string;    // ISO 8601 timestamp
  metadata?: Record<string, unknown>;  // Free-form, provider-specific extras
}
```

See `../app/models/schemas.py` for the authoritative Pydantic definition. Token breakdown semantics, the `data_source`/`input_source` taxonomy, and the event-sourced data model are documented in [CLAUDE.md](../CLAUDE.md) and [statistics.md](statistics.md).
