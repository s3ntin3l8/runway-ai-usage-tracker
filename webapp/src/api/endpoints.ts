// Typed endpoint functions over the Runway API (port of frontend/js/api.js,
// extended with the endpoints the v1 UI called inline).

import { api, qs } from './client';
import type {
  AccountPreviewRequest,
  AccountPreviewResponse,
  AnomaliesResponse,
  AppConfig,
  AuditEntry,
  CollectorStatus,
  CostForecastResponse,
  CredentialTagRequest,
  CumulativeResponse,
  DashboardLayout,
  EventRangeResponse,
  EventsResponse,
  FleetResponse,
  ForecastResponse,
  GlobalStatsResponse,
  HeatmapResponse,
  HistoryChartResponse,
  HistoryDeltas,
  HistoryWindow,
  HistoryWindowRow,
  LimitCard,
  ProviderConfig,
  SessionEntry,
  SessionsPaginatedResponse,
  Sidecar,
  SidecarChannel,
  SidecarDownloads,
  SystemSettings,
  TokenHealthEntry,
  TopModelsResponse,
  TopProjectsResponse,
  TopToolsResponse,
  UntaggedCredentialsList,
  UpdateCheckResult,
  Webhook,
  DebugRawResponse,
  WindowDetailResponse,
} from './types';

type Params = Record<string, string | number | boolean | null | undefined>;

// --- Usage -----------------------------------------------------------------

export const fetchLimits = () => api<{ limits: LimitCard[] }>('/api/v1/usage/limits');

export const fetchFleetUsage = () => api<FleetResponse>('/api/v1/usage/fleet');

export const fetchCumulative = (params: Params = {}) =>
  api<CumulativeResponse>(`/api/v1/usage/cumulative${qs(params)}`);

export const fetchForecast = (params: Params = {}) =>
  api<ForecastResponse>(`/api/v1/usage/forecast${qs(params)}`);

export const fetchCostForecast = (params: Params = {}) =>
  api<CostForecastResponse>(`/api/v1/usage/cost-forecast${qs(params)}`);

export const fetchHeatmap = (params: Params) =>
  api<HeatmapResponse>(`/api/v1/usage/heatmap${qs(params)}`);

export const fetchSessions = (params: Params) =>
  api<{ sessions: SessionEntry[] }>(`/api/v1/usage/sessions${qs(params)}`);

export const fetchSessionsPaginated = (params: Params) =>
  api<SessionsPaginatedResponse>(`/api/v1/usage/sessions/paginated${qs(params)}`);

export const fetchTopProjects = (params: Params = {}) =>
  api<TopProjectsResponse>(`/api/v1/usage/top-projects${qs(params)}`);

export const fetchTopTools = (params: Params = {}) =>
  api<TopToolsResponse>(`/api/v1/usage/top-tools${qs(params)}`);

export const fetchProjects = (params: Params = {}) =>
  api<{ projects: string[] }>(`/api/v1/usage/projects${qs(params)}`);

export const fetchEvents = (params: Params) =>
  api<EventsResponse>(`/api/v1/usage/events${qs(params)}`);

export const fetchEventRange = (params: Params) =>
  api<EventRangeResponse>(`/api/v1/usage/events/range${qs(params)}`);

export const fetchAnomalies = (params: Params = {}) =>
  api<AnomaliesResponse>(`/api/v1/usage/anomalies${qs(params)}`);

export const fetchHistoryChart = (params: Params) =>
  api<HistoryChartResponse>(`/api/v1/usage/history/chart${qs(params)}`);

export const fetchTopModels = (params: Params = {}) =>
  api<TopModelsResponse>(`/api/v1/usage/top-models${qs(params)}`);

export const fetchGlobalStats = () => api<GlobalStatsResponse>('/api/v1/usage/global-stats');

export const fetchHistoryWindows = (params: Params = {}) =>
  api<{ windows: HistoryWindowRow[] }>(`/api/v1/usage/history/windows${qs(params)}`);

export const fetchHistorySnapshots = (params: Params = {}) =>
  api<Record<string, unknown>>(`/api/v1/usage/history/snapshots${qs(params)}`);

export const fetchHistoryWindowDetail = (params: Params) =>
  api<WindowDetailResponse>(`/api/v1/usage/history/window-detail${qs(params)}`);

export const fetchHistoryDeltas = (params: Params = {}) =>
  api<HistoryDeltas>(`/api/v1/usage/history/deltas${qs(params)}`);

export const fetchWindowHistory = (params: Params) =>
  api<{ windows: HistoryWindow[] }>(`/api/v1/usage/window-history${qs(params)}`);

