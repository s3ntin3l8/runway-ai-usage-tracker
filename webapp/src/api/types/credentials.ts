import type { LoginDirectoryReport } from './fleet';
import type { MergedAccount } from './provider';

// Silent-listener pending entry — a credential the sidecar reported but
// the operator hasn't tagged yet. Surfaces on the fleet view (PR #288).
export interface UntaggedCredential {
  sidecar_id: string;
  provider_id: string;
  claimed_account_id?: string | null;
  source_id?: string | null;
  selection_required?: boolean;
  organization_count?: number;
  // Stable host-side descriptor; phase 1 ships `provider:<id>`,
  // per-rule expansion is the follow-up issue.
  credential_origin: string;
  /** What the credential is, in words: "Browser cookie", "auth.json", … */
  origin_label?: string;
  /** The tool that owns it: "Chrome" for a native Claude cookie, "Codex CLI" for a file, … */
  origin_app?: string | null;
  /** Extra context for the origin: the browser and profile a cookie came from. */
  origin_detail?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
  quota_preview_observed_at?: string | null;
  quota_preview_stale?: boolean;
  quota_preview?: Array<{
    service_name?: string;
    remaining?: string | number;
    unit?: string;
    unit_type?: string;
    pct_used?: number;
    window_type?: string;
    reset?: string;
    reset_at?: string;
  }>;
}

// Response shape of GET /api/v1/fleet/credentials/tags/pending — both the
// flat item list and the per-sidecar counts the banner uses.
export interface UntaggedCredentialsList {
  items: UntaggedCredential[];
  counts_by_sidecar: Record<string, number>;
}

// Body for POST /api/v1/fleet/credentials/tags — operator resolves a
// pending entry into a server-side account_id by selecting one of the
// provider_configs rows for the entry's provider_id.
// ``scope`` (#319): "sidecar" (default) persists the tag for
// ``sidecar_id`` only ("This machine"); "deployment" persists a
// deployment-wide tag (sidecar_id NULL) that applies to every machine.
export interface CredentialTagRequest {
  sidecar_id: string;
  provider_id: string;
  credential_origin: string;
  account_id: string;
  scope?: 'sidecar' | 'deployment';
}

// Resolved credential tag (GET /fleet/credentials/tags). ``sidecar_id`` is
// ``null`` for a deployment-wide ("All machines") tag.
export interface CredentialTag {
  provider_id: string;
  credential_origin: string;
  account_id: string;
  sidecar_id: string | null;
  set_by: string;
  // Provider owning account_id when it differs from provider_id (e.g. Gemini
  // usage assigned to an Antigravity account); null = same provider.
  target_provider_id?: string | null;
  set_at: string | null;
  /** "auth.json", "OPENROUTER_API_KEY", "Browser cookie" … */
  origin_label?: string;
  origin_app?: string | null;
  origin_path?: string | null;
  /** Extra context for the origin: the browser and profile a cookie came from. */
  origin_detail?: string | null;
  /** Freshest machine-reported credential this rule resolved; null = never. */
  last_matched_at?: string | null;
  /** True when the `stale_credential_rules` check would list this rule. */
  stale?: boolean;
}

export interface CredentialTagList {
  items: CredentialTag[];
}

/** `failing` = not rejected, but the last several collections with it failed. `invalid` = the provider rejected the credential (401/403) — opaque keys and
 * cookies carry no expiry, so this is the only way they ever go bad. `stale` = a
 * sidecar-reported credential that hasn't been re-reported recently (machine gone). */
export type TokenHealthStatus =
  | 'valid'
  | 'expiring'
  | 'expired'
  | 'invalid'
  | 'failing'
  | 'stale'
  | 'unknown'
  | string;

// ── Credential inventory (`GET /api/v1/system/credentials`) ─────────────────
// One row per credential *source* (a secret found in one place), grouped
// provider → account. Never carries secret values.

/** Why a credential belongs to its account. */
export type CredentialMapping =
  | 'local' // identity read on the machine
  | 'verified' // the server resolved it
  | 'claim' // the sidecar claimed it
  | 'operator' // an assignment rule
  | 'rotation' // carried over from the same location after a re-login re-keyed it
  | 'config' // pasted in Settings → Providers
  | 'server' // env var / file on the server host
  | 'pending'; // not mapped yet

