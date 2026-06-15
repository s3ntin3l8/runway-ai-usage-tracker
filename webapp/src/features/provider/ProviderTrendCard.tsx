// Per-day token or cost bars for one account, with a range selector.
// Reuses the History page's chart option-building (HistoryChart) and the
// shared Tabs segmented control.

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { HistoryChart } from '@/features/history/HistoryChart';
import { useProviderHistoryChart, type DateRange } from './queries';
import type { Metric } from '@/features/history/queries';

const RANGES = [7, 14, 30, 90];

export function ProviderTrendCard({
  providerId,
  accountId,
  metric,
  title,
  defaultDays = 30,
  compact = false,
  range,
  excludeCache = false,
}: {
  providerId: string;
  accountId: string;
  metric: Exclude<Metric, 'percent'>;
  title: string;
  defaultDays?: number;
  compact?: boolean;
  // When set, the bars are scoped to this closed period and the day-range tabs
  // are hidden (the period is fixed by the shared month selector instead).
  range?: DateRange;
  // Drop cache tokens from the bars (token metric only — see HistoryChart).
  excludeCache?: boolean;
}) {
  const [days, setDays] = useState(defaultDays);
  const chart = useProviderHistoryChart(providerId, accountId, days, metric, range);
  const hasData = (chart.data?.bars?.length ?? 0) > 0;

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>{title}</CardTitle>
        {range ? null : (
          <Tabs value={String(days)} onValueChange={(v) => setDays(Number(v))}>
            <TabsList className="border-0">
              {RANGES.map((d) => (
                <TabsTrigger key={d} value={String(d)} className="h-8 px-2">
                  {d}d
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        )}
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