export const collectProvider = (providerId: string, accountId?: string) =>
  api<{ status: string; provider: string; cards: number }>(
    `/api/v1/usage/collect/${encodeURIComponent(providerId)}${qs({ account_id: accountId })}`,
    { method: 'POST' },
  );

export const resetProvider = (providerId: string, accountId?: string) =>
  api<{ status: string }>(
    `/api/v1/usage/reset/${encodeURIComponent(providerId)}${qs({ account_id: accountId })}`,
    { method: 'POST' },
  );

// --- Fleet (sidecars) ------------------------------------------------------

export const fetchSidecars = () => api<{ sidecars: Sidecar[] }>('/api/v1/fleet/sidecars');

// --- Fleet (silent-listener pending credential tags) ------------------

export const fetchUntaggedCredentials = (sidecarId?: string) => {
  const qs = sidecarId ? `?sidecar_id=${encodeURIComponent(sidecarId)}` : '';
  return api<UntaggedCredentialsList>(
    `/api/v1/fleet/credentials/tags/pending${qs}`,
  );
};

export const tagCredential = (body: CredentialTagRequest) =>
  api<{ status: string }>('/api/v1/fleet/credentials/tags', {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const patchSidecar = (sidecarId: string, body: { custom_name?: string; tags?: string[] }) =>
  api<Sidecar>(`/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
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

// Push a one-shot self-update to a sidecar; it installs on its next heartbeat.
export const triggerSidecarUpdate = (sidecarId: string) =>
  api<{ status: string; sidecar_id: string }>(
    `/api/v1/fleet/sidecars/${encodeURIComponent(sidecarId)}/update`,
    { method: 'POST' },
  );

// --- Auth -------------------------------------------------------------------

// Exchange the admin key for an HttpOnly session cookie. `remember` selects
// the longer cookie lifetime. Throws ApiError(403) on a bad key.
export const login = (key: string, remember: boolean) =>
  api<{ is_authenticated: boolean }>('/api/v1/auth/session', {
    method: 'POST',
    body: JSON.stringify({ key, remember }),
  });

// Clear this browser's session cookie.
export const logout = () => api<void>('/api/v1/auth/logout', { method: 'POST' });

// Rotate the server session secret — invalidates every session everywhere.
export const revokeAllSessions = () =>
  api<void>('/api/v1/auth/revoke-all', { method: 'POST' });

// --- System ----------------------------------------------------------------

export const fetchSettings = () => api<SystemSettings>('/api/v1/system/settings');

// Force an immediate GitHub release poll (server + sidecars); admin-gated.
export const checkForUpdates = () =>
  api<UpdateCheckResult>('/api/v1/system/check-updates', { method: 'POST' });

export const fetchSidecarDownloads = (channel: SidecarChannel = 'stable') =>
  api<SidecarDownloads>(`/api/v1/system/sidecar-downloads${qs({ channel })}`);

export const fetchStatus = () => api<CollectorStatus>('/api/v1/system/status');

export const fetchAppConfig = () => api<AppConfig>('/api/v1/system/app-config');

export const putAppConfig = (body: Partial<AppConfig>) =>
  api<{ status: string }>('/api/v1/system/app-config', {
    method: 'PUT',
    body: JSON.stringify(body),
  });

export const fetchProviderConfigs = () =>
  api<{ providers: ProviderConfig[] }>('/api/v1/system/provider-configs');

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
}

// Multi-account canonical PUT (#281). accountId is required in the URL —
// see #281 for the legacy shortcut that resolves to account_id="default"
// only when exactly one row exists.
export const putProviderConfig = (providerId: string, accountId: string, body: ProviderConfigUpdate) =>
  api<{ status: string }>(
    `/api/v1/system/provider-config/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}`,
    {
      method: 'PUT',
      body: JSON.stringify(body),
    },
  );

// Legacy single-account PUT — kept permanently for non-webapp callers
// (operator scripts, the sidecar helper). The backend resolves the target
// row from the existing rows for this provider:
//   - 0 rows → creates one with account_id="default"
//   - 1 row  → updates that row in place (account_id preserved)
//   - 2+ rows → 409 Conflict, the caller must disambiguate via the
//                per-account endpoint above.
// The legacy edit dialog uses this route so saving an email-keyed
// single-row install updates the existing account rather than creating a
// second `default` row.
export const putProviderConfigLegacy = (providerId: string, body: ProviderConfigUpdate) =>
  api<{ status: string }>(`/api/v1/system/provider-config/${encodeURIComponent(providerId)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });

export const deleteProviderConfig = (providerId: string, accountId: string) =>
  api<{ status: string }>(
    `/api/v1/system/provider-config/${encodeURIComponent(providerId)}/${encodeURIComponent(accountId)}`,
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
export const previewAccount = (body: AccountPreviewRequest, signal?: AbortSignal) =>
  api<AccountPreviewResponse>('/api/v1/system/provider-config/preview-account', {
    method: 'POST',
    body: JSON.stringify(body),
    signal,
  });

export const getDashboardLayout = () => api<DashboardLayout>('/api/v1/system/dashboard-layout');

export const putDashboardLayout = (layout: DashboardLayout) =>
  api<{ status: string }>('/api/v1/system/dashboard-layout', {
    method: 'PUT',
    body: JSON.stringify(layout),
  });

export const forceCollect = () =>
  api<{ ok: boolean; cards: number; sidecars_triggered: number }>('/api/v1/system/force-collect', {
    method: 'POST',
  });

// Silently wake the poller (resumes normal interval if dormant).
export async function postWake(): Promise<void> {
  try {
    await api('/api/v1/system/wake', { method: 'POST' });
  } catch {
    // background optimization — never surface
  }
}

export interface CleanupRequest {
  clear_cache?: boolean;
  prune_snapshots_days?: number | null;
  prune_cumulative_days?: number | null;
  remove_inactive_sidecars_days?: number | null;
}

export const postCleanup = (body: CleanupRequest) =>
  api<{ ok: boolean; results: Record<string, unknown> }>('/api/v1/system/cleanup', {
    method: 'POST',
    body: JSON.stringify(body),
  });

export const fetchTokenHealth = () =>
  api<{ tokens: TokenHealthEntry[] }>('/api/v1/system/token-health');

export const postTokenRefresh = (provider: string, accountId: string) =>
  api<{ status: string }>(
    `/api/v1/system/token-health/refresh/${encodeURIComponent(provider)}/${encodeURIComponent(accountId)}`,
    { method: 'POST' },
  );

export const deleteTokenHealth = (provider: string, accountId: string) =>
  api<{ ok: boolean }>(
    `/api/v1/system/token-health/${encodeURIComponent(provider)}/${encodeURIComponent(accountId)}`,
    { method: 'DELETE' },
  );

export const fetchAuditLog = (limit = 200) =>
  api<{ entries: AuditEntry[] }>(`/api/v1/system/audit-log${qs({ limit })}`);

export const fetchDebugRaw = (providerId: string) =>
  api<DebugRawResponse>(`/api/v1/system/debug/raw/${encodeURIComponent(providerId)}`);

// --- Webhooks ----------------------------------------------------------------

export const fetchWebhooks = () => api<{ webhooks: Webhook[] }>('/api/v1/system/webhooks');

export interface WebhookCreate {
  provider_id: string;
  account_id?: string | null;
  threshold_pct: number;
  url: string;
  channel: 'discord' | 'slack';
  active?: boolean;
}

export const createWebhook = (body: WebhookCreate) =>
  api<{ id: number }>('/api/v1/system/webhooks', { method: 'POST', body: JSON.stringify(body) });

export const updateWebhook = (
  id: number,
  body: Partial<Pick<Webhook, 'threshold_pct' | 'url' | 'active' | 'account_id'>>,
) =>
  api<{ status: string }>(`/api/v1/system/webhooks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });

export const deleteWebhook = (id: number) =>
  api<undefined>(`/api/v1/system/webhooks/${id}`, { method: 'DELETE' });

export const testWebhook = (id: number) =>
  api<{ status: string }>(`/api/v1/system/webhooks/${id}/test`, { method: 'POST' });

// --- GitHub OAuth ------------------------------------------------------------

export const initGitHubOAuth = () =>
  api<{
    device_code: string;
    user_code: string;
    verification_uri: string;
    expires_in: number;
    interval: number;
  }>('/api/v1/auth/github/init');

export const pollGitHubOAuth = (deviceCode: string) =>
  api<{ status: string; interval?: number }>('/api/v1/auth/github/poll', {
    method: 'POST',
    body: JSON.stringify({ device_code: deviceCode }),
  });

export const getGitHubOAuthStatus = () =>
  api<{ authenticated: boolean; account?: string; name?: string; email?: string }>(
    '/api/v1/auth/github/status',
  );

export const logoutGitHub = () =>
  api<Record<string, unknown>>('/api/v1/auth/github/logout', { method: 'POST' });
