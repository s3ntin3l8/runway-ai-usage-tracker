import { api, qs } from '../client';
import type {
  AnomaliesResponse,
  CostForecastResponse,
  CumulativeResponse,
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
  SessionEntry,
  SessionsPaginatedResponse,
  SourceAppsResponse,
  TopHostsResponse,
  TopModelsResponse,
  TopProjectsResponse,
  TopSessionsResponse,
  TopToolsResponse,
  UsageSourcesResponse,
  WindowDetailResponse,
} from '../types';

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

export const fetchTopSessions = (params: Params = {}) =>
  api<TopSessionsResponse>(`/api/v1/usage/top-sessions${qs(params)}`);

export const fetchTopHosts = (params: Params = {}) =>
  api<TopHostsResponse>(`/api/v1/usage/top-hosts${qs(params)}`);

export const fetchTopTools = (params: Params = {}) =>
  api<TopToolsResponse>(`/api/v1/usage/top-tools${qs(params)}`);

export const fetchProjects = (params: Params = {}) =>
  api<{ projects: string[] }>(`/api/v1/usage/projects${qs(params)}`);

export const fetchSessionModels = (params: Params) =>
  api<{ models: string[] }>(`/api/v1/usage/session-models${qs(params)}`);

export const fetchSessionBranches = (params: Params) =>
  api<{ branches: string[] }>(`/api/v1/usage/session-branches${qs(params)}`);

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

export const fetchSourceApps = (params: Params = {}) =>
  api<SourceAppsResponse>(`/api/v1/usage/source-apps${qs(params)}`);

export const fetchGlobalStats = (params: Params = {}) =>
  api<GlobalStatsResponse>(`/api/v1/usage/global-stats${qs(params)}`);

export const fetchUsageSources = () => api<UsageSourcesResponse>('/api/v1/usage/sources');

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
