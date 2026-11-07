// API response types, mirroring app/models/schemas.py and the endpoint
// payloads in app/api/endpoints/. Fields the UI doesn't consume yet are
// typed loosely (Record<string, unknown>) rather than omitted, so payloads
// survive round-trips and narrowing can happen later.

import type { BlockedSource } from './credentials';
import type { MergedAccount } from './provider';

export interface TokenUsage {
  input?: number;
  output?: number;
  reasoning?: number;
  cache_read?: number;
  cache_create?: number;
  total?: number;
}

export interface ByModelEntry {
  cost?: number;
  msgs?: number;
  tokens?: TokenUsage;
}

export interface CreditBalanceComponent {
  kind: 'purchased' | 'subscription' | 'promotion';
  label: string;
  currency: string;
  remaining_value: number;
  granted_value?: number | null;
  expires_at?: string | null;
  products: string[];
}

export interface LimitCard {
  service_name: string;
  icon?: string;
  remaining?: string;
  unit?: string;
  reset?: string;
  health?: string;
  pace?: string;
  detail?: string;
  used_value?: number | null;
  limit_value?: number | null;
  is_unlimited?: boolean;
  unit_type?: string; // "currency" | "tokens" | "requests" | "minutes" | "percent" | "generic"
  currency?: string | null;
  credit_breakdown?: CreditBalanceComponent[] | null;
  reset_at?: string | null;
  data_source?: string;
  input_source?: string;
  error_type?: string | null;
  tier?: string | null;
  billing_evidence?: string | null;
  usage_url?: string | null;
  updated_at?: string | null;
  // Poll timing — set by /usage/fleet on the critical_gauge card only.
  fetched_at?: string | null;
  next_poll_at?: string | null;
  cache_ttl_seconds?: number | null;
  metadata?: Record<string, unknown> | null;
  provider_id?: string | null;
  account_id?: string | null;
  account_label?: string | null;
  model_id?: string | null;
  sidecar_id?: string | null;
  window_type?: string; // "session" | "daily" | "weekly" | "monthly" | "rolling" | "unknown"
  variant?: string | null;
  quota_pool_id?: string | null;
  token_usage?: TokenUsage | null;
  by_model?: Record<string, ByModelEntry> | null;
  msgs?: number | null;
  pct_used?: number | null;
  stale?: boolean;
  collection_failing?: boolean;
}

// Live aggregation of usage_events over a quota window, split by model,
// sidecar and client app. Computed on demand by /usage/fleet for the longest
// active window.
export interface WindowAggregation {
  window_type: string;
  window_start: string;
  window_end: string;
  token_usage: TokenUsage;
  by_model: Record<string, CumulativeModelBucket>;
  by_sidecar: Record<string, CumulativeModelBucket>;
  by_source_app?: Record<string, CumulativeModelBucket>;
}

export interface FleetEntry {
  provider_id: string;
  account_id: string;
  server_collector_available?: boolean;
  billing_type?: 'unknown' | 'subscription' | 'pay_as_you_go';
  critical_gauge: LimitCard;
  secondary_limits: LimitCard[];
  sidecar_contributions?: Record<string, TokenUsage>;
  window_aggregations?: { longest?: WindowAggregation };
}

export interface FleetResponse {
  fleet: FleetEntry[];
  generated_at: string;
  /** Credential sources a machine found but could not read. Absent from an older server. */
  blocked_sources?: BlockedSource[];
  /**
   * The accounts that were merged away, and where each of them went. A
   * merged-away account holds no credential and has no quota card, so it is
   * not a fleet entry — it would need a gauge it does not have. Absent from
   * an older server.
   */
  merged_accounts?: (MergedAccount & { provider_id: string })[];
}

// Flat token/cost bucket used by cumulative rollups (also the per-model and
// per-sidecar split values inside a bucket).
export interface CumulativeModelBucket {
  tokens_input?: number;
  tokens_output?: number;
  tokens_cache_read?: number;
  tokens_cache_create?: number;
  tokens_reasoning?: number;
  msgs?: number;
  cost_usd?: number;
  // Per-component cost (reasoning folds into cost_output, billed at the output rate).
  cost_input?: number;
  cost_output?: number;
  cost_cache_read?: number;
  cost_cache_create?: number;
  // Cache portion of cost_usd (cache_read + cache_create), for the exclude-cache toggle.
  cost_cache?: number;
}

