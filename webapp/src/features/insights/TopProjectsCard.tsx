// Top Projects card: ranks working directories by tokens, cost, or session
// count. Stacked bars show token/cost breakdown when the metric is tokens or
// cost. Reused globally on Insights and per-provider on the Activity tab.

import { useState } from 'react';
import type { TopProjectEntry } from '@/api/types';
import { RankBar, type RankRow, type RankSegment } from '@/components/charts/RankBar';
import { COST_SEGMENT_KEYS, TOKEN_SEGMENT_KEYS } from '@/components/charts/segmentDefs';
import { useChartTokens } from '@/components/charts/theme';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatAxisCost, formatAxisNumber, formatCost, formatTokens } from '@/lib/format';
import { formatRangeLabel, type DateRangeValue } from '@/lib/timeRange';
import { INSIGHTS_TOP_N, type ProjectMetric, useTopProjects } from './queries';
import { CircleAlert } from 'lucide-react';

const COST_ACCESSORS: Record<string, (p: TopProjectEntry) => number> = {
  cost_input: (p) => p.cost_input,
  cost_output: (p) => p.cost_output,
  cost_cache_read: (p) => p.cost_cache_read,
  cost_cache_create: (p) => p.cost_cache_create,
};

const TOKEN_ACCESSORS: Record<string, (p: TopProjectEntry) => number> = {
  tokens_input: (p) => p.tokens_input,
  tokens_output: (p) => p.tokens_output,
  tokens_reasoning: (p) => p.tokens_reasoning,
  tokens_cache_read: (p) => p.tokens_cache_read,
  tokens_cache_create: (p) => p.tokens_cache_create,
};

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
    const accessors = COST_ACCESSORS;
    const segments: RankSegment[] = COST_SEGMENT_KEYS.map((k, i) => ({
      key: k.key,
      label: k.label,
      color: series[i],
      value: accessors[k.key]?.(p) ?? 0,
    }));
    if (excludeCache) {
      for (const s of segments) {
        if (s.key.startsWith('cost_cache')) s.value = 0;
      }
    }
    return {
      label: p.project,
      value: p.cost_usd - (excludeCache ? p.cost_cache : 0),
      sub,
      segments,
    };
  }

  // tokens
  const accessors = TOKEN_ACCESSORS;
  const segments: RankSegment[] = TOKEN_SEGMENT_KEYS.map((k, i) => ({
    key: k.key,
    label: k.label,
    color: series[i],
    value: accessors[k.key]?.(p) ?? 0,
  }));
  if (excludeCache) {
    for (const s of segments) {
      if (s.key.startsWith('tokens_cache')) s.value = 0;
    }
  }
  const cache = p.tokens_cache_read + p.tokens_cache_create;
  return {
    label: p.project,
    value: p.tokens_input + p.tokens_output + p.tokens_reasoning + (excludeCache ? 0 : cache),
    sub,
    segments,
  };
}

const FORMAT: Record<ProjectMetric, (v: number) => string> = {
  tokens: (v) => formatTokens(v),
  cost: (v) => formatCost(v),
  sessions: (v) => v.toLocaleString(),
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
