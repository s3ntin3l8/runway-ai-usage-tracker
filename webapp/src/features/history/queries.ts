import { useQuery } from '@tanstack/react-query';
import {
  fetchGlobalStats,
  fetchHistoryChart,
  fetchHistoryDeltas,
  fetchHistoryWindowDetail,
  fetchHistoryWindows,
  fetchTopModels,
  fetchTopProjects,
  fetchTopTools,
} from '@/api/endpoints';
import type { HistoryWindowRow } from '@/api/types';

export type Metric = 'percent' | 'tokens' | 'cost';
export type TopMetric = 'tokens' | 'cost';
export type ProjectMetric = 'tokens' | 'cost' | 'sessions';

export const useTopModels = (metric: TopMetric, days: number, excludeCache: boolean) =>
  useQuery({
    queryKey: ['usage', 'top-models', metric, days, excludeCache],
    queryFn: () =>
      fetchTopModels({ metric, days, exclude_cache: excludeCache, limit: 12 }),
    refetchInterval: 120_000,
  });

export const useGlobalStats = () =>
  useQuery({
    queryKey: ['usage', 'global-stats'],
    queryFn: fetchGlobalStats,
    refetchInterval: 300_000,
  });

export interface RankWindow {
  days?: number;
  range?: { since: string; until: string };
  providerId?: string;
}

// Top Projects ranking. `providerId` undefined → cross-provider (History);
// set → scoped to one provider (the Activity card). Window is either a rolling
// `days` count or an explicit month `range`; neither → the endpoint's current
// month default.
export const useTopProjects = (metric: ProjectMetric, excludeCache: boolean, win: RankWindow) =>
  useQuery({
    queryKey: [
      'usage',
      'top-projects',
      metric,
      excludeCache,
      win.days ?? null,
      win.range?.since ?? null,
      win.range?.until ?? null,
      win.providerId ?? null,
    ],
    queryFn: () =>
      fetchTopProjects({
        metric,
        exclude_cache: excludeCache,
        limit: 12,
        ...(win.range
          ? { since: win.range.since, until: win.range.until }
          : win.days != null
            ? { days: win.days }
            : {}),
        ...(win.providerId ? { provider_id: win.providerId } : {}),
      }),
    refetchInterval: 120_000,
  });

export const useTopTools = (days: number) =>
  useQuery({
    queryKey: ['usage', 'top-tools', days],
    queryFn: () => fetchTopTools({ days, limit: 12 }),
    refetchInterval: 120_000,
  });

export const useHistoryChart = (
  providerId: string | null,
  accountId: string | null,
  days: number,
  metric: Metric,
) =>
  useQuery({
    queryKey: ['usage', 'history-chart', providerId, accountId, days, metric],
    queryFn: () =>
      fetchHistoryChart({ provider_id: providerId, account_id: accountId, days, metric }),
    enabled: !!providerId && !!accountId,
    refetchInterval: 120_000,
  });

export const useHistoryDeltas = (days: number) =>
  useQuery({
    queryKey: ['usage', 'history-deltas', days],
    queryFn: () => fetchHistoryDeltas({ days }),
    refetchInterval: 120_000,
  });

export const useHistoryWindows = (providerId: string | null, days: number) =>
  useQuery({
    queryKey: ['usage', 'history-windows', providerId, days],
    queryFn: () => fetchHistoryWindows({ provider_id: providerId, days, limit: 50 }),
    refetchInterval: 300_000,
  });

export const useWindowDetail = (row: HistoryWindowRow | null) =>
  useQuery({
    queryKey: [
      'usage',
      'window-detail',
      row?.provider_id,
      row?.account_id,
      row?.window_type,
      row?.window_start,
    ],
    queryFn: () =>
      fetchHistoryWindowDetail({
        provider_id: row!.provider_id,
        account_id: row!.account_id,
        window_type: row!.window_type,
        window_start: row!.window_start,
        window_end: row!.window_end,
      }),
    enabled: !!row && !!row.window_start && !!row.window_end,
  });