export interface CumulativeBucket extends CumulativeModelBucket {
  by_model?: Record<string, CumulativeModelBucket>;
  by_sidecar?: Record<string, CumulativeModelBucket>;
  by_source_app?: Record<string, CumulativeModelBucket>;
}

export interface CumulativeEntry {
  provider_id: string;
  account_id: string;
  lifetime?: CumulativeBucket;
  [periodKey: string]: CumulativeBucket | string | undefined;
}

export interface CumulativeResponse {
  cumulative: CumulativeEntry[];
  current_month_key: string;
  current_year_key: string;
  generated_at: string;
}

export type ForecastStatus =
  | 'ok'
  | 'warn'
  | 'risk'
  | 'insufficient_data'
  | 'stable'
  | 'exhausted'
  | 'decelerating'
  | 'low_resolution'
  | 'near_limit';

export interface ForecastSeriesPoint {
  ts: string;
  pct: number;
  [key: string]: unknown;
}

export interface ForecastEntry {
  provider_id: string;
  account_id: string;
  account_label?: string | null;
  model_id?: string | null;
  service_name?: string;
  window_type?: string;
  window_start?: string | null;
  projected_limit_hit_at?: string | null;
  variant?: string | null;
  unit_type?: string;
  now_used?: number | null;
  now_pct?: number | null;
  projected_used?: number | null;
  projected_pct?: number | null;
  limit_value?: number | null;
  reset_at?: string | null;
  samples_used?: number;
  confidence?: number;
  status: ForecastStatus;
  method?: string;
  slope?: number;
  glide_pct?: number | null;
  series?: ForecastSeriesPoint[];
}

export interface ForecastResponse {
  forecasts: ForecastEntry[];
  summary?: Record<string, number>;
}

export interface CostForecastByProvider {
  provider_id: string;
  account_id: string;
  current_month_to_date: number;
  daily_burn_avg_7d: number;
  projected_eom: number;
}

export interface CostForecastResponse {
  as_of: string;
  current_month_to_date: number;
  daily_burn_avg_7d: number;
  projected_eom: number;
  days_in_month: number;
  day_of_month: number;
  days_remaining: number;
  by_provider: CostForecastByProvider[];
}

export interface HeatmapCell {
  dow: number; // SQLite convention: 0=Sunday … 6=Saturday
  hour: number;
  tokens: number;
  cost_usd?: number;
}

export interface HeatmapResponse {
  cells: HeatmapCell[];
  tz: string;
}

export interface UsageSourcesResponse {
  sidecar_ids: string[];
  // The client apps that produced the events ("opencode", "claude_code", ...).
  // Several apps share one provider, so the provider id alone can't say which
  // app spent the quota.
  source_apps?: string[];
}

export interface SessionModelSplit extends CumulativeModelBucket {
  model_id: string;
  tokens_total?: number;
  tool_calls?: number;
}

export interface SubagentSplit {
  subagent_type: string; // "Explore" | "Plan" | …
  turns?: number;
  // The API requests behind those turns. Equal to `turns` for a provider
  // that emits one event per message, larger for one that emits an event
  // per request (MiniMax Code).
  requests?: number;
  tokens_total?: number;
  tokens_input?: number;
  tokens_output?: number;
  tokens_cache_read?: number;
  tokens_cache_create?: number;
  tokens_reasoning?: number;
  tool_calls?: number;
  cost_usd?: number;
  cost_input?: number;
  cost_output?: number;
  cost_cache_read?: number;
  cost_cache_create?: number;
}

export interface SessionEntry {
  session_id: string;
  // Display name: the agent-set name if present, else the tool-generated one.
  name?: string | null;
  ts_start?: string;
  ts_end?: string;
  duration_seconds?: number;
  msgs?: number;
  models?: string[];
  by_model?: SessionModelSplit[];
  subagents?: SubagentSplit[];
  tokens_total?: number;
  tokens_input?: number;
  tokens_output?: number;
  tokens_cache_read?: number;
  tokens_cache_create?: number;
  tokens_cache?: number;
  tokens_reasoning?: number;
  tool_calls?: number;
  subagent_msgs?: number;
  cache_pct?: number;
  cost_usd?: number;
  cost_input?: number;
  cost_output?: number;
  cost_cache_read?: number;
  cost_cache_create?: number;
  cache_hit_pct?: number;
  sidecar_id?: string | null;
  project?: string | null;
  cwd?: string | null;
  git_branch?: string | null;
  [key: string]: unknown;
}

