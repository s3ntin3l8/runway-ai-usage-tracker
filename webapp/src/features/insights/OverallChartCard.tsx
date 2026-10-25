// Overall usage: cross-provider tokens/cost over the selected range, stacked
// per provider. Range-total tiles sit above the chart. Shares the Insights
// page's time range and the global exclude-cache preference.

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { StatTile } from '@/components/ui/StatTile';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { HistoryChart } from '@/features/history/HistoryChart';
import { useHistoryDeltas } from '@/features/history/queries';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatCost, formatTokens } from '@/lib/format';
import { formatRangeLabel, type DateRangeValue } from '@/lib/timeRange';
import { useOverallChart, type OverallMetric } from './queries';
import { CircleAlert } from 'lucide-react';

export function OverallChartCard({ range }: { range: DateRangeValue }) {
  const { excludeCache } = useExcludeCache();
  const [metric, setMetric] = useState<OverallMetric>('tokens');
  const chart = useOverallChart(range, metric);
  const deltas = useHistoryDeltas(range);
  const rangeLabel = formatRangeLabel(range);

  const hasData = (chart.data?.bars?.length ?? 0) > 0;

  const rangeTokens =
    (deltas.data?.token_delta_total ?? 0) -
    (excludeCache ? (deltas.data?.token_cache_total ?? 0) : 0);
  const rangeCost =
    (deltas.data?.cost_delta_total ?? 0) -
    (excludeCache ? (deltas.data?.cost_cache_total ?? 0) : 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Overall usage · {rangeLabel}</CardTitle>
        <SegmentedControl
          value={metric}
          onChange={(v) => setMetric(v as OverallMetric)}
          label="Overall metric"
          options={[
            { value: 'tokens', label: 'Tokens' },
            { value: 'cost', label: 'Cost' },
          ]}
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-3 pt-2">
        <div className="grid grid-cols-2 gap-3">
          <StatTile
            label={`Tokens (${rangeLabel})`}
            value={formatTokens(rangeTokens)}
            loading={deltas.isPending}
          />
          <StatTile
            label={`Cost (${rangeLabel})`}
            value={formatCost(rangeCost)}
            loading={deltas.isPending}
          />
        </div>
        {chart.isPending ? (
          <Skeleton className="h-72 w-full" />
        ) : chart.isError ? (
          <EmptyState
            icon={CircleAlert}
            title="Could not load this data"
            description={chart.error.message}
            action={<Button size="sm" onClick={() => chart.refetch()}>Retry</Button>}
          />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">No usage in this range.</p>
        ) : (
          <HistoryChart data={chart.data!} metric={metric} excludeCache={excludeCache} />
        )}
      </CardContent>
    </Card>
  );
}
