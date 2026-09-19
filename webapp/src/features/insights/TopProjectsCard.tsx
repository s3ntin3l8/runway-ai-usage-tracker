// Top Projects card: ranks working directories by tokens, cost, or session
// count. Stacked bars show token/cost breakdown when the metric is tokens or
// cost. Reused globally on Insights and per-provider on the Activity tab.

import { useState } from 'react';
import type { TopProjectEntry } from '@/api/types';
import { RankBar, type RankRow, type RankSegment } from '@/components/charts/RankBar';
import { COST_SEGMENT_KEYS, TOKEN_SEGMENT_KEYS } from '@/components/charts/segmentDefs';
import { useChartTokens } from '@/components/charts/theme';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatCost, formatTokens } from '@/lib/format';
import { useTopProjects, type ProjectMetric } from './queries';

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

// Either a rolling `days` window (History, cross-provider) or a month `range`
// + `providerId` (Activity, per-provider). `title` overrides the default.
export function TopProjectsCard({
  days,
  range,
  providerId,
  title,
}: {
  days?: number;
  range?: { since: string; until: string };
  providerId?: string;
  title?: string;
}) {
  const { excludeCache } = useExcludeCache();
  const t = useChartTokens();
  const [metric, setMetric] = useState<ProjectMetric>('tokens');
  const top = useTopProjects(metric, excludeCache, { days, range, providerId });
  const rows = (top.data?.projects ?? []).map((p) => projectRow(p, metric, excludeCache, t.series));
  const hasData = rows.some((r) => r.value > 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title ?? `Top projects · ${days}d`}</CardTitle>
        <Tabs value={metric} onValueChange={(v) => setMetric(v as ProjectMetric)}>
          <TabsList className="border-0" aria-label="Top projects metric">
            <TabsTrigger value="tokens" className="h-8 px-2.5">
              Tokens
            </TabsTrigger>
            <TabsTrigger value="cost" className="h-8 px-2.5">
              Cost
            </TabsTrigger>
            <TabsTrigger value="sessions" className="h-8 px-2.5">
              Sessions
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </CardHeader>
      <CardContent className="pt-2">
        {top.isPending ? (
          <Skeleton className="h-72 w-full" />
        ) : !hasData ? (
          <p className="py-16 text-center text-xs text-fg-subtle">
            No project-attributed usage in this range.
          </p>
        ) : (
          <RankBar rows={rows} format={FORMAT[metric]} />
        )}
      </CardContent>
    </Card>
  );
}
