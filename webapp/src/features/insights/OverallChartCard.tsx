// Overall usage: cross-provider tokens/cost over the selected range, stacked
// per provider. Range-total tiles sit above the chart. Shares the Insights
// page's day-range and the global exclude-cache preference.

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatTile } from '@/components/ui/StatTile';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { HistoryChart } from '@/features/history/HistoryChart';
import { useHistoryDeltas } from '@/features/history/queries';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatCost, formatTokens } from '@/lib/format';
import { useOverallChart, type OverallMetric } from './queries';

export function OverallChartCard({ days }: { days: number }) {
  const { excludeCache } = useExcludeCache();
  const [metric, setMetric] = useState<OverallMetric>('tokens');
  const chart = useOverallChart(days, metric);
  const deltas = useHistoryDeltas(days);

  const hasData = (chart.data?.bars?.length ?? 0) > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Overall usage · {days}d</CardTitle>
        <Tabs value={metric} onValueChange={(v) => setMetric(v as OverallMetric)}>
          <TabsList className="border-0" aria-label="Overall metric">
            <TabsTrigger value="tokens" className="h-8 px-2.5">
              Tokens
            </TabsTrigger>
            <TabsTrigger value="cost" className="h-8 px-2.5">
              Cost
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 pt-2">
        <div className="grid grid-cols-2 gap-3">
          <StatTile
            label={`Tokens (${days}d)`}
            value={formatTokens(deltas.data?.token_delta_total ?? 0)}
            loading={deltas.isPending}
          />
          <StatTile
            label={`Cost (${days}d)`}
            value={formatCost(deltas.data?.cost_delta_total)}
            loading={deltas.isPending}
          />
        </div>
        {chart.isPending ? (
          <Skeleton className="h-72 w-full" />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">No usage in this range.</p>
        ) : (
          <HistoryChart data={chart.data!} metric={metric} excludeCache={excludeCache} />
        )}
      </CardContent>
    </Card>
  );
}
