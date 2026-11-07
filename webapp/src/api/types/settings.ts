export interface SystemSettings {
  project_name?: string;
  app_host?: string;
  app_port?: number;
  version?: string;
  // The commit the running server was built from. `version` names a release
  // and is identical across every `:edge` build; this pins one exact build.
  // null when the build carries no SHA (e.g. an image built without one).
  build_sha?: string | null;
  // Latest published Runway release tag (no `v` prefix); null until the server
  // has polled GitHub at least once. update_available compares it to `version`.
  latest_version?: string | null;
  update_available?: boolean;
  encryption_enabled?: boolean;
  admin_auth_required?: boolean;
  auth_methods?: string[];
  user_context?: string | null;
  is_authenticated?: boolean;
  ingest_api_key_is_default?: boolean;
}

// Result of POST /system/check-updates — an on-demand GitHub release poll.
export interface UpdateCheckResult {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
}

// GET /system/sidecar-downloads — latest sidecar builds for the Fleet page's
// "Add a sidecar" card. Mirrors SidecarDownloadsResponse in app/models/schemas.py.
export type SidecarPlatform = 'macOS' | 'Windows' | 'Linux' | 'Linux-CLI';
export type SidecarChannel = 'stable' | 'beta' | 'edge';

export interface SidecarDownloadAsset {
  platform: SidecarPlatform;
  // installer = .dmg / -setup.exe; payload = portable .zip / .tar.gz
  kind: 'installer' | 'payload';
  name: string;
  url: string;
  size?: number | null;
  sha256_url?: string | null;
}

export interface SidecarDownloads {
  channel: SidecarChannel;
  version?: string | null;
  published_at?: string | null;
  release_url: string;
  checksums_url?: string | null;
  assets: SidecarDownloadAsset[];
  // Set (with empty assets) when the server couldn't reach GitHub.
  error?: string | null;
}

// POST /fleet/pairing-codes — a one-time code + runway-sidecar://pair deep link.
export interface PairingCode {
  code: string;
  expires_at: string;
  server_url: string;
  deep_link: string;
}

export interface AppConfig {
  browser_preference?: string | null;
  default_poll_interval_seconds?: number;
  user_timezone?: string | null;
  env_timezone?: string | null;
  // Update channel sidecars track for the "update available" check.
  sidecar_update_channel?: SidecarChannel | null;
  // Fleet-wide opt-in: when true, sidecars self-install available updates
  // (a sidecar's explicit local config overrides this).
  sidecar_auto_update?: boolean | null;
  /** Fleet-wide default: keep-alive on for every sidecar without its own override. */
  sidecar_keep_alive_default?: boolean | null;
  /** Stored Claude cloud-runner collector switch; null/absent = none stored (the env default applies). */
  claude_cloud_collector_enabled?: boolean | null;
  /** The collector switch in effect: the stored value, else the env default. */
  claude_cloud_collector_effective?: boolean;
  /** Extra worktree directory patterns for project labels (added to the built-ins). */
  worktree_patterns?: string[];
  /** The built-in worktree directory names (read-only). */
  worktree_builtin_patterns?: string[];
}

