// Cross-provider model ranking: stacked horizontal bars showing token or cost
// breakdown per model. Each segment represents a token type (input/output/
// reasoning/cache) or cost component. Rich tooltips show the full breakdown.

import { useMemo } from 'react';
import type { TopModelEntry } from '@/api/types';
import { formatCost, formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

export type TopMetric = 'tokens' | 'cost';

interface Segment {
  key: string;
  label: string;
  color: string;
  get: (m: TopModelEntry) => number;
}

function getSegments(t: ReturnType<typeof useChartTokens>, metric: TopMetric): Segment[] {
  if (metric === 'cost') {
    return [
      { key: 'cost_input', label: 'Input', color: t.series[0], get: (m) => m.cost_input },
      { key: 'cost_output', label: 'Output', color: t.series[1], get: (m) => m.cost_output },
      {
        key: 'cost_cache_read',
        label: 'Cache read',
        color: t.series[2],
        get: (m) => m.cost_cache_read,
      },
      {
        key: 'cost_cache_create',
        label: 'Cache create',
        color: t.series[3],
        get: (m) => m.cost_cache_create,
      },
    ];
  }
  return [
    { key: 'tokens_input', label: 'Input', color: t.series[0], get: (m) => m.tokens_input },
    { key: 'tokens_output', label: 'Output', color: t.series[1], get: (m) => m.tokens_output },
    {
      key: 'tokens_reasoning',
      label: 'Reasoning',
      color: t.series[2],
      get: (m) => m.tokens_reasoning,
    },
    {
      key: 'tokens_cache_read',
      label: 'Cache read',
      color: t.series[3],
      get: (m) => m.tokens_cache_read,
    },
    {
      key: 'tokens_cache_create',
      label: 'Cache create',
      color: t.series[4],
      get: (m) => m.tokens_cache_create,
    },
  ];
}

function totalValue(m: TopModelEntry, metric: TopMetric, excludeCache: boolean): number {
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

  const option = useMemo(() => {
    const fmt = metric === 'cost' ? (v: number) => formatCost(v) : (v: number) => formatTokens(v);
    const segments = getSegments(t, metric);

    // Filter to models with data and sort ascending (ECharts draws bottom-up).
    const visible = models
      .filter((m) => totalValue(m, metric, excludeCache) > 0)
      .sort((a, b) => totalValue(a, metric, excludeCache) - totalValue(b, metric, excludeCache));

    const categoryData = visible.map((m) => m.model_id);

    const isCacheKey = (key: string) => key.startsWith('tokens_cache') || key.startsWith('cost_cache');

    // Build one series per segment, each stacked on 'total'.
    const rawSegments = segments.filter((seg) => !excludeCache || !isCacheKey(seg.key));
    const series = rawSegments.map((seg, i) => ({
      name: seg.label,
      type: 'bar' as const,
      stack: 'total',
      barMaxWidth: 18,
      data: visible.map((m) => seg.get(m)),
      itemStyle: {
        color: seg.color,
        // Round only the end-cap segment's right corners.
        borderRadius: i === rawSegments.length - 1 ? [0, 3, 3, 0] : 0,
      },
      emphasis: { itemStyle: { color: seg.color } },
    }));

    return {
      grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        ...baseTooltip(t),
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => {
          if (!params.length) return '';
          const idx = params[0].dataIndex;
          const m = visible[idx];
          if (!m) return '';

          const lines: string[] = [`<b>${m.model_id}</b>`];
          for (const p of params) {
            const val = p.value as number;
            if (val > 0) {
              lines.push(`${p.seriesName}: ${fmt(val)}`);
            }
          }
          lines.push(`<span style="opacity:.6">Total: ${fmt(totalValue(m, metric, excludeCache))}</span>`);
          if (m.providers.length) {
            lines.push(`<span style="opacity:.6">via ${m.providers.join(', ')}</span>`);
          }
          return lines.join('<br/>');
        },
      },
      xAxis: {
        type: 'value',
        ...baseAxisStyle(t),
        axisLabel: { ...baseAxisStyle(t).axisLabel, formatter: (v: number) => fmt(v) },
      },
      yAxis: {
        type: 'category',
        data: categoryData,
        ...baseAxisStyle(t),
        splitLine: { show: false },
        axisLabel: {
          color: t.fgMuted,
          fontSize: 11,
          fontFamily: t.monoFamily,
          formatter: (v: string) => (v.length > 10 ? v.slice(0, 10) + '…' : v),
        },
      },
      series,
    };
  }, [models, metric, excludeCache, t]);

  return <EChart option={option} className={className ?? 'h-72'} />;
}
