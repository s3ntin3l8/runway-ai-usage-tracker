// Cross-provider model ranking: one horizontal bar per model_id, sized by
// tokens or cost. Mirrors the server's sort (the displayed metric drives the
// order) and honours the shared exclude-cache toggle the same way the donuts do.

import { useMemo } from 'react';
import type { TopModelEntry } from '@/api/types';
import { formatCost, formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

export type TopMetric = 'tokens' | 'cost';

function modelValue(m: TopModelEntry, metric: TopMetric, excludeCache: boolean): number {
  if (metric === 'cost') {
    return m.cost_usd - (excludeCache ? m.cost_cache : 0);
  }
  return (
    m.tokens_input +
    m.tokens_output +
    m.tokens_reasoning +
    (excludeCache ? 0 : m.tokens_cache_read + m.tokens_cache_create)
  );
}

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
  const fmt = metric === 'cost' ? (v: number) => formatCost(v) : (v: number) => formatTokens(v);

  const option = useMemo(() => {
    // Server already ordered by the metric; re-sort defensively after the
    // exclude-cache recompute so the bars stay monotonic. ECharts category
    // axis draws bottom-up, so put the largest at the top via `inverse`.
    const rows = models
      .map((m) => ({ name: m.model_id, value: modelValue(m, metric, excludeCache), providers: m.providers }))
      .filter((r) => r.value > 0)
      .sort((a, b) => a.value - b.value);

    return {
      grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        ...baseTooltip(t),
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: { name: string; value: number; dataIndex: number }[]) => {
          const p = params[0];
          const providers = rows[p.dataIndex]?.providers ?? [];
          const via = providers.length ? `<br/><span style="opacity:.6">via ${providers.join(', ')}</span>` : '';
          return `${p.name}: ${fmt(p.value)}${via}`;
        },
      },
      xAxis: {
        type: 'value',
        ...baseAxisStyle(t),
        axisLabel: { ...baseAxisStyle(t).axisLabel, formatter: (v: number) => fmt(v) },
      },
      yAxis: {
        type: 'category',
        data: rows.map((r) => r.name),
        ...baseAxisStyle(t),
        splitLine: { show: false },
        axisLabel: {
          color: t.fgMuted,
          fontSize: 11,
          fontFamily: t.monoFamily,
          formatter: (v: string) => (v.length > 10 ? v.slice(0, 10) + '…' : v),
        },
      },
      series: [
        {
          type: 'bar',
          data: rows.map((r) => r.value),
          barMaxWidth: 18,
          itemStyle: { color: t.accent, borderRadius: [0, 3, 3, 0] },
          emphasis: { itemStyle: { color: t.series[0] } },
        },
      ],
    };
  }, [models, metric, excludeCache, t, fmt]);

  return <EChart option={option} className={className ?? 'h-72'} />;
}
