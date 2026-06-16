// Top Projects card: ranks working directories by tokens, cost, or session
// count. Reused in two places — globally on Insights (no providerId, spans every
// provider) and per-provider on the Activity tab. Shares the exclude-cache pref.

import { useState } from 'react';
import type { TopProjectEntry } from '@/api/types';
import { RankBar, type RankRow } from '@/components/charts/RankBar';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatCost, formatTokens } from '@/lib/format';
import { useTopProjects, type ProjectMetric } from './queries';

function projectRow(p: TopProjectEntry, metric: ProjectMetric, excludeCache: boolean): RankRow {
  const sub = p.providers.length ? `via ${p.providers.join(', ')}` : undefined;
  if (metric === 'sessions') return { label: p.project, value: p.sessions, sub };
  if (metric === 'cost') {
    return { label: p.project, value: p.cost_usd - (excludeCache ? p.cost_cache : 0), sub };
  }
  const cache = p.tokens_cache_read + p.tokens_cache_create;
  return {
    label: p.project,
    value: p.tokens_input + p.tokens_output + p.tokens_reasoning + (excludeCache ? 0 : cache),
    sub,
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
  const [metric, setMetric] = useState<ProjectMetric>('tokens');
  const top = useTopProjects(metric, excludeCache, { days, range, providerId });
  const rows = (top.data?.projects ?? []).map((p) => projectRow(p, metric, excludeCache));
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
