// API response types, mirroring app/models/schemas.py and the endpoint
// payloads in app/api/endpoints/. Fields the UI doesn't consume yet are
// typed loosely (Record<string, unknown>) rather than omitted, so payloads
// survive round-trips and narrowing can happen later.

export interface TokenUsage {
  input?: number;
  output?: number;
  reasoning?: number;
  cache_read?: number;
  total?: number;
}

export interface ByModelEntry {
  cost?: number;
  msgs?: number;
  tokens?: TokenUsage;
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
  reset_at?: string | null;
  data_source?: string;
  input_source?: string;
  error_type?: string | null;
  tier?: string | null;
  usage_url?: string | null;
  updated_at?: string | null;
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
}

export interface FleetEntry {
  provider_id: string;
  account_id: string;
  critical_gauge: LimitCard;
  secondary_limits: LimitCard[];
  sidecar_contributions?: Record<string, TokenUsage>;
  window_aggregations?: Record<string, unknown>;
}

export interface FleetResponse {
  fleet: FleetEntry[];
  generated_at: string;
}

export interface CumulativeBucket {
  tokens_input?: number;
  tokens_output?: number;
  tokens_cache_read?: number;
  tokens_cache_create?: number;
  tokens_reasoning?: number;
  cost_usd?: number;
  msgs?: number;
  by_model?: Record<string, ByModelEntry>;
  by_sidecar?: Record<string, TokenUsage>;
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
  model_id?: string | null;
  service_name?: string;
  window_type?: string;
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
}

export interface HeatmapResponse {
  cells: HeatmapCell[];
  tz: string;
}

export interface SessionEntry {
  session_id: string;
  [key: string]: unknown;
}

export interface UsageEvent {
  event_id?: string;
  ts?: string;
  model_id?: string | null;
  sidecar_id?: string | null;
  kind?: string;
  [key: string]: unknown;
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
  [key: string]: unknown;
}

export interface HistoryDeltas {
  token_delta_total?: number;
  cost_delta_total?: number;
  provider_token_deltas?: Record<string, number>;
  critical_series_count?: number;
  series_sampled?: number;
  [key: string]: unknown;
}

export interface Sidecar {
  sidecar_id: string;
  hostname?: string;
  custom_name?: string | null;
  tags?: string[];
  first_seen?: string;
  last_seen?: string;
  last_ip?: string;
  error_count?: number;
  ingest_count?: number;
  sidecar_version?: string;
  os_platform?: string;
  collection_enabled?: boolean;
  collection_errors?: string[] | null;
  last_log_lines?: string[] | null;
  [key: string]: unknown;
}

export interface SystemSettings {
  project_name?: string;
  app_host?: string;
  app_port?: number;
  version?: string;
  encryption_enabled?: boolean;
  admin_auth_required?: boolean;
  auth_methods?: string[];
  user_context?: string | null;
  is_authenticated?: boolean;
  ingest_api_key_is_default?: boolean;
}

export interface AppConfig {
  browser_preference?: string | null;
  default_poll_interval_seconds?: number;
  user_timezone?: string | null;
  env_timezone?: string | null;
}

export interface CollectionStrategy {
  id: string;
  enabled: boolean;
  [key: string]: unknown;
}

export interface ProviderConfig {
  provider_id: string;
  name: string;
  icon?: string;
  enabled?: boolean;
  api_key_set?: boolean;
  session_cookie_set?: boolean;
  account_label?: string | null;
  poll_interval_seconds?: number | null;
  default_ttl_seconds?: number;
  effective_poll_interval?: number;
  poll_interval_source?: string;
  supports_api_key?: boolean;
  supports_session_cookie?: boolean;
  api_key_label?: string | null;
  api_key_help?: string | null;
  session_cookie_label?: string | null;
  session_cookie_help?: string | null;
  supported_strategies?: CollectionStrategy[];
  collection_strategies?: CollectionStrategy[];
}

export interface Webhook {
  id: number;
  provider_id: string;
  threshold_pct: number;
  url: string;
  channel: 'discord' | 'slack';
  active: boolean;
  last_fired_at?: string | null;
}

export type TokenHealthStatus = 'valid' | 'expiring' | 'expired' | 'unknown' | string;

export interface TokenHealthEntry {
  provider: string;
  account_id: string;
  account_label?: string | null;
  source?: string | null;
  token_types?: string[];
  status: TokenHealthStatus;
  expires_at?: string | null;
  ttl_remaining_seconds?: number;
  can_refresh?: boolean;
}

export interface AuditEntry {
  id: number;
  ts: string;
  actor: string;
  source_ip?: string;
  action: string;
  target_id?: string | null;
  payload_json?: string | null;
}

export interface DashboardLayout {
  provider_order: string[];
  card_orders: Record<string, string[]>;
}

export interface CollectorStatus {
  [key: string]: unknown;
}
