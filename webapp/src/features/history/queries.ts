import { useQuery } from '@tanstack/react-query';
import {
  fetchHistoryChart,
  fetchHistoryDeltas,
  fetchHistoryWindowDetail,
  fetchHistoryWindows,
} from '@/api/endpoints';
import type { HistoryWindowRow } from '@/api/types';
import type { DateRangeValue } from '@/components/ui/DateRangeTabs';
import { getUserTz, localDateStartISO, nextCalendarDate } from '@/lib/tz';

export type Metric = 'percent' | 'tokens' | 'cost';

type RangeInput = DateRangeValue | number;

function toDateRange(v: RangeInput): DateRangeValue {
  return typeof v === 'number' ? { days: v } : v;
}

export function dateRangeParams(range: DateRangeValue): Record<string, unknown> {
  if (range.since && range.until) {
    return {
      since: localDateStartISO(range.since),
      until: localDateStartISO(nextCalendarDate(range.until)),
    };
  }
  return { days: range.days ?? 7 };
}

export const useHistoryChart = (
  providerId: string | null,
  accountId: string | null,
  range: RangeInput,
  metric: Metric,
) => {
  const dr = toDateRange(range);
  const params = dateRangeParams(dr);
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
  range: RangeInput,
  providerId?: string | null,
  accountId?: string | null,
) => {
  const dr = toDateRange(range);
  const params = dateRangeParams(dr);
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
  range: RangeInput,
) => {
  const dr = toDateRange(range);
  const params = dateRangeParams(dr);
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
