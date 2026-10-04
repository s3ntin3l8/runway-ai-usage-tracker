// Test fixtures for the Credentials views.
import type {
  CredentialAccountView,
  CredentialInventory,
  CredentialSourceView,
} from '@/api/types';

export function source(o: Partial<CredentialSourceView> = {}): CredentialSourceView {
  return {
    source_id: 'sidecar:a',
    provider_id: 'gemini',
    account_id: 'alice@example.com',
    origin_kind: 'machine',
    origin_type: 'file',
    label: 'oauth_creds.json',
    machine_id: 'host-a',
    machine_name: 'Workstation',
    mapping: 'local',
    mapping_scope: null,
    fingerprinted: false,
    identity_pending: false,
    status: 'valid',
    expires_at: null,
    expires_in_seconds: 7200,
    token_types: ['oauth_token', 'refresh_token'],
    can_refresh: false,
    rollable: false,
    refreshed_by: null,
    keep_alive: null,
    rejected: false,
    redundant: false,
    removable: true,
    enabled: true,
    priority: 0,
    live: true,
    is_active: false,
    health: 'healthy',
    last_seen: new Date().toISOString(),
    last_attempt_at: null,
    last_success_at: null,
    last_error: null,
    ...o,
  };
}

export function account(
  sources: CredentialSourceView[],
  o: Partial<CredentialAccountView> = {},
): CredentialAccountView {
  return {
    provider_id: 'gemini',
    account_id: 'alice@example.com',
    account_label: null,
    status: 'valid',
    identity_pending: false,
    active_source_id: null,
    data_source: null,
    input_source: null,
    sources,
    ...o,
  };
}

export function inventory(o: Partial<CredentialInventory> = {}): CredentialInventory {
  return {
    providers: [],
    machines: [],
    unmapped_count: 0,
    rule_count: 0,
    pending_usage_events: 0,
    ...o,
  };
}

/** The scenario from the original bug: one account's credential on several machines. */
export function multiMachineInventory(): CredentialInventory {
  const sources = [
    source({
      source_id: 'sidecar:a',
      machine_id: 'dev-01',
      machine_name: 'DEV-01',
      is_active: true,
      last_success_at: new Date().toISOString(),
    }),
    source({ source_id: 'sidecar:b', machine_id: 'macbook', machine_name: 'MacBook' }),
    source({ source_id: 'sidecar:c', machine_id: 'mgmt', machine_name: 'mgmt', status: 'stale' }),
  ];
  return inventory({
    providers: [
      {
        provider_id: 'gemini',
        name: 'Gemini',
        accounts: [
          account(sources, {
            account_label: 'Work',
            active_source_id: 'sidecar:a',
            data_source: 'api',
            input_source: 'sidecar',
          }),
        ],
      },
    ],
    machines: [
      { machine_id: 'dev-01', name: 'DEV-01', last_seen: null, credential_count: 1, unmapped_count: 0 },
      { machine_id: 'macbook', name: 'MacBook', last_seen: null, credential_count: 1, unmapped_count: 2 },
      { machine_id: 'mgmt', name: 'mgmt', last_seen: null, credential_count: 1, unmapped_count: 0 },
    ],
  });
}

/** An inventory with one provider/account holding the given sources (for banner tests). */
export function inventoryWith(
  sources: CredentialSourceView[],
  o: { provider_id?: string; account_id?: string; account_label?: string | null } = {},
): CredentialInventory {
  const provider_id = o.provider_id ?? 'zai';
  const account_id = o.account_id ?? 'default';
  return inventory({
    providers: [
      {
        provider_id,
        name: provider_id,
        accounts: [account(sources, { provider_id, account_id, account_label: o.account_label ?? null })],
      },
    ],
  });
}