export interface SessionsPaginatedResponse {
  sessions: SessionEntry[];
  total: number;
  limit: number;
  offset: number;
}

export interface TopProjectEntry {
  project: string;
  msgs: number;
  sessions: number;
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  cost_usd: number;
  cost_cache: number;
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
  providers: string[];
}

export interface TopProjectsResponse {
  projects: TopProjectEntry[];
  metric: string; // "tokens" | "cost" | "sessions"
  total: number; // the ranked groups before the limit truncated them
  limit: number; // the cap that was applied
  generated_at: string;
}

export interface TopSessionEntry {
  session_id: string;
  name?: string | null; // stored display name (first provider alphabetically that has one)
  project: string | null;
  cwd: string | null;
  sidecar_id: string | null; // the host that pushed the session's latest event
  ts_start: string | null; // ISO-8601 UTC
  ts_end: string | null; // ISO-8601 UTC
  hosts: number; // distinct hosts the session ran on
  msgs: number;
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  cost_usd: number;
  cost_cache: number;
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
  providers: string[];
  models: string[];
}

export interface TopSessionsResponse {
  sessions: TopSessionEntry[];
  metric: string; // "tokens" | "cost"
  total: number; // the ranked groups before the limit truncated them
  limit: number; // the cap that was applied
  generated_at: string;
}

export interface TopHostEntry {
  sidecar_id: string; // the hostname that pushed these events
  ts_start: string | null; // ISO-8601 UTC
  ts_end: string | null; // ISO-8601 UTC
  msgs: number;
  sessions: number; // distinct sessions that ran on this host
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  cost_usd: number;
  cost_cache: number;
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
  providers: string[];
}

export interface TopHostsResponse {
  hosts: TopHostEntry[];
  metric: string; // "tokens" | "cost"
  total: number; // the ranked groups before the limit truncated them
  limit: number; // the cap that was applied
  generated_at: string;
}

export interface TopToolEntry {
  tool: string;
  calls: number;
  msgs: number;
}

export interface TopToolsResponse {
  tools: TopToolEntry[];
  total: number; // the ranked groups before the limit truncated them
  limit: number; // the cap that was applied
  generated_at: string;
}

export interface UsageEvent {
  credential_origin?: string | null;
  id?: number;
  event_id?: string;
  ts?: string;
  ingested_at?: string;
  model_id?: string | null;
  sidecar_id?: string | null;
  session_id?: string | null;
  kind?: string;
  stop_reason?: string | null;
  tool_calls?: number | null;
  latency_ms?: number | null;
  cost_usd?: number | null;
  tokens_input?: number;
  tokens_output?: number;
  tokens_cache_read?: number;
  tokens_cache_create?: number;
  // Cache writes split by TTL (Anthropic only; both 0 = split unknown, e.g.
  // events ingested before this split existed). Sums to tokens_cache_create.
  tokens_cache_create_1h?: number;
  tokens_cache_create_5m?: number;
  tokens_reasoning?: number;
  // Per-message dimensions. effort: anthropic + chatgpt (antigravity/opencode
  // pending); the rest Anthropic-only, null/0 elsewhere.
  effort?: string | null;
  speed?: string | null;
  service_tier?: string | null;
  entrypoint?: string | null;
  app_version?: string | null;
  web_search_requests?: number;
  web_fetch_requests?: number;
  [key: string]: unknown;
}

// Paginated event-tail response. `total` is the full count of matching rows
// (not the page size), so offset/limit paging math is well-defined.
export interface EventsResponse {
  events: UsageEvent[];
  total: number;
  limit: number;
  offset: number;
}

// Earliest/latest event timestamps for a (provider, account) pair. Bounds how
// far back the month selector can page. Both null when the pair has no events.
export interface EventRangeResponse {
  earliest: string | null;
  latest: string | null;
}

export interface AnomalyEntry {
  provider_id: string;
  account_id: string;
  model_id: string;
  today_tokens: number;
  today_cost_usd: number;
  historical_mean_tokens: number;
  historical_stddev_tokens: number;
  z_score_tokens: number;
  verdict: string;
}

export interface AnomaliesResponse {
  as_of: string;
  lookback_days: number;
  z_threshold: number;
  anomalies: AnomalyEntry[];
}

export interface HistoryWindow {
  provider_id?: string;
  account_id?: string;
  window_type?: string;
  window_start?: string;
  window_end?: string;
  series_model_id?: string;
  series_variant?: string;
  totals?: CumulativeModelBucket;
  by_model?: SessionModelSplit[];
  by_sidecar?: Record<string, CumulativeModelBucket>;
  [key: string]: unknown;
}

