import { api, qs } from '../client';
import type {
  AppConfig,
  AuditEntry,
  CollectorStatus,
  DashboardLayout,
  DataHealthFixPlan,
  DataHealthJob,
  DataHealthReport,
  PairingCode,
  SidecarChannel,
  SidecarDownloads,
  SystemSettings,
  UpdateCheckResult,
  Webhook,
} from '../types';

// --- System ----------------------------------------------------------------

export const fetchSettings = () => api<SystemSettings>('/api/v1/system/settings');

// Force an immediate GitHub release poll (server + sidecars); admin-gated.
export const checkForUpdates = () =>
  api<UpdateCheckResult>('/api/v1/system/check-updates', { method: 'POST' });

export const createPairingCode = (serverUrl: string) =>
  api<PairingCode>('/api/v1/fleet/pairing-codes', {
    method: 'POST',
    body: JSON.stringify({ server_url: serverUrl }),
  });

export const fetchSidecarDownloads = (channel: SidecarChannel = 'stable') =>
  api<SidecarDownloads>(`/api/v1/system/sidecar-downloads${qs({ channel })}`);

export const fetchStatus = () => api<CollectorStatus>('/api/v1/system/status');

export const fetchAppConfig = () => api<AppConfig>('/api/v1/system/app-config');

export const putAppConfig = (body: Partial<AppConfig>) =>
  api<{ status: string }>('/api/v1/system/app-config', {
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

export const fetchAuditLog = (limit = 200) =>
  api<{ entries: AuditEntry[] }>(`/api/v1/system/audit-log${qs({ limit })}`);

// --- Webhooks ----------------------------------------------------------------

export const fetchWebhooks = () => api<{ webhooks: Webhook[] }>('/api/v1/system/webhooks');

export interface WebhookCreate {
  provider_id: string;
  account_id?: string | null;
  threshold_pct: number;
  url: string;
  channel: 'discord' | 'slack';
  active?: boolean;
  credential_alerts?: boolean;
}

export const createWebhook = (body: WebhookCreate) =>
  api<{ id: number }>('/api/v1/system/webhooks', { method: 'POST', body: JSON.stringify(body) });

export const updateWebhook = (
  id: number,
  body: Partial<
    Pick<Webhook, 'threshold_pct' | 'url' | 'active' | 'account_id' | 'credential_alerts'>
  >,
) =>
  api<{ status: string }>(`/api/v1/system/webhooks/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });

export const deleteWebhook = (id: number) =>
  api<undefined>(`/api/v1/system/webhooks/${id}`, { method: 'DELETE' });

export const testWebhook = (id: number) =>
  api<{ status: string }>(`/api/v1/system/webhooks/${id}/test`, { method: 'POST' });

// --- Data Health ---------------------------------------------------------------

export const fetchDataHealthReport = () =>
  api<DataHealthReport>('/api/v1/system/data-health/');

export const rescanDataHealth = () =>
  api<{ started: boolean }>('/api/v1/system/data-health/rescan', { method: 'POST' });

export const previewDataHealthFix = (
  checkId: string,
  groupKey: string,
  params: Record<string, unknown>,
) =>
  api<DataHealthFixPlan>(`/api/v1/system/data-health/${encodeURIComponent(checkId)}/preview`, {
    method: 'POST',
    body: JSON.stringify({ group_key: groupKey, params }),
  });

export const applyDataHealthFix = (
  checkId: string,
  groupKey: string,
  params: Record<string, unknown>,
) =>
  api<{ job_id: string }>(`/api/v1/system/data-health/${encodeURIComponent(checkId)}/apply`, {
    method: 'POST',
    body: JSON.stringify({ group_key: groupKey, params, confirm: true }),
  });

export const fetchDataHealthJob = (jobId: string) =>
  api<DataHealthJob>(`/api/v1/system/data-health/jobs/${encodeURIComponent(jobId)}`);
