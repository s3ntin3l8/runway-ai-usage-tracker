// Cross-provider model ranking: stacked horizontal bars showing token or cost
// breakdown per model. Each segment represents a token type (input/output/
// reasoning/cache) or cost component. Rich tooltips show the full breakdown.
//
// This is a thin adapter over the shared RankBar: it maps the API entries
// onto RankRows and lets RankBar draw the chart, the legend, the tooltip and
// the text alternative. The one thing the adapter adds is the backend's
// authoritative cost total, which RankBar discloses next to the drawn total
// whenever the two disagree.

import { useMemo } from 'react';
import type { TopModelEntry } from '@/api/types';
import { buildSegments, costTotal } from '@/features/insights/rankSegments';
import { formatCost, formatTokens } from '@/lib/format';
import { RankBar, type RankRow } from './RankBar';
import { useChartTokens } from './theme';

export type TopMetric = 'tokens' | 'cost';

export function TopModelsBar({
  models,
  metric,
  excludeCache = false,
  className,
}: {
  models: TopModelEntry[];
  metric: TopMetric;
  excludeCache?: boolean;
  className?: string;
}) {
  const t = useChartTokens();

  const fmt = useMemo(
    () => (metric === 'cost' ? (v: number) => formatCost(v) : (v: number) => formatTokens(v)),
    [metric],
  );

  const rows = useMemo<RankRow[]>(
    () =>
      models.map((m) => {
        // The cache segments are dropped from the chart AND from the legend
        // when cache is excluded, so the chart describes only what it draws.
        const segments = buildSegments(m, metric, t.series, excludeCache ? 'omit' : 'include');
        return {
          label: m.model_id,
          // The bar length is the sum of the segments it draws. The backend's
          // authoritative cost total is disclosed beside it when the two
          // disagree (a provider that reports its own cost).
          value: segments.reduce((sum, s) => sum + s.value, 0),
          reportedValue: metric === 'cost' ? costTotal(m, excludeCache) : undefined,
          sub: m.providers.length ? `via ${m.providers.join(', ')}` : undefined,
          segments,
        };
      }),
    [models, metric, excludeCache, t.series],
  );

  return (
    <RankBar
      rows={rows}
      format={fmt}
      className={className ?? 'h-72'}
      ariaLabel="Top models chart"
      caption={metric === 'cost' ? 'Top models by cost' : 'Top models by tokens'}
    />
  );
}
