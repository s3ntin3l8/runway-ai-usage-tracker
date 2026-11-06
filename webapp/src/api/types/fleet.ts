import type { SidecarChannel } from './settings';

export type SidecarLogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR';

export interface LoginDirectorySettings {
  claude_config_dirs: string[];
  codex_home: string[];
}

export interface LoginDirectoryReport {
  applied: LoginDirectorySettings;
  searched: Partial<Record<'anthropic' | 'chatgpt', string[]>>;
}

/** One of the two Fleet entries a re-paired machine ends up with, described so they can be told apart. */
export interface SidecarDescriptor {
  sidecar_id: string;
  name: string;
  os_platform?: string | null;
  first_seen: string;
  last_seen: string;
  ingest_count: number;
}

/** What merging an old shared-key entry into its paired device moves (or, for a dry run, would move). */
export interface SidecarAdoptSummary {
  dry_run: boolean;
  events: number;
  pending_events: number;
  credential_tags: number;
  /** Per-source enabled/priority choices copied onto the device's matching credential sources. */
  source_prefs?: number;
  settings: string[];
  /** Dry run only: the old entry, the paired device, and the device's push count after the merge. */
  from?: SidecarDescriptor;
  into?: SidecarDescriptor;
  pushes_after?: number;
  /** Real run only. */
  rollup_pairs_rebuilt?: number;
  windows_rebuilt?: number;
  pushes_carried?: number;
  errors_carried?: number;
}

export interface Sidecar {
  /** On a paired device: the old shared-key entries it probably replaced. */
  adoptable_legacy?: SidecarDescriptor[];
  /** On an old shared-key entry: the paired device(s) that replaced it. */
  replaced_by?: SidecarDescriptor[];
  /** Paired devices only (null/absent on a legacy sidecar). `never` = past the grace period
   *  without a completed `POST /fleet/source-sync`; `stale` = it synced once, not for an hour. */
  source_sync_state?: 'ok' | 'never' | 'pending' | 'stale' | null;
  source_sync_last_at?: string | null;
  login_dirs_desired?: LoginDirectorySettings;
  login_dirs_report?: LoginDirectoryReport | null;
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
  // Release channel derived from the version string ("1.0.0+edge.<sha>" → edge).
  channel?: 'stable' | 'beta' | 'edge';
  os_platform?: string;
  collection_enabled?: boolean;
  collection_errors?: string[] | null;
  last_log_lines?: string[] | null;
  // Update status computed server-side by fleet_registry.to_dict(). A stale
  // sidecar (no check-in within stale_threshold_minutes) never reports
  // update_available=true — it can't receive the push either way.
  update_available?: boolean;
  latest_version?: string | null;
  // Whether the build can self-update in place (frozen, non-Docker). null = not
  // reported; false = from-source/Docker (no update push offered).
  self_update_capable?: boolean | null;
  // Auto-update and update channel. `*_desired` is the operator's per-sidecar override (null =
  // follow the fleet-wide setting); `effective_*` is what the dashboard tells the sidecar to use;
  // the plain fields are what the sidecar reports it uses (null = an older sidecar, or no
  // check-in yet) — they differ when its own config.json / RUNWAY_UPDATE_CHANNEL wins.
  auto_update?: boolean | null;
  auto_update_desired?: boolean | null;
  effective_auto_update?: boolean;
  update_channel?: SidecarChannel | null;
  update_channel_desired?: SidecarChannel | null;
  effective_update_channel?: SidecarChannel;
  // Log level: `log_level` is what the sidecar reports it logs at (null = a sidecar too old to
  // report it, so it can't be changed from here); `log_level_desired` the operator's override
  // (null = the sidecar's own config.json level).
  log_level?: SidecarLogLevel | null;
  log_level_desired?: SidecarLogLevel | null;
  // Whether the sidecar runs with --keep-alive (renews the logins in keepAlive.ts itself). null = not
  // reported by an older sidecar — treat as unknown, not off.
  keep_alive?: boolean | null;
  // The operator's remote keep-alive setting (null = no preference; the sidecar's own flag decides).
  keep_alive_desired?: boolean | null;
  // What the sidecar runs per login ({provider_id: running}). null = a sidecar too old to report
  // it, so per-login settings can't be applied.
  keep_alive_providers?: Record<string, boolean> | null;
  // The operator's per-login overrides on top of the sidecar-level setting (only overridden logins).
  keep_alive_desired_providers?: Record<string, boolean> | null;
  // One-shot "Update now" push queued but not yet delivered (persisted
  // server-side; survives a server restart until the sidecar's next
  // successful ingest — see fleet_registry.set_pending_update/consume_pending_update).
  pending_update?: boolean;
  stale?: boolean;
  stale_threshold_minutes?: number;
  // A newer build exists, regardless of whether it can be pushed right now
  // (#202) — lets an offline sidecar still show that it is behind.
  outdated?: boolean;
  // Per event provider: which account this sidecar stamped its data with in
  // the latest cycle, and why ("local" discovery, operator "tag"/auto-hint,
  // or "default" when nothing resolved).
  identity_sources?: Record<string, SidecarIdentitySource>;
  [key: string]: unknown;
}

