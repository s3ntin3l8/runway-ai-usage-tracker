import { useQuery } from '@tanstack/react-query';
import {
  fetchHistoryChart,
  fetchHistoryDeltas,
  fetchHistoryWindowDetail,
  fetchHistoryWindows,
} from '@/api/endpoints';
import type { HistoryWindowRow } from '@/api/types';

export type Metric = 'percent' | 'tokens' | 'cost';

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
