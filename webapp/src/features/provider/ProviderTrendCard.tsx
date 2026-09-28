// Per-day token or cost bars for one account. The window follows the shared
// time-range picker (`range`, resolved instants); callers without a picker
// (Forecast) fall back to a fixed last-7-days window. Reuses the History
// page's chart option-building (HistoryChart).

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { HistoryChart } from '@/features/history/HistoryChart';
import { useProviderHistoryChart, type DateRange } from './queries';
import type { Metric } from '@/features/history/queries';

export function ProviderTrendCard({
  providerId,
  accountId,
  metric,
  title,
  compact = false,
  range,
  excludeCache = false,
}: {
  providerId: string;
  accountId: string;
  metric: Exclude<Metric, 'percent'>;
  title: string;
  compact?: boolean;
  // When set, the bars are scoped to this closed period; otherwise a fixed
  // last-7-days window is used.
  range?: DateRange;
  // Drop cache tokens from the bars (token metric only — see HistoryChart).
  excludeCache?: boolean;
}) {
  const chart = useProviderHistoryChart(providerId, accountId, 7, metric, range);
  const hasData = (chart.data?.bars?.length ?? 0) > 0;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {chart.isPending ? (
          <Skeleton className={compact ? 'h-44 w-full' : 'h-72 w-full'} />
        ) : !hasData ? (
          <p className={`${compact ? 'py-10' : 'py-16'} text-center text-xs text-fg-subtle`}>
            No data in this range.
          </p>
        ) : (
          <HistoryChart
            data={chart.data!}
            metric={metric}
            className={compact ? 'h-44' : 'h-72'}
            excludeCache={excludeCache}
          />
        )}
      </CardContent>
    </Card>
  );
}
