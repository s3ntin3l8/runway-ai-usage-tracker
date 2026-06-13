// Provider-detail data hooks, parametrized by (provider_id, account_id).

import { keepPreviousData, useQuery } from '@tanstack/react-query';
import {
  fetchAnomalies,
  fetchCostForecast,
  fetchCumulative,
  fetchDebugRaw,
  fetchEvents,
  fetchForecast,
  fetchHeatmap,
  fetchHistoryChart,
  fetchSessions,
  fetchWindowHistory,
} from '@/api/endpoints';
import type { Metric } from '@/features/history/queries';

export const useProviderForecast = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'forecast', providerId, accountId, 'series'],
    queryFn: () =>
      fetchForecast({ provider_id: providerId, account_id: accountId, include_series: true }),
    refetchInterval: 60_000,
  });

export const useProviderCumulative = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'cumulative', providerId, accountId],
    queryFn: () => fetchCumulative({ provider_id: providerId, account_id: accountId }),
    refetchInterval: 120_000,
  });

export const useProviderHeatmap = (providerId: string, accountId: string, tz: string) =>
  useQuery({
    queryKey: ['usage', 'heatmap', providerId, accountId, tz],
    queryFn: () => fetchHeatmap({ provider_id: providerId, account_id: accountId, days: 14, tz }),
    refetchInterval: 300_000,
  });

export const useProviderSessions = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'sessions', providerId, accountId],
    queryFn: () =>
      fetchSessions({ provider_id: providerId, account_id: accountId, limit: 10, sort_by: 'tokens' }),
    refetchInterval: 120_000,
  });

// The 3 most recent sessions (by ts_end) — drives the Overview activity pulse.
export const useProviderRecentSessions = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'sessions', 'recent', providerId, accountId],
    queryFn: () =>
      fetchSessions({ provider_id: providerId, account_id: accountId, limit: 3, sort_by: 'recent' }),
    refetchInterval: 120_000,
  });

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
    enabled,
  }: { page: number; pageSize: number; since: string; enabled: boolean },
) =>
  useQuery({
    queryKey: ['usage', 'events', providerId, accountId, since, page, pageSize],
    queryFn: () =>
      fetchEvents({
        provider_id: providerId,
        account_id: accountId,
        since,
        limit: pageSize,
        offset: page * pageSize,
      }),
    enabled,
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
  });

export const useWindowHistory = (providerId: string, accountId: string, windowType: string) =>
  useQuery({
    queryKey: ['usage', 'window-history', providerId, accountId, windowType],
    queryFn: () =>
      fetchWindowHistory({
        provider_id: providerId,
        account_id: accountId,
        window_type: windowType,
        limit: 12,
      }),
    enabled: windowType !== 'unknown' && windowType !== '',
  });

// Per-day token / cost bars for this account (drives the trend cards).
export const useProviderHistoryChart = (
  providerId: string,
  accountId: string,
  days: number,
  metric: Metric,
) =>
  useQuery({
    queryKey: ['usage', 'history-chart', providerId, accountId, days, metric],
    queryFn: () =>
      fetchHistoryChart({ provider_id: providerId, account_id: accountId, days, metric }),
    refetchInterval: 120_000,
  });

export const useProviderAnomalies = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'anomalies', providerId, accountId],
    queryFn: () => fetchAnomalies({ provider_id: providerId, account_id: accountId }),
    refetchInterval: 300_000,
  });

export const useProviderCostForecast = (providerId: string, accountId: string) =>
  useQuery({
    queryKey: ['usage', 'cost-forecast', providerId, accountId],
    queryFn: () => fetchCostForecast({ provider_id: providerId, account_id: accountId }),
    refetchInterval: 120_000,
  });

// Error events (kind="error") in the last 24h — feeds the alert banner.
export const useProviderErrors = (providerId: string, accountId: string) =>
  useQuery({
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

export const useDebugRaw = (providerId: string, enabled: boolean) =>
  useQuery({
    queryKey: ['system', 'debug-raw', providerId],
    queryFn: () => fetchDebugRaw(providerId),
    enabled,
    // 10/min rate limit + live upstream calls: fetch once per explicit ask
    staleTime: Infinity,
    retry: false,
  });
