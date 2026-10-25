// Top Hosts card: ranks the machines that pushed the events, across every
// provider/account. Answers "which host drove this?" in one glance.
//
// A host is the `sidecar_id` of the events — the hostname that pushed them —
// so the ranking follows the same identity the Fleet page shows. Rows are
// labelled with the Fleet registry's name for that host (custom name, then
// hostname) and fall back to the raw id for a host the server has not
// registered.

import { useState } from 'react';
import type { TopHostEntry } from '@/api/types';
import { RankBar, type RankRow } from '@/components/charts/RankBar';
import { useChartTokens } from '@/components/charts/theme';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { Skeleton } from '@/components/ui/Skeleton';
import { SegmentedControl } from '@/components/ui/SegmentedControl';
import { buildSidecarNameMap, useSidecars } from '@/features/fleet/queries';
import { useExcludeCache } from '@/hooks/useExcludeCache';
import { formatAxisCost, formatAxisNumber, formatCost, formatTokens } from '@/lib/format';
import { formatRangeLabel, type DateRangeValue } from '@/lib/timeRange';
import { INSIGHTS_TOP_N, type TopMetric, useTopHosts } from './queries';
import { costSegments, costTotal, tokenSegments, tokenTotal } from './rankSegments';
import { CircleAlert } from 'lucide-react';

function hostRow(
  h: TopHostEntry,
  metric: TopMetric,
  excludeCache: boolean,
  series: string[],
  hostNames: Map<string, string>,
): RankRow {
  const sub = [
    `${h.sessions} ${h.sessions === 1 ? 'session' : 'sessions'}`,
    ...(h.providers.length ? [`via ${h.providers.join(', ')}`] : []),
  ].join(' · ');
  const label = hostNames.get(h.sidecar_id) ?? h.sidecar_id;

  if (metric === 'cost') {
    const segments = costSegments(h, series, excludeCache);
    return {
      label,
      // The bar is the sum of the segments it draws, so the bar can never
      // contradict them; the backend's authoritative total is disclosed
      // beside it when the two differ.
      value: segments.reduce((sum, seg) => sum + seg.value, 0),
      reportedValue: costTotal(h, excludeCache),
      sub,
      segments,
    };
  }
  return {
    label,
    value: tokenTotal(h, excludeCache),
    sub,
    segments: tokenSegments(h, series, excludeCache),
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

export function TopHostsCard({ range }: { range: DateRangeValue }) {
  const { excludeCache } = useExcludeCache();
  const t = useChartTokens();
  const [metric, setMetric] = useState<TopMetric>('tokens');
  const top = useTopHosts(metric, range, excludeCache);
  const sidecars = useSidecars();
  const hostNames = buildSidecarNameMap(sidecars.data?.sidecars ?? []);
  const rows = (top.data?.hosts ?? []).map((h) => hostRow(h, metric, excludeCache, t.series, hostNames));
  const hasData = rows.some((r) => r.value > 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Top {INSIGHTS_TOP_N} hosts · {formatRangeLabel(range)}</CardTitle>
        <SegmentedControl
          value={metric}
          onChange={(v) => setMetric(v as TopMetric)}
          label="Top hosts metric"
          options={[
            { value: 'tokens', label: 'Tokens' },
            { value: 'cost', label: 'Cost' },
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
          <p className="py-16 text-center text-xs text-fg-subtle">No host-attributed usage in this range.</p>
        ) : (
          <RankBar rows={rows} format={FORMAT[metric]} axisFormat={AXIS_FORMAT[metric]} />
        )}
      </CardContent>
    </Card>
  );
}
