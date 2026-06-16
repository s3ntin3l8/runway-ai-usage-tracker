import { useQuery } from '@tanstack/react-query';
import {
  fetchGlobalStats,
  fetchHistoryChart,
  fetchTopModels,
  fetchTopProjects,
  fetchTopTools,
} from '@/api/endpoints';

export type TopMetric = 'tokens' | 'cost';
export type ProjectMetric = 'tokens' | 'cost' | 'sessions';
export type OverallMetric = 'tokens' | 'cost';

// Cross-provider "overall" time-series: tokens/cost bars summed across every
// provider/account, one stacked segment per provider (group=provider). Unlike
// useHistoryChart this has no account filter and no enabled guard.
export const useOverallChart = (days: number, metric: OverallMetric) =>
  useQuery({
    queryKey: ['usage', 'overall-chart', days, metric],
    queryFn: () => fetchHistoryChart({ days, metric, group: 'provider' }),
    refetchInterval: 120_000,
  });

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

// Top Projects ranking. `providerId` undefined → cross-provider (Insights);
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
