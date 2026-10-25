// Top Projects card: ranks working directories by tokens, cost, or session
// count. Stacked bars show token/cost breakdown when the metric is tokens or
// cost. Reused globally on Insights and per-provider on the Activity tab.

import { useState } from 'react';
import type { TopProjectEntry } from '@/api/types';
import { RankBar, type RankRow } from '@/components/charts/RankBar';
import { useChartTokens } from '@/components/charts/theme';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatAxisCost, formatAxisNumber, formatCost, formatCount, formatTokens } from '@/lib/format';
import { formatRangeLabel, type DateRangeValue } from '@/lib/timeRange';
import { INSIGHTS_TOP_N, type ProjectMetric, useTopProjects } from './queries';
import { costSegments, costTotal, tokenSegments } from './rankSegments';
import { CircleAlert } from 'lucide-react';

function projectRow(
  p: TopProjectEntry,
  metric: ProjectMetric,
  excludeCache: boolean,
  series: string[],
): RankRow {
  const sub = p.providers.length ? `via ${p.providers.join(', ')}` : undefined;

  if (metric === 'sessions') {
    return { label: p.project, value: p.sessions, sub };
  }

  if (metric === 'cost') {
    const segments = costSegments(p, series, excludeCache);
    return {
      label: p.project,
      // The bar length is the sum of the segments the bar draws; the
      // backend's authoritative cost total is disclosed beside it when the
      // two disagree (a provider that reports its own cost).
      value: segments.reduce((sum, s) => sum + s.value, 0),
      reportedValue: costTotal(p, excludeCache),
      sub,
      segments,
    };
  }

  // tokens — the backend's token total IS the sum of the components, so
  // there is nothing to disclose beside the drawn total.
  const segments = tokenSegments(p, series, excludeCache);
  return {
    label: p.project,
    value: segments.reduce((sum, s) => sum + s.value, 0),
    sub,
    segments,
  };
}

const FORMAT: Record<ProjectMetric, (v: number) => string> = {
  tokens: (v) => formatTokens(v),
  cost: (v) => formatCost(v),
  sessions: (v) => formatCount(v),
};

const AXIS_FORMAT: Record<ProjectMetric, (v: number) => string> = {
  tokens: formatAxisNumber,
  cost: formatAxisCost,
  sessions: formatAxisNumber,
};

// The shared range value scopes the ranking; `providerId` narrows it to one
// provider (Activity). `title` overrides the default `Top ${INSIGHTS_TOP_N} projects · label`.
export function TopProjectsCard({
  range,
  providerId,
  title,
}: {
  range: DateRangeValue;
  providerId?: string;
  title?: string;
}) {
  const { excludeCache } = useExcludeCache();
  const t = useChartTokens();
  const [metric, setMetric] = useState<ProjectMetric>('tokens');
  const top = useTopProjects(metric, excludeCache, range, providerId);
  const rows = (top.data?.projects ?? []).map((p) => projectRow(p, metric, excludeCache, t.series));
  const hasData = rows.some((r) => r.value > 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title ?? `Top ${INSIGHTS_TOP_N} projects · ${formatRangeLabel(range)}`}</CardTitle>
        <SegmentedControl
          value={metric}
          onChange={(v) => setMetric(v as ProjectMetric)}
          label="Top projects metric"
          options={[
            { value: 'tokens', label: 'Tokens' },
            { value: 'cost', label: 'Cost' },
            { value: 'sessions', label: 'Sessions' },
          ]}
        />
      </CardHeader>
      <CardContent className="pt-2">
        {top.isPending ? (
          <Skeleton className="h-72 w-full" />
        ) : top.isError ? (
          <EmptyState
            headingLevel="h3"
            icon={CircleAlert}
            title="Could not load this data"
            description={top.error.message}
            action={<Button size="sm" onClick={() => top.refetch()}>Retry</Button>}
          />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">
            No project-attributed usage in this range.
          </p>
        ) : (
          <RankBar rows={rows} format={FORMAT[metric]} axisFormat={AXIS_FORMAT[metric]} />
        )}
      </CardContent>
    </Card>
  );
}
