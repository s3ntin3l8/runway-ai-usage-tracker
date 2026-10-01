import { useQuery } from '@tanstack/react-query';
import {
  fetchGlobalStats,
  fetchHistoryChart,
  fetchTopModels,
  fetchTopProjects,
  fetchTopTools,
} from '@/api/endpoints';
import { rangeQueryParams, type DateRangeValue } from '@/lib/timeRange';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { useUsageSource } from '@/hooks/useUsageSource';

export type TopMetric = 'tokens' | 'cost';
export type ProjectMetric = 'tokens' | 'cost' | 'sessions';
export type OverallMetric = 'tokens' | 'cost';

// Cross-provider "overall" time-series: tokens/cost bars summed across every
// provider/account, one stacked segment per provider (group=provider). Unlike
// useHistoryChart this has no account filter and no enabled guard.
export const useOverallChart = (range: DateRangeValue, metric: OverallMetric) => {
  const params = rangeQueryParams(range);
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'overall-chart', params, metric, sidecarId],
    queryFn: () => fetchHistoryChart({ ...params, metric, group: 'provider', sidecar_id: sidecarId }),
    refetchInterval: 120_000,
  });
};

export const useTopModels = (metric: TopMetric, range: DateRangeValue, excludeCache: boolean) => {
  const params = rangeQueryParams(range);
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'top-models', metric, params, excludeCache, sidecarId],
    queryFn: () =>
      fetchTopModels({ metric, ...params, exclude_cache: excludeCache, sidecar_id: sidecarId, limit: 12 }),
    refetchInterval: 120_000,
  });
};

export const useGlobalStats = () => {
  const { excludeCache } = useExcludeCache();
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'global-stats', sidecarId, excludeCache],
    queryFn: () => fetchGlobalStats({ sidecar_id: sidecarId, exclude_cache: excludeCache }),
    refetchInterval: 300_000,
  });
};

// Top Projects ranking. `providerId` undefined → cross-provider (Insights);
// set → scoped to one provider (the Activity card). The window comes from the
// shared range value — rolling `days` or an absolute span.
export const useTopProjects = (
  metric: ProjectMetric,
  excludeCache: boolean,
  range: DateRangeValue,
  providerId?: string,
) => {
  const params = rangeQueryParams(range);
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'top-projects', metric, excludeCache, params, providerId ?? null, sidecarId],
    queryFn: () =>
      fetchTopProjects({
        metric,
        exclude_cache: excludeCache,
        sidecar_id: sidecarId,
        limit: 12,
        ...params,
        ...(providerId ? { provider_id: providerId } : {}),
      }),
    refetchInterval: 120_000,
  });
};

export const useTopTools = (range: DateRangeValue) => {
  const params = rangeQueryParams(range);
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'top-tools', params, sidecarId],
    queryFn: () => fetchTopTools({ ...params, sidecar_id: sidecarId, limit: 12 }),
    refetchInterval: 120_000,
  });
};
