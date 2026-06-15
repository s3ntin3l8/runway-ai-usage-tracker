// Top Models card: cross-provider model ranking with a tokens/cost toggle.
// Shares the History page's day-range and the global exclude-cache preference.

import { useState } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { TopModelsBar, type TopMetric } from '@/components/charts/TopModelsBar';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { useTopModels } from './queries';

export function TopModelsCard({ days }: { days: number }) {
  const { excludeCache } = useExcludeCache();
  const [metric, setMetric] = useState<TopMetric>('tokens');
  const top = useTopModels(metric, days, excludeCache);
  const hasData = (top.data?.models.length ?? 0) > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Top models · {days}d</CardTitle>
        <Tabs value={metric} onValueChange={(v) => setMetric(v as TopMetric)}>
          <TabsList className="border-0" aria-label="Top models metric">
            <TabsTrigger value="tokens" className="h-8 px-2.5">
              Tokens
            </TabsTrigger>
            <TabsTrigger value="cost" className="h-8 px-2.5">
              Cost
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </CardHeader>
      <CardContent className="pt-2">
        {top.isPending ? (
          <Skeleton className="h-72 w-full" />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">No model usage in this range.</p>
        ) : (
          <TopModelsBar models={top.data!.models} metric={metric} excludeCache={excludeCache} />
        )}
      </CardContent>
    </Card>
  );
}
