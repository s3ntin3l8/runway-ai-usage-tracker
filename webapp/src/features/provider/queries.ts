// Provider-detail data hooks, parametrized by (provider_id, account_id).

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  fetchAnomalies,
  fetchCostForecast,
  fetchCumulative,
  fetchDebugRaw,
  fetchEventRange,
  fetchEvents,
  fetchForecast,
  fetchHeatmap,
  fetchHistoryChart,
  fetchProjects,
  fetchSessions,
  fetchSessionsPaginated,
  fetchWindowHistory,
} from '@/api/endpoints';
import type { Metric } from '@/features/history/queries';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { useUsageSource } from '@/hooks/useUsageSource';

// A closed [since, until) instant range (ISO strings) used to scope a tab to a
// selected past month. `undefined` means "use the endpoint's rolling default".
export interface DateRange {
  since: string;
  until: string;
}

export const useProviderForecast = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'forecast', providerId, accountId, 'series'],
    queryFn: () =>
      fetchForecast({ provider_id: providerId, account_id: accountId, include_series: true }),
    refetchInterval: 60_000,
  });

export const useProviderCumulative = (providerId: string, accountId: string) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'cumulative', providerId, accountId, sidecarId],
    queryFn: () => fetchCumulative({ provider_id: providerId, account_id: accountId, sidecar_id: sidecarId }),
    refetchInterval: 120_000,
  });
};

// tz-correct cumulative totals for a specific past month ('YYYY-MM'). The
// server aggregates this bucket live from usage_events on the user's local
// calendar (see the /cumulative month-live path), so it matches the live
// current-month gauge. Read the bucket at `month_${periodKey}`.
export const useProviderCumulativeMonth = (
  providerId: string,
  accountId: string,
  periodKey: string,
  enabled = true,
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'cumulative', providerId, accountId, 'month', periodKey, sidecarId],
    queryFn: () =>
      fetchCumulative({
        provider_id: providerId,
        account_id: accountId,
        period_type: 'month',
        period_key: periodKey,
        sidecar_id: sidecarId,
      }),
    enabled,
    refetchInterval: 120_000,
  });
};

// Cumulative totals aggregated live over an arbitrary [since, until) window —
// the rolling-range counterpart to the month bucket above. The server runs
// `query_cumulative_live` and points `current_month_key` at the range bucket,
// so the read is identical to the month/live responses.
export const useProviderCumulativeRange = (
  providerId: string,
  accountId: string,
  range: DateRange,
  enabled = true,
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'cumulative', providerId, accountId, 'range', range.since, range.until, sidecarId],
    queryFn: () =>
      fetchCumulative({
        provider_id: providerId,
        account_id: accountId,
        since: range.since,
        until: range.until,
        sidecar_id: sidecarId,
      }),
    enabled,
    refetchInterval: 120_000,
  });
};

// Earliest/latest event timestamps — bounds the month selector's reach.
export const useProviderEventRange = (providerId: string, accountId: string) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'events', 'range', providerId, accountId, sidecarId],
    queryFn: () => fetchEventRange({ provider_id: providerId, account_id: accountId, sidecar_id: sidecarId }),
    staleTime: 300_000,
  });
};

export const useProviderHeatmap = (
  providerId: string,
  accountId: string,
  tz: string,
  range?: DateRange,
) => {
  const [sidecarId] = useUsageSource();
  const { excludeCache } = useExcludeCache();
  return useQuery({
    queryKey: ['usage', 'heatmap', providerId, accountId, tz, range?.since, range?.until, sidecarId, excludeCache],
    queryFn: () =>
      fetchHeatmap(
        range
          ? { provider_id: providerId, account_id: accountId, since: range.since, until: range.until, tz, sidecar_id: sidecarId, exclude_cache: excludeCache }
          : { provider_id: providerId, account_id: accountId, days: 14, tz, sidecar_id: sidecarId, exclude_cache: excludeCache },
      ),
    refetchInterval: 300_000,
  });
};

export const useProviderSessions = (
  providerId: string,
  accountId: string,
  range?: DateRange,
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'sessions', providerId, accountId, range?.since, range?.until, sidecarId],
    queryFn: () =>
      fetchSessions({
        provider_id: providerId,
        account_id: accountId,
        limit: 10,
        sort_by: 'tokens',
        sidecar_id: sidecarId,
        ...(range ? { since: range.since, until: range.until } : {}),
      }),
    refetchInterval: 120_000,
  });
};

// The 3 most recent sessions (by ts_end) — drives the Overview activity pulse.
export const useProviderRecentSessions = (
  providerId: string,
  accountId: string,
  range?: DateRange,
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: [
      'usage',
      'sessions',
      'recent',
      providerId,
      accountId,
      range?.since ?? null,
      range?.until ?? null,
      sidecarId ?? null,
    ],
    queryFn: () =>
      fetchSessions({
        provider_id: providerId,
        account_id: accountId,
        limit: 3,
        sort_by: 'recent',
        sidecar_id: sidecarId,
        ...(range ? { since: range.since, until: range.until } : {}),
      }),
    refetchInterval: 120_000,
  });
};

