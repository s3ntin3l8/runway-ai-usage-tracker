// Top Models card: cross-provider model ranking with a tokens/cost toggle.
// Shares the Insights page's time range and the global exclude-cache preference.

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton, SkeletonRegion } from '@/components/ui/Skeleton';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { TopModelsBar, type TopMetric } from '@/components/charts/TopModelsBar';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatRangeLabel, type DateRangeValue } from '@/lib/timeRange';
import { INSIGHTS_TOP_N, useTopModels } from './queries';
import { CircleAlert } from 'lucide-react';

export function TopModelsCard({ range }: { range: DateRangeValue }) {
  const { excludeCache } = useExcludeCache();
  const [metric, setMetric] = useState<TopMetric>('tokens');
  const top = useTopModels(metric, range, excludeCache);
  const hasData = (top.data?.models.length ?? 0) > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Top {INSIGHTS_TOP_N} models · {formatRangeLabel(range)}</CardTitle>
        <SegmentedControl
          value={metric}
          onChange={(v) => setMetric(v as TopMetric)}
          label="Top models metric"
          options={[
            { value: 'tokens', label: 'Tokens' },
            { value: 'cost', label: 'Cost' },
          ]}
        />
      </CardHeader>
      <CardContent className="pt-2">
        {top.isPending ? (
          <SkeletonRegion>
            <Skeleton className="h-72 w-full" />
          </SkeletonRegion>
        ) : top.isError ? (
          <EmptyState
            headingLevel="h3"
            icon={CircleAlert}
            title="Could not load this data"
            description={top.error.message}
            action={<Button size="sm" onClick={() => top.refetch()}>Retry</Button>}
          />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">No model usage in this range.</p>
        ) : (
          <TopModelsBar models={top.data!.models} metric={metric} excludeCache={excludeCache} />
        )}
      </CardContent>
    </Card>
  );
}