export interface CredentialSourceView {
  source_id: string;
  provider_id: string;
  /** Storage key; may be a placeholder while identity is pending. */
  account_id: string;
  /** `machine` = a sidecar found it, `config` = pasted key, `server` = env/file on the server. */
  origin_kind: 'machine' | 'config' | 'server';
  /** What the credential is: `file`, `env`, `cookie`, `keychain`, or `sidecar` for an unrecognised origin. */
  origin_type: string;
  label: string;
  /** Tool that owns the file ("Codex CLI", "OpenCode", …); null when unknown. */
  origin_app?: string | null;
  /** File path with the home directory collapsed to `~`. */
  origin_path?: string | null;
  /** Extra context for the origin: the browser and profile a cookie came from
   * ("Chrome · Profile 1"), the keychain service, …; null when there is none. */
  origin_detail?: string | null;
  /** How to re-authenticate the owning tool ("run `codex login`"). */
  login_hint?: string | null;
  machine_id?: string | null;
  machine_name?: string | null;
  /** The machine has stopped checking in (the Fleet page's stale rule). */
  machine_stale?: boolean;
  /** Other machines reporting the same secret (matched by fingerprint, never returned). */
  shared_with?: string[];
  /** The subset of `shared_with` on machines that stopped checking in. */
  shared_with_stale?: string[];
  mapping: CredentialMapping;
  mapping_scope?: 'machine' | 'all_machines' | null;
  fingerprinted: boolean;
  identity_pending: boolean;
  status: TokenHealthStatus;
  expires_at?: string | null;
  expires_in_seconds?: number | null;
  token_types: string[];
  can_refresh: boolean;
  rollable: boolean;
  refreshed_by: 'server' | 'machine' | null;
  /** Sidecar keep-alive on the owning machine; null = not applicable to this credential. */
  keep_alive?: 'on' | 'off' | 'unknown' | null;
  /** The operator's server-side keep-alive override for that sidecar (null = none); compare with
   * `keep_alive` (what it runs) to show a pending request. */
  keep_alive_desired?: boolean | null;
  /** The provider rejected this credential (HTTP 401/403 at the last collection). */
  rejected: boolean;
  /** Expired and unrefreshable, but another healthy credential for the same account can stand
   * in for it: not blocking collection, so it should not raise a hard dashboard alert. */
  redundant: boolean;
  removable: boolean;
  enabled: boolean;
  priority: number;
  live: boolean;
  /** Produced this account's most recent successful collection. */
  is_active: boolean;
  health: string;
  last_seen?: string | null;
  last_attempt_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
  /** Why a server env/file credential that is present isn't feeding any collection. */
  unused_reason?: CredentialUnusedReason | null;
}

export type CredentialUnusedReason =
  | 'provider_disabled'
  | 'default_disabled'
  | 'account_keyed_config'
  | 'shadowed_by_config_key';

export interface CredentialAccountView {
  provider_id: string;
  account_id: string;
  account_label?: string | null;
  /** The best status among the account's enabled sources. */
  status: TokenHealthStatus;
  identity_pending: boolean;
  active_source_id?: string | null;
  data_source?: string | null;
  input_source?: string | null;
  sources: CredentialSourceView[];
  /** The account is retired (its provider_configs row is archived). */
  archived?: boolean;
}

export interface CredentialProviderView {
  provider_id: string;
  name: string;
  accounts: CredentialAccountView[];
}

export interface CredentialMachineView {
  login_dirs_report?: LoginDirectoryReport | null;
  machine_id: string;
  name: string;
  last_seen?: string | null;
  credential_count: number;
  unmapped_count: number;
  stale?: boolean;
}

/** A credential a machine found but could not map to an account, whose provider is not
 * collecting from any other source — the quota behind it stays dark until it is assigned. */
export interface BlockedCollection {
  sidecar_id: string;
  provider_id: string;
  credential_origin: string;
  first_seen?: string | null;
}

/** A credential source a machine found but could not read — a browser cookie store the
 * browser holds open, or one whose values are app-bound encrypted. The provider is not
 * "not signed in": its credential exists, the sidecar just cannot read it. */
export interface BlockedSource {
  sidecar_id: string;
  provider_id: string;
  credential_origin: string;
  source_store?: string | null;
  /** source_locked | source_unsupported | source_unreadable */
  reason: string;
  detail?: string | null;
  remedy?: string | null;
  /** Whether another source covers the provider anyway. */
  collecting?: boolean;
  first_seen?: string | null;
  last_seen?: string | null;
}

export interface CredentialFileMigrationNotice {
  providers: { provider_id: string; name: string }[];
  removed_count: number;
  recorded_at: string;
}

export interface CredentialInventory {
  credential_file_migration_notice?: CredentialFileMigrationNotice | null;
  providers: CredentialProviderView[];
  machines: CredentialMachineView[];
  unmapped_count: number;
  rule_count: number;
  pending_usage_events: number;
  /** Absent from an older server. */
  blocked_collection?: BlockedCollection[];
  /** Absent from an older server. */
  blocked_sources?: BlockedSource[];
  /**
   * The accounts that were merged away, and where each of them went. The
   * inventory lists the accounts that hold a credential, and a merged-away
   * account holds none — so it is listed here instead. Absent from an older
   * server.
   */
  merged_accounts?: (MergedAccount & { provider_id: string })[];
}
