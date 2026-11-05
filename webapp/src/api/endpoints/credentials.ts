import { api, qs } from '../client';
import type {
  AccountPreviewRequest,
  AccountPreviewResponse,
  ClaudeCloudProbeReport,
  ClaudeCloudProbeRequest,
  CredentialInventory,
  DebugRawResponse,
  ProviderConfig,
  SourceProbeResponse,
  UnmergePlan,
} from '../types';

export const fetchProviderConfigs = () =>
  api<{ providers: ProviderConfig[] }>('/api/v1/system/provider-configs');

export const putDiscoveredAccountLabel = (providerId: string, accountId: string, account_label: string | null) =>
  api<{ status: string }>(
    `/api/v1/system/provider-account-label/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}`,
    { method: 'PUT', body: JSON.stringify({ account_label }) },
  );

export interface ProviderConfigUpdate {
  enabled?: boolean;
  archived?: boolean;
  api_key?: string;
  session_cookie?: string;
  // Explicit-clear flags (#287). When set to true, the corresponding
  // stored credential is wiped server-side. Wins over a same-field write
  // (the UI sends one or the other, not both).
  clear_api_key?: boolean;
  clear_session_cookie?: boolean;
  account_label?: string;
  poll_interval_seconds?: number | null;
  collection_strategies?: { id: string; enabled: boolean }[];
  opencode_workspace_id?: string;
  claude_organization_id?: string | null;
  billing_type?: 'subscription' | 'pay_as_you_go' | 'unknown';
}

// Multi-account PUT. accountId is required in the URL.
export const putProviderConfig = (providerId: string, accountId: string, body: ProviderConfigUpdate) =>
  api<{ status: string; provider_id?: string; account_id?: string }>(
    `/api/v1/system/provider-config/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}`,
    {
      method: 'PUT',
      body: JSON.stringify(body),
    },
  );

export const patchCredentialSources = (
  providerId: string,
  accountId: string,
  sources: { source_id: string; enabled: boolean; priority: number }[],
  allMachines = false,
) => {
  const endpoint = `/api/v1/system/provider-config/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}/credential-sources`;
  return api<{ status: string }>(endpoint, {
    method: 'PATCH',
    body: JSON.stringify({ sources, all_machines: allMachines }),
  });
};

// --- Account merge / un-merge ---------------------------------------------

export const previewAccountUnmerge = (providerId: string, accountId: string) =>
  api<UnmergePlan>('/api/v1/system/provider-account-unmerge/preview', {
    method: 'POST',
    body: JSON.stringify({ provider_id: providerId, source_account_id: accountId }),
  });

export const unmergeProviderAccount = (
  providerId: string,
  accountId: string,
  confirmCollisions = false,
  previewedCollisions = 0,
) =>
  api<UnmergePlan & { status: string }>('/api/v1/system/provider-account-unmerge/apply', {
    method: 'POST',
    body: JSON.stringify({
      provider_id: providerId,
      source_account_id: accountId,
      confirm_collisions: confirmCollisions,
      // The count the preview showed: the server refuses when the rows in the
      // way changed since, because the confirmation was for other rows.
      previewed_collisions: previewedCollisions,
    }),
  });

export const deleteProviderConfig = (providerId: string, accountId: string, permanent = false) =>
  api<{ status: string }>(
    `/api/v1/system/provider-config/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}${permanent ? '?permanent=true' : ''}`,
    { method: 'DELETE' },
  );

export const putProviderConfigForAccount = (
  providerId: string,
  accountId: string,
  body: ProviderConfigUpdate,
) =>
  api<{ status: string }>(
    `/api/v1/system/provider-config/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}`,
    { method: 'PUT', body: JSON.stringify(body) },
  );

export interface ArchivedProvider {
  provider_id: string;
  account_id: string;
  lifetime: {
    tokens_input: number;
    tokens_output: number;
    tokens_cache_read: number;
    tokens_cache_create: number;
    tokens_reasoning: number;
    msgs: number;
    cost_usd: number;
    by_model: Record<string, { tokens_input: number; tokens_output: number; msgs: number; cost_usd: number }>;
  } | null;
  last_activity_ts: string | null;
}

export const fetchArchivedProviders = () =>
  api<{ archived: ArchivedProvider[] }>('/api/v1/usage/archived-providers');

// Wizard (#287) preview endpoint — debounced from step 2. Returns 409
// (with structured body in `detail`) when the previewed identity already
// exists for this provider. The optional `signal` is forwarded to `fetch`
// so callers (the wizard's debounce effect) can actually cancel an
// in-flight request — without it, abort() is decorative and the request
// races the next debounce.
export const discoverConsoleOrganizations = (sessionCookie: string, signal?: AbortSignal) =>
  api<{ organizations: { id: string; name: string }[] }>('/api/v1/system/provider-config/console-organizations', {
    method: 'POST', body: JSON.stringify({ session_cookie: sessionCookie }), signal,
  });

export const previewAccount = (body: AccountPreviewRequest, signal?: AbortSignal) =>
  api<AccountPreviewResponse>('/api/v1/system/provider-config/preview-account', {
    method: 'POST',
    body: JSON.stringify(body),
    signal,
  });

export const dismissCredentialFileMigrationNotice = () =>
  api<{ status: string }>('/api/v1/system/credentials/migration-notice/dismiss', { method: 'POST' });

export const fetchCredentialInventory = () =>
  api<CredentialInventory>('/api/v1/system/credentials');

const credentialSourcePath = (provider: string, accountId: string, sourceId: string) =>
  `/api/v1/system/credentials/${encodeURIComponent(provider)}/${encodeURIComponent(accountId)}/${encodeURIComponent(sourceId)}`;

export const postCredentialSourceRefresh = (
  provider: string,
  accountId: string,
  sourceId: string,
) =>
  api<{ status: string }>(`${credentialSourcePath(provider, accountId, sourceId)}/refresh`, {
    method: 'POST',
  });

export const deleteCredentialSource = (provider: string, accountId: string, sourceId: string) =>
  api<{ ok: boolean }>(credentialSourcePath(provider, accountId, sourceId), {
    method: 'DELETE',
  });

export interface RemoveCredentialSourcesResponse {
  removed: string[];
  skipped: { source_id: string; reason: 'managed_elsewhere' | 'not_found' }[];
}

export const removeCredentialSources = (
  provider: string,
  accountId: string,
  sourceIds: string[],
) =>
  api<RemoveCredentialSourcesResponse>(
    `/api/v1/system/credentials/${encodeURIComponent(provider)}/${encodeURIComponent(accountId)}/remove`,
    { method: 'POST', body: JSON.stringify({ source_ids: sourceIds }) },
  );

export const probeCredentialSources = (providerId: string, accountId: string) =>
  api<SourceProbeResponse>(
    `/api/v1/system/debug/sources/${encodeURIComponent(providerId)}${qs({ account_id: accountId })}`,
    { method: 'POST' },
  );

export const probeClaudeCloud = (body: ClaudeCloudProbeRequest) =>
  api<ClaudeCloudProbeReport>('/api/v1/system/debug/claude-cloud', {
    method: 'POST', body: JSON.stringify(body),
  });

export const fetchDebugRaw = (providerId: string, accountId?: string) =>
  api<DebugRawResponse>(
    `/api/v1/system/debug/raw/${encodeURIComponent(providerId)}${qs({ account_id: accountId })}`,
  );