export interface Webhook {
  id: number;
  provider_id: string;
  account_id: string | null; // null = applies to all accounts
  threshold_pct: number;
  url: string;
  channel: 'discord' | 'slack';
  active: boolean;
  credential_alerts: boolean; // also alert when Token Health goes expired/invalid
  last_fired_at?: string | null;
  validation_error?: string | null; // legacy destinations blocked by current webhook policy
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

// One account's row of the Claude cloud-runner collector's health record
// (numbers, enums and the account id only; never session ids or error text).
export interface ClaudeCloudAccountHealth {
  account_id: string;
  last_outcome: string;
  last_cycle_at: number; // epoch seconds
  last_events: number;
  consecutive_failures: number;
  backlog: boolean;
  healthy: boolean;
}

export interface ClaudeCloudHealth {
  enabled: boolean;
  accounts: ClaudeCloudAccountHealth[];
}

export interface CollectorStatus {
  claude_cloud?: ClaudeCloudHealth;
  [key: string]: unknown;
}

// --- Debug/raw per-strategy capture -----------------------------------------

export interface StrategyCaptureRequest {
  method: string;
  url: string;
  timestamp: number;
}

export interface StrategyCaptureResponse {
  url: string;
  method: string;
  status: number;
  headers: Record<string, string>;
  body: unknown;
  timestamp: number;
}

export interface StrategyCaptureError {
  type: string;
  message: string;
}

export interface StrategyCapture {
  label: string;
  kind: 'primary' | 'enrichment';
  status: 'success' | 'error' | 'skipped';
  cards_returned: number;
  cards_summary: Array<{
    service_name?: string;
    remaining?: string;
    error_type?: string;
    detail?: string;
  }>;
  requests: StrategyCaptureRequest[];
  responses: StrategyCaptureResponse[];
  errors: StrategyCaptureError[];
}

/** One credential source's result from `POST /system/debug/sources/{provider}`. A source that
 * was not called (`probed: false`) says why in `outcome`. Never carries a secret. */
export type SourceProbeOutcome =
  | 'healthy'
  | 'degraded'
  | 'auth_failed'
  | 'unavailable'
  | 'waiting_on_machine'
  | 'disabled'
  | 'pending'
  | 'over_limit';

export interface SourceProbeResult {
  source_id: string;
  outcome: SourceProbeOutcome | string;
  probed: boolean;
  http_status?: number | null;
  error_type?: string | null;
  message?: string | null;
  cards?: number;
  duration_ms?: number;
}

export interface SourceProbeResponse {
  provider_id: string;
  account_id: string;
  probed_at: string;
  /** More sources than one probe will call: the rest are listed as `over_limit`. */
  truncated?: boolean;
  sources: SourceProbeResult[];
}

/** Private operator input, never returned by the Chat fixture diagnostic. */
export interface ClaudeChatFixtureRequest {
  account_id: string;
  source_id: string;
  organization_id: string;
  conversation_id: string;
}

export type ChatValueShape = 'missing' | 'null' | 'empty_object' | 'object'
  | 'array' | 'string' | 'boolean' | 'number';
export type ChatCounterShape = 'missing' | 'null' | 'zero' | 'positive_integer'
  | 'invalid_number' | 'boolean' | 'string' | 'array' | 'object';
export interface ChatUsageScope {
  records: number;
  shapes: Record<ChatValueShape, number>;
  fields: Record<'input_tokens' | 'output_tokens' | 'cache_read_input_tokens'
    | 'cache_creation_input_tokens', Record<ChatCounterShape, number>>;
  /** Shape of token_count only; never interpreted as consumed tokens. */
  context_count_shapes: Record<ChatCounterShape, number>;
}

/** Counts of fixed field shapes, with no transcript, identifiers or token values. */
export interface ClaudeChatFixtureReport {
  format_version: 1;
  outcome: 'observed' | 'busy' | 'source_unavailable' | 'source_ineligible'
    | 'unsupported_source' | 'source_changed' | 'expired' | 'identity_mismatch'
    | 'identity_unavailable' | 'organization_mismatch' | 'target_mismatch'
    | 'challenge' | 'rejected' | 'rate_limited' | 'unavailable' | 'timeout' | 'response_limit'
    | 'unsupported_contract';
  stage: 'source' | 'account' | 'organizations' | 'conversation' | 'complete';
  requests: number;
  http_status: number | null;
  account_verified: boolean;
  organization_verified: boolean;
  target_verified: boolean;
  messages: number;
  roles: Record<'human' | 'assistant' | 'unknown', number>;
  identities: Record<'present' | 'missing' | 'invalid' | 'duplicate', number>;
  usage: Record<'conversation' | 'messages', ChatUsageScope>;
  history_complete: false;
  exact_token_accounting_verified: false;
  provenance_verified: false;
  cloud_contract_verified: false;
  ready_for_ingestion: false;
}

/** Explicit read-only sample. IDs are inputs only; no credentials are accepted. */
export interface ClaudeCloudProbeRequest {
  account_id: string;
  source_id: string;
  organization_id?: string | null;
  session_id?: string | null;
  max_history_pages?: number;
  report_version?: 1 | 2;
}

export type CloudStructureLocation = 'event' | 'data' | 'content' | 'message' | 'payload'
  | 'event.event' | 'data.message' | 'content.message' | 'event.event.message';
export interface CloudStructure {
  events: number;
  locations: Record<CloudStructureLocation, {
    types: Record<'missing' | 'null' | 'boolean' | 'number' | 'string' | 'array' | 'object', number>;
    categories: Record<'assistant' | 'result' | 'user' | 'system' | 'tool_result' | 'tool_use'
      | 'stream_event' | 'message' | 'text' | 'thinking' | 'other' | 'missing' | 'non_string' | 'not_object', number>;
    fields: Record<'type' | 'event' | 'message' | 'data' | 'content' | 'payload' | 'usage' | 'modelUsage' | 'role', number>;
  }>;
}

export type ClaudeCloudProbeReport = ClaudeCloudProbeFields & (
  | { format_version: 1; observations: CloudUsageObservation<'event' | 'data' | 'content'>[] }
  | { format_version: 2; structure: CloudStructure; observations: CloudUsageObservation<'event' | 'data' | 'content' | 'payload'>[] }
);

/** Independent, unverified envelope audit; never combine locations into consumption. */
interface CloudUsageObservation<L extends string> {
  location: L;
  report: Record<string, unknown>;
}

interface ClaudeCloudProbeFields {
  outcome:
    | 'sample_complete' | 'sample_limited' | 'sample_not_found' | 'no_sessions' | 'busy'
    | 'source_unavailable' | 'source_ineligible' | 'unsupported_source' | 'source_changed'
    | 'expired' | 'identity_mismatch' | 'identity_unavailable' | 'rejected' | 'rate_limited'
    | 'unavailable' | 'timeout' | 'response_limit' | 'unsupported_contract';
  stage: 'source' | 'profile' | 'sessions' | 'history' | 'complete';
  requests: number;
  http_status: number | null;
  account_verified: boolean;
  organization_verified: boolean;
  session_list_records: number;
  session_list_complete: boolean | null;
  history_pages: number;
  history_events: number;
  history_complete: boolean | null;
  sequence: { numeric: number; invalid: number; duplicates: number; gaps: number; out_of_order: number };
  cloud_contract_verified: false;
  ready_for_ingestion: false;
}

export interface DebugRawResponse {
  provider_id: string;
  account_id?: string | null;
  is_configured: boolean;
  credentials: { token_found: boolean; token_source: string | null };
  active_strategy: string | null;
  active_strategy_card_count: number;
  strategies: Record<string, StrategyCapture>;
  timestamp: number;
}

// --- Data Health -------------------------------------------------------------
// Mirrors app/services/data_health/base.py's dataclasses and the
// DataHealth* Pydantic models in app/models/schemas.py.

export type DataHealthSeverity = 'error' | 'warn' | 'info';

export interface DataHealthFinding {
  label: string;
  detail: Record<string, unknown>;
}

export interface DataHealthParamSpec {
  name: string;
  label: string;
  required: boolean;
  options?: string[] | null;
}

export interface DataHealthFindingGroup {
  key: string;
  label: string;
  count: number;
  fixable: boolean;
  not_fixable_reason?: string | null;
  params: DataHealthParamSpec[];
  samples: DataHealthFinding[];
  detail: Record<string, unknown>;
}

export interface DataHealthCheckReport {
  check_id: string;
  title: string;
  description: string;
  impact: string;
  recommended_action: string;
  severity: DataHealthSeverity;
  total_count: number;
  fixable_count: number;
  groups: DataHealthFindingGroup[];
  blocked_by: string[];
  blocked_by_titles: string[];
  blocked: boolean;
}

export interface DataHealthReport {
  scanning: boolean;
  checks: DataHealthCheckReport[];
  scan_error?: string | null;
  last_scanned_at?: string | null;
}

export interface DataHealthFixPlan {
  check_id: string;
  group_key: string;
  summary: string;
  counts: Record<string, unknown>;
  samples: DataHealthFinding[];
  confirmation_text?: string | null;
}

export interface DataHealthFixResult {
  check_id: string;
  group_key: string;
  summary: string;
  counts: Record<string, unknown>;
}

export type DataHealthJobStatus = 'running' | 'succeeded' | 'failed';

/** What kind of failure a fix ended in. */
export type DataHealthErrorKind = 'db_busy' | 'unknown';

export interface DataHealthJob {
  id: string;
  check_id: string;
  group_key: string;
  status: DataHealthJobStatus;
  result?: DataHealthFixResult | null;
  /** The plain-language reason the fix failed — never the raw exception. */
  error?: string | null;
  error_kind?: DataHealthErrorKind | null;
  /** Whether running the same fix again could succeed. */
  retryable?: boolean;
  started_at: string;
  finished_at?: string | null;
}

/** Explicit #956 operator protocol. No action grants consent or refreshes tokens. */
export type ClaudeVerificationCategory = 'cli_code' | 'desktop_code' | 'remote_code' | 'cowork'
  | 'ordinary_chat' | 'mobile_only' | 'unknown';
export interface ClaudeVerificationCounters {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_creation_input_tokens: number;
}
export interface ClaudeVerificationLocalRecord {
  type: 'assistant';
  message: { id: string; usage: ClaudeVerificationCounters };
  /** Job-scoped HMACs only; raw SDK identifiers are not accepted. */
  parent_tool_use_id?: string | null;
  session_scope?: string | null;
  request_scope?: string | null;
  record_version?: string | null;
  scope_conflict?: boolean;
  scope_unknown?: boolean;
  event_sequence?: number | null;
  sdk_timestamp?: number | null;
  provider_timestamp?: number | null;
  sdk_timestamp_unknown?: boolean;
  provider_timestamp_unknown?: boolean;
}
export type ClaudeVerificationAction = {
  action: 'start'; account_id: string; source_id: string;
  organization_id?: string | null; session_id?: string | null;
  job_key?: string | null; expected_local?: ClaudeVerificationLocalRecord[];
  declared_category?: ClaudeVerificationCategory;
} | { action: 'step' | 'cancel'; job_handle: string }
  | { action: 'status'; job_handle?: string | null }
  | { action: 'archive_status'; job_handle: string }
  | { action: 'archive'; job_handle: string; revision: number };
export interface ClaudeVerificationComparison {
  local_messages: number; cloud_messages: number; matched_messages: number;
  missing_messages: number; additional_cloud_messages: number; mismatched_messages: number;
  local_conflicts: number; cloud_conflicts: number;
  local_unusable_records: number; cloud_unusable_records: number;
  all_expected_input_cache_match: boolean;
  cloud_contract_verified: false; ready_for_ingestion: false;
}
export interface ClaudeVerificationAudit {
  format_version: 1; status: 'incomplete' | 'observations_only';
  source_contract: 'sdk_style_jsonl'; assistant_output_accounting: 'excluded_placeholder';
  cloud_contract_verified: false; ready_for_ingestion: false;
  assistant: {
    records: number; unique_messages: number; duplicate_messages: number;
    conflicting_messages: number; subagent_records: number;
    input_cache_observations: Omit<ClaudeVerificationCounters, 'output_tokens'> | null;
  };
  result: {
    records: number; usage_observations: number; model_usage_observations: number;
    last_usage_observation: ClaudeVerificationCounters | null;
    last_model_usage_observation: ClaudeVerificationCounters | null;
    usage_decreases: number; model_usage_decreases: number; error_results: number;
  };
  diagnostics: ('unsupported_record' | 'subagent_scope_excluded' | 'missing_assistant_identity'
    | 'invalid_assistant_usage' | 'conflicting_assistant_usage' | 'output_placeholder_divergence'
    | 'error_result_observed' | 'result_scope_unverified' | 'no_usage_observations'
    | 'missing_result_usage' | 'invalid_result_usage' | 'missing_result_model_usage'
    | 'invalid_result_model_usage')[];
}
export interface ClaudeVerificationReport {
  format_version: 1;
  /** Transport only; remove before saving or publishing sanitized evidence. */
  job_handle: string;
  started_at: number; deadline: number; last_verified_at: number | null;
  phase: 'listing' | 'history' | 'complete' | 'stopped';
  outcome: 'pending' | 'complete' | 'cancelled' | 'interrupted' | 'expired' | 'limit_reached'
    | 'source_changed' | 'source_unavailable' | 'source_ineligible' | 'unsupported_source'
    | 'identity_mismatch' | 'identity_unavailable' | 'rejected' | 'rate_limited' | 'timeout'
    | 'unavailable' | 'unsupported_contract' | 'response_limit' | 'no_sessions' | 'target_not_found';
  requests: number; accepted_response_bytes: number; http_status: number | null;
  account_verified: boolean; organization_verified: boolean;
  listed_sessions: number; list_pages: number; list_complete: boolean;
  /** Optional on older peers; null means no verified failure-time evidence. */
  rejected_list?: ClaudeRejectedListEvidence | null;
  history_pages: number; history_events: number; history_complete: boolean;
  sequence: { numeric: number; invalid: number; duplicates: number; gaps: number; out_of_order: number };
  audit: ClaudeVerificationAudit;
  comparison: ClaudeVerificationComparison; subagent_comparison: ClaudeVerificationComparison;
  scope_observations: Record<'initializations' | 'session_scopes' | 'parent_scopes'
    | 'unknown_parent_records' | 'request_scopes' | 'record_versions' | 'record_version_changes'
    | 'scope_conflicts' | 'unknown_scope_records' | 'shared_session_scopes' | 'shared_parent_scopes', number>;
  declared_category: ClaudeVerificationCategory;
  execution_kind: 'anthropic_cloud' | 'bridge' | 'other' | 'unknown';
  environment_kind: string | null;
  cloud_contract_verified: false; ready_for_ingestion: false;
  live_acceptance_passed: false; provenance_verified: false;
}
export type ClaudeVerificationResponse = ClaudeVerificationReport | { state: 'absent' | 'cancelled' }
  | { state: 'unavailable'; job_handle: string }
  | { state: 'terminal' | 'not_terminal' | 'archived'; job_handle: string; revision: number };

/** Explicit failed-list inspection; never resumes or modifies the checkpoint. */
export interface ClaudeListBoundaryRequest { job_handle: string }
export type ClaudeBoundaryKind = 'missing' | 'null' | 'boolean' | 'number' | 'string'
  | 'array' | 'object' | 'not_object';
export interface ClaudeListBoundaryObservation {
  top_level_kind: ClaudeBoundaryKind;
  data_kind: ClaudeBoundaryKind;
  record_count: number | null;
  records_over_limit: boolean;
  checked_records: number;
  id_kinds: Record<ClaudeBoundaryKind, number>;
  valid_ids: number;
  invalid_ids: number;
  duplicate_valid_ids: number;
  previously_seen_ids: number;
  cursor_state: 'missing' | 'null' | 'empty_string' | 'nonempty_string' | 'invalid';
  /** Frozen diagnostic-v1 sample cursor rule; excludes verification terminal omission. */
  cursor_supported: boolean;
  cursor_repeated: boolean | null;
  has_more_state: 'missing' | 'true' | 'false' | 'invalid';
}
export interface ClaudeRejectedListEvidence {
  boundary: ClaudeListBoundaryObservation | null;
  response_encoding: 'identity' | 'unsupported';
  json_state: 'not_attempted' | 'decoded' | 'duplicate_keys' | 'nonfinite_values'
    | 'invalid_json' | 'encoding_error' | 'nesting_limit';
  duplicate_json_keys: number;
  nonfinite_json_values: number;
}
export interface ClaudeListBoundaryReport {
  response_encoding: 'unknown' | 'identity' | 'unsupported';
  json_state: 'not_attempted' | 'decoded' | 'duplicate_keys' | 'nonfinite_values'
    | 'invalid_json' | 'encoding_error' | 'nesting_limit';
  duplicate_json_keys: number;
  nonfinite_json_values: number;
  format_version: 1;
  outcome: 'observed' | 'busy' | 'checkpoint_unavailable' | 'checkpoint_ineligible'
    | 'checkpoint_changed' | 'source_changed' | 'identity_mismatch' | 'identity_unavailable'
    | 'expired' | 'rejected' | 'rate_limited' | 'timeout' | 'unavailable'
    | 'unsupported_contract' | 'response_limit';
  stage: 'checkpoint' | 'source' | 'profile' | 'sessions' | 'complete';
  requests: number;
  http_status: number | null;
  account_verified: boolean;
  organization_verified: boolean;
  original_list_pages: number | null;
  original_listed_sessions: number | null;
  boundary: ClaudeListBoundaryObservation | null;
  cloud_contract_verified: false;
  ready_for_ingestion: false;
  live_acceptance_passed: false;
  provenance_verified: false;
}
