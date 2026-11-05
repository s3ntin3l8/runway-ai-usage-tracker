import { api, qs } from '../client';
import type {
  CredentialTagList,
  CredentialTagRequest,
  FleetSourceAction,
  FleetSourceResponse,
  PendingUsageAssignmentGroup,
  PendingUsageAssignResult,
  PendingUsageEvent,
  PendingUsageFilter,
  PendingUsageMapping,
  PendingUsageSessionsResponse,
  Sidecar,
  SidecarAdoptSummary,
  SidecarChannel,
  SidecarLogLevel,
  UntaggedCredentialsList,
} from '../types';

// --- Fleet (sidecars) ------------------------------------------------------

export const fetchSidecars = () => api<{ sidecars: Sidecar[] }>('/api/v1/fleet/sidecars');

// --- Fleet (silent-listener pending credential tags) ------------------

export const fetchUntaggedCredentials = (sidecarId?: string) => {
  const qs = sidecarId ? `?sidecar_id=${encodeURIComponent(sidecarId)}` : '';
  return api<UntaggedCredentialsList>(
    `/api/v1/fleet/credentials/tags/pending${qs}`,
  );
};

export const fetchPendingUsageEvents = (offset = 0) =>
  api<{ items: PendingUsageEvent[]; total: number; offset: number; limit: number }>(
    `/api/v1/fleet/events/pending${qs({ offset, limit: 100 })}`,
  );

export interface FetchPendingUsageSessionsOptions {
  offset?: number;
  filters?: PendingUsageFilter;
  limit?: number;
}

export const fetchPendingUsageSessions = (
  { offset = 0, filters = {}, limit = 100 }: FetchPendingUsageSessionsOptions = {},
) =>
  api<PendingUsageSessionsResponse>(
    `/api/v1/fleet/events/pending/sessions${qs({ offset, limit, ...filters })}`,
  );

export const assignPendingUsageEvents = (
  eventIds: number[],
  accountId: string,
  targetProviderId?: string,
) =>
  api<{
    assigned: number;
    provider_id: string;
    target_provider_id?: string;
    /** One entry per provider group the selected events were split into. */
    mappings: PendingUsageAssignResult[];
  }>(
    '/api/v1/fleet/events/pending/assign',
    {
      method: 'POST',
      body: JSON.stringify({
        event_ids: eventIds,
        account_id: accountId,
        ...(targetProviderId ? { target_provider_id: targetProviderId } : {}),
      }),
    },
  );

export const assignPendingUsageEventsBatch = (assignments: PendingUsageAssignmentGroup[]) =>
  api<{
    assigned: number;
    providers: string[];
    mappings: PendingUsageMapping[];
  }>('/api/v1/fleet/events/pending/assign-batch', {
    method: 'POST',
    body: JSON.stringify({ assignments }),
  });

export const tagCredential = (body: CredentialTagRequest) =>
  api<{ status: string; target_archived?: boolean }>('/api/v1/fleet/credentials/tags', {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const fetchCredentialTags = () =>
  api<CredentialTagList>('/api/v1/fleet/credentials/tags');

export const deleteCredentialTag = (tag: {
  provider_id: string;
  credential_origin: string;
  sidecar_id: string | null;
}) => {
  const qs = new URLSearchParams({
    provider_id: tag.provider_id,
    credential_origin: tag.credential_origin,
  });
  if (tag.sidecar_id) qs.set('sidecar_id', tag.sidecar_id);
  return api<{ status: string }>(`/api/v1/fleet/credentials/tags?${qs.toString()}`, {
    method: 'DELETE',
  });
};

export const patchSidecar = (sidecarId: string, body: { custom_name?: string; tags?: string[] }) =>
  api<Sidecar>(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });

// Fold a legacy sidecar's history and settings into the enrolled device that replaced it.
// `dryRun` previews the counts and writes nothing.
export const adoptLegacySidecar = (sidecarId: string, legacySidecarId: string, dryRun: boolean) =>
  api<SidecarAdoptSummary>(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/adopt`, {
    method: 'POST',
    body: JSON.stringify({ legacy_sidecar_id: legacySidecarId, dry_run: dryRun }),
  });

export const deleteSidecar = (sidecarId: string) =>
  api<{ status: string }>(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}`, {
    method: 'DELETE',
  });

export const setSidecarEnabled = (sidecarId: string, enabled: boolean) =>
  api<{ status: string }>(
    `/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/${enabled ? 'resume' : 'pause'}`,
    { method: 'POST' },
  );

// Force a sidecar's keep-alive (login renewal) on or off; null defers to its own flag / the fleet
// default. With a `provider` it overrides just that login on top of the sidecar-level setting (null
// clears that login's override). Delivered on the sidecar's next check-in.
export const setSidecarKeepAlive = (sidecarId: string, enabled: boolean | null, provider?: string) =>
  api<{
    status: string;
    keep_alive_desired: boolean | null;
    keep_alive_desired_providers?: Record<string, boolean>;
  }>(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/keep-alive`, {
    method: 'PUT',
    body: JSON.stringify(provider ? { enabled, provider } : { enabled }),
  });

// Override the fleet's auto-update flag and/or update channel for one sidecar, or its log level. A key that is left
// out stays as it is; null clears that override so the sidecar follows the fleet again. Delivered
// on the sidecar's next check-in.
export const setSidecarSettings = (
  sidecarId: string,
  settings: {
    auto_update?: boolean | null;
    update_channel?: SidecarChannel | null;
    log_level?: SidecarLogLevel | null;
    claude_config_dirs?: string[] | null;
    codex_home?: string[] | null;
  },
) =>
  api<{
    status: string;
    auto_update_desired: boolean | null;
    update_channel_desired: SidecarChannel | null;
    log_level_desired: SidecarLogLevel | null;
    login_dirs_desired?: { claude_config_dirs: string[]; codex_home: string[] };
    effective_auto_update: boolean;
    effective_update_channel: SidecarChannel;
  }>(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/settings`, {
    method: 'PUT',
    body: JSON.stringify(settings),
  });

// Push a one-shot self-update to a sidecar; it installs on its next heartbeat.
export const triggerSidecarUpdate = (sidecarId: string) =>
  api<{ status: string; sidecar_id: string }>(
    `/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/update`,
    { method: 'POST' },
  );

export interface ClaudeSourceOrganizations {
  organizations: { id: string; name: string }[];
  complete: boolean;
  selection_required: boolean;
}
export const fetchClaudeSourceOrganizations = (providerId: string, sourceId: string) =>
  api<ClaudeSourceOrganizations>(`/api/v1/fleet/credentials/${encodeURIComponent(sourceId)}/claude-organizations?provider_id=${encodeURIComponent(providerId)}`);
export const selectClaudeSourceOrganization = (providerId: string, sourceId: string, organizationId: string) =>
  api<{ status: 'ok' }>(`/api/v1/fleet/credentials/${encodeURIComponent(sourceId)}/claude-organizations`, {
    method: 'POST', body: JSON.stringify({ provider_id: providerId, organization_id: organizationId }),
  });


export const fetchSidecarSources = (sidecarId: string) =>
  api<FleetSourceResponse>('/api/v1/fleet/sidecars/' + encodeURIComponent(sidecarId) + '/sources');

export const commandSidecarSource = (sidecarId: string, sourceId: string, action: FleetSourceAction) =>
  api<{ id: number; status: 'pending' }>('/api/v1/fleet/sidecars/' + encodeURIComponent(sidecarId) + '/sources/' + encodeURIComponent(sourceId) + '/commands', { method: 'POST', body: JSON.stringify({ action }) });