export interface SidecarIdentitySource {
  account_id: string;
  source: 'local' | 'tag' | 'default' | string;
}

export interface PendingUsageEvent {
  id: number;
  provider_id: string;
  event_id: string;
  sidecar_id: string;
  ts: string;
  reason: string;
  model_id?: string | null;
  session_id?: string | null;
}

export interface PendingUsageSession {
  provider_id: string;
  sidecar_id: string;
  session_id: string | null;
  event_ids: number[];
  event_count: number;
  first_ts: string;
  last_ts: string;
  model_ids: string[];
}

export interface PendingUsageSessionsResponse {
  items: PendingUsageSession[];
  /** Events across all unassigned groups, before the current filters. */
  total_events: number;
  /** Events in groups matching the current filters. */
  matching_events: number;
  total_groups: number;
  sidecars: string[];
  providers: string[];
  offset: number;
  limit: number;
}

export interface PendingUsageFilter {
  sidecar_id?: string;
  provider_id?: string;
  search?: string;
}

export interface PendingUsageAssignmentGroup {
  event_ids: number[];
  account_id: string;
  // Only set when the account belongs to a related provider (e.g. antigravity).
  target_provider_id?: string;
}

/** One provider group of an /events/pending/assign response. */
export interface PendingUsageAssignResult {
  provider_id: string | null;
  target_provider_id: string;
  account_id: string;
  assigned: number;
}

export interface PendingUsageMapping {
  provider_id: string;
  sidecar_id: string;
  target_provider_id: string;
  account_id: string;
}

/** Per-scraper consent and collection are independent of device liveness. */
export interface FleetSourceStatus {
  source_id: string;
  provider_id: string;
  label: string;
  revision: string;
  generation: number;
  read: boolean;
  state: 'Off' | 'Setup required' | 'Connecting' | 'Ready' | 'Limited' | 'Action required' | 'Withdrawal pending';
  renewal_requested: boolean;
  renewal_supported: boolean;
  renewal_capable: boolean;
  last_read_at?: number | null;
  last_renewed_at?: number | null;
  new: boolean;
  remote_paused: boolean;
  revoked: boolean;
  report_at: string;
  report_stale: boolean;
  announcement: boolean;
  collection_health: 'withdrawn' | 'paused' | 'healthy' | 'action_required' | 'untried';
  last_collection_at: string | null;
}

export type FleetSourceAction = 'pause' | 'resume' | 'revoke' | 'request_setup' | 'dismiss';
export interface FleetSourceCommand {
  id: number;
  source_id: string;
  generation: number;
  action: FleetSourceAction;
}
export interface FleetSourceResponse {
  protocol: 1;
  sources: FleetSourceStatus[];
  commands: FleetSourceCommand[];
}