// /usage/history/chart — metric=percent returns line series, tokens/cost
// return stacked time buckets.
export interface ChartSeriesPoint {
  ts: string;
  pct_used: number | null;
}

export interface ChartSeries {
  key: string;
  provider_id: string;
  window_type: string;
  model_id: string;
  label: string;
  color_hint?: string;
  points: ChartSeriesPoint[];
}

export interface ChartBarSegment {
  provider_id: string;
  model_id: string;
  label: string;
  value: number;
  value_cache?: number;
}

export interface ChartBar {
  date: string;
  ts: string;
  segments: ChartBarSegment[];
}

export interface HistoryChartResponse {
  series?: ChartSeries[];
  bars?: ChartBar[];
}

// /usage/history/windows — flat per-window rows (open + closed).
export interface HistoryWindowRow {
  provider_id: string;
  account_id: string;
  account_label?: string | null;
  service_name?: string;
  window_type?: string;
  window_start?: string | null;
  window_end?: string | null;
  is_open?: boolean;
  pct_used?: number | null;
  limit_value?: number | null;
  unit_type?: string | null;
  tokens_total?: number | null;
  tokens_input?: number | null;
  tokens_output?: number | null;
  tokens_cache_read?: number | null;
  tokens_cache_create?: number | null;
  tokens_reasoning?: number | null;
  cost_usd?: number | null;
  msgs?: number | null;
  top_model?: string | null;
}

export interface WindowDetailModelEntry {
  model_id: string;
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  cost_usd: number;
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
  msgs: number;
}

export interface WindowDetailResponse {
  fill_series: ChartSeriesPoint[];
  fill_by_model: { model_id: string; series: ChartSeriesPoint[] }[];
  by_model: WindowDetailModelEntry[];
}

export interface HistoryDeltas {
  token_delta_total?: number;
  token_input_total?: number;
  token_output_total?: number;
  token_reasoning_total?: number;
  token_cache_read_total?: number;
  token_cache_create_total?: number;
  token_cache_total?: number;
  cost_delta_total?: number;
  cost_cache_total?: number;
  provider_token_deltas?: Record<string, number>;
  critical_series_count?: number;
  series_sampled?: number;
  [key: string]: unknown;
}

// --- Cross-provider stats (Top Models + Global Insights) -------------------

export interface TopModelEntry {
  model_id: string;
  msgs: number;
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  cost_usd: number;
  cost_cache: number; // cache_read + cache_create cost, for exclude-cache
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
  providers: string[];
}

export interface TopModelsResponse {
  models: TopModelEntry[];
  metric: string; // "tokens" | "cost"
  total: number; // the ranked groups before the limit truncated them
  limit: number; // the cap that was applied
  generated_at: string;
}

// One client app's totals in the per-app usage breakdown.
export interface SourceAppEntry {
  source_app: string;
  msgs: number;
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  cost_usd: number;
  cost_cache: number; // cache_read + cache_create cost, for exclude-cache
  cost_input: number;
  cost_output: number;
  cost_cache_read: number;
  cost_cache_create: number;
  providers: string[];
}

export interface SourceAppsResponse {
  apps: SourceAppEntry[];
  metric: string; // "tokens" | "cost"
  generated_at: string;
}

export interface GlobalLifetimeTotals {
  tokens_total: number;
  tokens_input: number;
  tokens_output: number;
  tokens_cache_read: number;
  tokens_cache_create: number;
  tokens_reasoning: number;
  tokens_cache: number;
  cost_usd: number;
  cost_cache: number;
  msgs: number;
}

export interface GlobalSessionStats {
  count: number;
  avg_cost: number;
  avg_tokens: number;
}

export interface GlobalBusiestDay {
  period_key: string; // "YYYY-MM-DD" (UTC date)
  tokens: number;
}

export interface GlobalBusiestHour {
  hour: number; // 0–23, local tz
  tokens: number;
}

export interface GlobalStatsResponse {
  lifetime: GlobalLifetimeTotals;
  sessions: GlobalSessionStats;
  cache_hit_ratio: number; // 0..1
  distinct_models: number;
  distinct_providers: number;
  busiest_day: GlobalBusiestDay | null;
  busiest_hour: GlobalBusiestHour | null;
  generated_at: string;
}
