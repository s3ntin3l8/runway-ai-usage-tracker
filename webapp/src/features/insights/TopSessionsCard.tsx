// Top Sessions card: ranks the sessions behind a usage spike, across every
// provider/account. Answers "which session drove this?" in one glance — the
// per-provider Sessions browser (provider detail → Sessions) stays the
// detailed drill-down.
//
// Each row is labelled by project (falling back to the tail of the session
// id) and names the host that pushed the session's latest event, so a spike
// can be traced to a machine without leaving the page. A session that moved
// between machines says so.

import { useState } from 'react';
import type { TopSessionEntry } from '@/api/types';
import { RankBar, type RankRow } from '@/components/charts/RankBar';
import { useChartTokens } from '@/components/charts/theme';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Skeleton } from '@/components/ui/Skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatAxisCost, formatAxisNumber, formatCost, formatTokens } from '@/lib/format';
import { formatRangeLabel, type DateRangeValue } from '@/lib/timeRange';
import { useTopSessions, type TopMetric } from './queries';
import { costSegments, costTotal, tokenSegments, tokenTotal } from './rankSegments';

/** The tail of a session id — enough to recognize one, short enough to label a bar. */
function shortSessionId(sessionId: string): string {
  return sessionId.length > 8 ? sessionId.slice(-8) : sessionId;
}

function sessionRow(
  s: TopSessionEntry,
  metric: TopMetric,
  excludeCache: boolean,
  series: string[],
  hostNames: Map<string, string>,
): RankRow {
  // `sidecar_id` is the host that pushed the session's *latest* event, so it
  // is the host the session ended on. A session that moved between machines
  // says so, rather than pretending it only ever ran on one.
  const host = s.sidecar_id ? (hostNames.get(s.sidecar_id) ?? s.sidecar_id) : 'unknown host';
  const sub = [
    shortSessionId(s.session_id),
    host,
    ...(s.hosts > 1 ? [`${s.hosts} hosts`] : []),
    ...(s.providers.length ? [`via ${s.providers.join(', ')}`] : []),
  ].join(' · ');
  const label = s.project || shortSessionId(s.session_id);

  if (metric === 'cost') {
    return {
      label,
      value: costTotal(s, excludeCache),
      sub,
      segments: costSegments(s, series, excludeCache),
    };
  }
  return {
    label,
    value: tokenTotal(s, excludeCache),
    sub,
    segments: tokenSegments(s, series, excludeCache),
  };
}

const FORMAT: Record<TopMetric, (v: number) => string> = {
  tokens: (v) => formatTokens(v),
  cost: (v) => formatCost(v),
};

const AXIS_FORMAT: Record<TopMetric, (v: number) => string> = {
  tokens: formatAxisNumber,
  cost: formatAxisCost,
};

export function TopSessionsCard({ range }: { range: DateRangeValue }) {
  const { excludeCache } = useExcludeCache();
  const t = useChartTokens();
  const [metric, setMetric] = useState<TopMetric>('tokens');
  const top = useTopSessions(metric, range, excludeCache);
  const sidecars = useSidecars();
  const hostNames = buildSidecarNameMap(sidecars.data?.sidecars ?? []);
  const rows = (top.data?.sessions ?? []).map((s) =>
    sessionRow(s, metric, excludeCache, t.series, hostNames),
  );
  const hasData = rows.some((r) => r.value > 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Top sessions · {formatRangeLabel(range)}</CardTitle>
        <Tabs value={metric} onValueChange={(v) => setMetric(v as TopMetric)}>
          <TabsList className="border-0" aria-label="Top sessions metric">
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
          <p className="py-16 text-center text-xs text-fg-subtle">
            No session-attributed usage in this range.
          </p>
        ) : (
          <RankBar rows={rows} format={FORMAT[metric]} axisFormat={AXIS_FORMAT[metric]} />
        )}
      </CardContent>
    </Card>
  );
}
