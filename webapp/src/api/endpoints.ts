// Typed endpoint functions over the Runway API (port of frontend/js/api.js,
// extended with the endpoints the v1 UI called inline).

import { api, qs } from './client';
import type {
  AnomaliesResponse,
  AppConfig,
  AuditEntry,
  CollectorStatus,
  CostForecastResponse,
  CumulativeResponse,
  DashboardLayout,
  FleetResponse,
  ForecastResponse,
  HeatmapResponse,
  HistoryChartResponse,
  HistoryDeltas,
  HistoryWindow,
  HistoryWindowRow,
  LimitCard,
  ProviderConfig,
  SessionEntry,
  Sidecar,
  SystemSettings,
  TokenHealthEntry,
  UsageEvent,
  Webhook,
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

export const fetchEvents = (params: Params) =>
  api<{ events: UsageEvent[]; total: number; limit: number }>(
    `/api/v1/usage/events${qs(params)}`,
  );

export const fetchAnomalies = (params: Params = {}) =>
  api<AnomaliesResponse>(`/api/v1/usage/anomalies${qs(params)}`);

export const fetchHistoryChart = (params: Params) =>
  api<HistoryChartResponse>(`/api/v1/usage/history/chart${qs(params)}`);

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

// --- System ----------------------------------------------------------------

export const fetchSettings = () => api<SystemSettings>('/api/v1/system/settings');

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
  api_key?: string;
  session_cookie?: string;
  account_label?: string;
  poll_interval_seconds?: number | null;
  collection_strategies?: { id: string; enabled: boolean }[];
}

export const putProviderConfig = (providerId: string, body: ProviderConfigUpdate) =>
  api<{ status: string }>(`/api/v1/system/provider-config/${encodeURIComponent(providerId)}`, {
    method: 'PUT',
    body: JSON.stringify(body),
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
  api<Record<string, unknown>>(`/api/v1/system/debug/raw/${encodeURIComponent(providerId)}`);

// --- Webhooks ----------------------------------------------------------------

export const fetchWebhooks = () => api<{ webhooks: Webhook[] }>('/api/v1/system/webhooks');

export interface WebhookCreate {
  provider_id: string;
  threshold_pct: number;
  url: string;
  channel: 'discord' | 'slack';
  active?: boolean;
}

export const createWebhook = (body: WebhookCreate) =>
  api<{ id: number }>('/api/v1/system/webhooks', { method: 'POST', body: JSON.stringify(body) });

export const updateWebhook = (
  id: number,
  body: Partial<Pick<Webhook, 'threshold_pct' | 'url' | 'active'>>,
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

export const initGitHubOAuth = () => api<Record<string, unknown>>('/api/v1/auth/github/init');

export const pollGitHubOAuth = (deviceCode: string) =>
  api<Record<string, unknown>>('/api/v1/auth/github/poll', {
    method: 'POST',
    body: JSON.stringify({ device_code: deviceCode }),
  });

export const getGitHubOAuthStatus = () =>
  api<{ authenticated: boolean }>('/api/v1/auth/github/status');

export const logoutGitHub = () =>
  api<Record<string, unknown>>('/api/v1/auth/github/logout', { method: 'POST' });
