export interface CollectionStrategy {
  id: string;
  enabled: boolean;
  [key: string]: unknown;
}

export interface CredentialSourceSummary {
  source_id: string;
  source_type: 'config' | 'sidecar' | 'env' | 'file' | 'cookie' | 'keychain' | 'server' | string;
  source_label: string;
  /** Extra context for the origin: the browser and profile a cookie came from. */
  source_detail?: string | null;
  sidecar_id?: string | null;
  enabled: boolean;
  priority: number;
  last_seen?: string | null;
  health: 'untried' | 'healthy' | 'auth_failed' | 'unavailable' | 'degraded' | string;
  available: boolean;
}

/** Per-account row in a provider's ``accounts`` list. Multi-account
 *  providers expose N of these under a ``ProviderConfig`` envelope. */
// The billing models an account can have. Mirrors `BillingType` in
// `app/services/billing_type.py` — the detector, the API and the UI speak
// about the same set.
export type BillingType = 'subscription' | 'pay_as_you_go' | 'unknown';

/**
 * An account that was merged away, and the account it became. The merge
 * moved the account's history and its credential to the destination, so the
 * merged-away account holds nothing — this is what says where it went.
 */
export interface MergedAccount {
  /** The account that was merged away. */
  account_id: string;
  /** Where it ended up (a chain of merges is followed to its end). */
  destination_account_id: string;
  /** When the merge happened (server timestamp, UTC). */
  merged_at: string;
}

/**
 * What undoing a merge would put back — and what it cannot. The duplicates
 * the merge dropped and the credential it wiped do not come back, and the
 * preview says so instead of losing them silently.
 */
export interface UnmergePlan {
  provider_id: string;
  source_account_id: string;
  destination_account_id: string;
  /** The rows that go back, per table. */
  rows: Record<string, number>;
  credential_sources: string[];
  credential_tags: number;
  contributions: number;
  /** What happened to the source's provider row: renamed | archived | absent. */
  config: string;
  total_collisions: number;
  /** The rows that got in the way, per table — replaced by the rows the merge moved. */
  rows_in_the_way: Record<string, number>;
  not_restored: string[];
}

export interface ProviderAccount {
  account_id: string;
  // Which provider this account belongs to. The Claude family groups several
  // providers under one card, so an account listed under the family is still
  // addressed by its own provider — an edit must never land on the family's
  // representative (#723). Optional for backward compatibility with older
  // servers; the UI falls back to the provider it was listed under.
  provider_id?: string;
  enabled?: boolean;
  archived?: boolean; // PR #292 — hide discontinued providers from the dashboard
  // Where this account went, when it was merged away. The row itself stays
  // behind (archived, no credential) — the merge is what retired the
  // account, not the archive. Absent from an older server.
  merged_into?: string | null;
  has_usage_events?: boolean;
  api_key_set?: boolean;
  session_cookie_set?: boolean;
  account_label?: string | null;
  poll_interval_seconds?: number | null;
  collection_strategies?: CollectionStrategy[] | null;
  opencode_workspace_id?: string | null;
  claude_organization_id?: string | null;
  billing_type?: BillingType;
  // Set by the server (#281 hardening + #286 follow-up). Lets the UI surface
  // orphaned `account_id="default"` rows that have been shadowed by a real
  // sibling account on the same provider. Safe to remove. The flag only
  // fires when a non-default sibling has live data — without a replacement,
  // "just-configured" and "collection currently failing" rows would be
  // flagged too, and pairing that with the destructive Remove button would
  // be a data-loss prompt on the user's only credential.
  is_orphaned?: boolean;
  // Where this account entry came from. "config" = a provider_configs DB
  // row; "discovered" = only present in token_cache / latest_usage (sidecar
  // or local discovery — passive providers like antigravity never get a
  // config row). Optional for backward compatibility with older servers.
  source?: 'config' | 'discovered';
  /** Non-secret source descriptions backed by matching sidecar token metadata. */
  credential_source_labels?: string[];
  credential_sources?: CredentialSourceSummary[];
}

// Provider envelope returned by GET /api/v1/system/provider-configs. The
// legacy top-level fields stay populated for single-account consumers
// (HomePage/HistoryPage/ProviderPage read only these).
// `accounts` carries the full per-account breakdown for the settings UI.
export interface ProviderConfig {
  provider_id: string;
  name: string;
  icon?: string;
  // Legacy single-account view: derived from the canonical row
  // (account_id="default" if it exists, else the first row).
  enabled?: boolean;
  archived?: boolean;
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
  opencode_workspace_id?: string | null;
  claude_organization_id?: string | null;
  // Multi-account view (one entry per ProviderConfig DB row, plus any
  // discovered-only identities — see ProviderAccount.source).
  accounts: ProviderAccount[];
  // Total entries in `accounts` (config-backed + discovered).
  account_count: number;
  archived_count?: number;
  /**
   * The accounts that were merged away, and where each of them went. The row
   * a merge left behind stays in `accounts` (archived, no credential) and
   * carries `merged_into`.
   */
  merged_accounts?: MergedAccount[];
}

// Wizard (#287) preview-endpoint types — keep in sync with
// `_AccountPreviewRequest` / preview_account_identity response in
// `app/api/endpoints/system_provider_config.py`.
export type AccountPreviewLabelSource = 'email' | 'credential_hash' | 'default' | 'organization';

export interface AccountPreviewRequest {
  provider_id: string;
  organization_id?: string;
  api_key?: string | null;
  session_cookie?: string | null;
  // Ask the server to prove how the account behind this credential bills.
  // Off by default — the wizard debounces keystrokes into the preview, and a
  // proof costs a round trip to Claude. The operator asks for it explicitly.
  detect_billing?: boolean;
}

export interface AccountPreviewResponse {
  suggested_account_id: string;
  suggested_label: string | null;
  label_source: AccountPreviewLabelSource;
  // How the account bills, when the server proved it. Absent when it did
  // not — the operator then decides.
  suggested_billing_type?: BillingType | null;
  already_exists: boolean;
}
