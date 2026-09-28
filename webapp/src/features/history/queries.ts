import { useQuery } from '@tanstack/react-query';
import {
  fetchHistoryChart,
  fetchHistoryDeltas,
  fetchHistoryWindowDetail,
  fetchHistoryWindows,
} from '@/api/endpoints';
import type { HistoryWindowRow } from '@/api/types';
import { getUserTz } from '@/lib/tz';
import { rangeQueryParams, type DateRangeValue } from '@/lib/timeRange';

export type Metric = 'percent' | 'tokens' | 'cost';

export const useHistoryChart = (
  providerId: string | null,
  accountId: string | null,
  range: DateRangeValue,
  metric: Metric,
) => {
  const params = rangeQueryParams(range);
  return useQuery({
    queryKey: ['usage', 'history-chart', providerId, accountId, params, getUserTz(), metric],
    queryFn: () =>
      fetchHistoryChart({
        provider_id: providerId,
        account_id: accountId,
        ...params,
        metric,
      }),
    enabled: !!providerId && !!accountId,
    refetchInterval: 120_000,
  });
};

export const useHistoryDeltas = (
  range: DateRangeValue,
  providerId?: string | null,
  accountId?: string | null,
) => {
  const params = rangeQueryParams(range);
  return useQuery({
    queryKey: ['usage', 'history-deltas', params, getUserTz(), providerId, accountId],
    queryFn: () =>
      fetchHistoryDeltas({
        ...params,
        provider_id: providerId,
        account_id: accountId,
      }),
    refetchInterval: 120_000,
  });
};

export const useHistoryWindows = (
  providerId: string | null,
  accountId: string | null,
  range: DateRangeValue,
) => {
  const params = rangeQueryParams(range);
  return useQuery({
    queryKey: ['usage', 'history-windows', providerId, accountId, params, getUserTz()],
    queryFn: () =>
      fetchHistoryWindows({
        provider_id: providerId,
        account_id: accountId,
        ...params,
        limit: 50,
      }),
    refetchInterval: 300_000,
  });
};

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