// Paginated event tail for the Events tab. `since` scopes the window (e.g. the
// start of the current month); offset/limit page through it. keepPreviousData
// holds the prior page on screen while the next loads, so paging doesn't flicker.
export const useProviderEventsPage = (
  providerId: string,
  accountId: string,
  {
    page,
    pageSize,
    since,
    until,
    enabled,
  }: { page: number; pageSize: number; since: string; until?: string; enabled: boolean },
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'events', providerId, accountId, since, until, sidecarId, page, pageSize],
    queryFn: () =>
      fetchEvents({
        provider_id: providerId,
        account_id: accountId,
        since,
        ...(until ? { until } : {}),
        sidecar_id: sidecarId,
        limit: pageSize,
        offset: page * pageSize,
      }),
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });
};

// One page of sessions for the Sessions browser tab, scoped to the selected
// month and (optionally) one project. keepPreviousData avoids paging flicker.
export const useSessionsPaginated = (
  providerId: string,
  accountId: string,
  {
    page,
    pageSize,
    since,
    until,
    project,
    sortBy = 'recent',
    sortDir = 'desc',
    enabled,
  }: {
    page: number;
    pageSize: number;
    since: string;
    until?: string;
    project?: string | null;
    sortBy?: string;
    sortDir?: string;
    enabled: boolean;
  },
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: [
      'usage',
      'sessions-page',
      providerId,
      accountId,
      since,
      until,
      project,
      sortBy,
      sortDir,
      page,
      pageSize,
      sidecarId,
    ],
    queryFn: () =>
      fetchSessionsPaginated({
        provider_id: providerId,
        account_id: accountId,
        since,
        ...(until ? { until } : {}),
        ...(project ? { project } : {}),
        page,
        limit: pageSize,
        sort_by: sortBy,
        sort_dir: sortDir,
        sidecar_id: sidecarId,
      }),
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: 120_000,
  });
};

// Distinct project labels for this provider in the selected window — feeds the
// Sessions tab filter dropdown.
export const useProjects = (providerId: string, range?: DateRange) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'projects', providerId, range?.since, range?.until, sidecarId],
    queryFn: () =>
      fetchProjects({
        provider_id: providerId,
        sidecar_id: sidecarId,
        ...(range ? { since: range.since, until: range.until } : {}),
      }),
    staleTime: 120_000,
  });
};

// Series identity is required: pass '' / 'default' to match unscoped rows with
// the default variant, including legacy rows retained by the history query.
export const useWindowHistory = (
  providerId: string,
  accountId: string,
  windowType: string,
  seriesModelId: string,
  seriesVariant: string,
) =>
  useQuery({
    queryKey: [
      'usage',
      'window-history',
      providerId,
      accountId,
      windowType,
      seriesModelId,
      seriesVariant,
    ],
    queryFn: () =>
      fetchWindowHistory({
        provider_id: providerId,
        account_id: accountId,
        window_type: windowType,
        series_model_id: seriesModelId,
        series_variant: seriesVariant,
        limit: 12,
      }),
    enabled: windowType !== 'unknown' && windowType !== '',
  });

// Per-day token / cost bars for this account (drives the trend cards). A
// `range` scopes the bars to a closed period (a selected past month) and the
// server renders them as daily bars; otherwise the rolling `days` window is used.
export const useProviderHistoryChart = (
  providerId: string,
  accountId: string,
  days: number,
  metric: Metric,
  range?: DateRange,
) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'history-chart', providerId, accountId, days, metric, range?.since, range?.until, sidecarId],
    queryFn: () =>
      fetchHistoryChart({
        provider_id: providerId,
        account_id: accountId,
        days,
        metric,
        sidecar_id: sidecarId,
        ...(range ? { since: range.since, until: range.until } : {}),
      }),
    refetchInterval: 120_000,
  });
};

export const useProviderAnomalies = (providerId: string, accountId: string) => {
  const [sidecarId] = useUsageSource();
  return useQuery({
    queryKey: ['usage', 'anomalies', providerId, accountId, sidecarId],
    queryFn: () =>
      fetchAnomalies({
        provider_id: providerId,
        account_id: accountId,
        sidecar_id: sidecarId,
      }),
    refetchInterval: 300_000,
  });
};

export const useProviderCostForecast = (providerId: string, accountId: string) => {
  const { excludeCache } = useExcludeCache();
  return useQuery({
    queryKey: ['usage', 'cost-forecast', providerId, accountId, excludeCache],
    queryFn: () => fetchCostForecast({ provider_id: providerId, account_id: accountId, exclude_cache: excludeCache }),
    refetchInterval: 120_000,
  });
};

// Error events (kind="error") in the last 24h — feeds a provider-wide alert
// banner so a source filter cannot hide errors from another source.
export const useProviderErrors = (providerId: string, accountId: string) => {
  return useQuery({
    queryKey: ['usage', 'events', 'errors', providerId, accountId],
    queryFn: () =>
      fetchEvents({
        provider_id: providerId,
        account_id: accountId,
        kind: 'error',
        since: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
        limit: 100,
      }),
    refetchInterval: 120_000,
  });
};

export const useDebugRaw = (providerId: string, accountId: string, enabled: boolean) =>
  useQuery({
    queryKey: ['system', 'debug-raw', providerId, accountId],
    queryFn: () => fetchDebugRaw(providerId, accountId),
    enabled,
    // 10/min rate limit + live upstream calls: fetch once per explicit ask
    staleTime: Infinity,
    retry: false,
  });
