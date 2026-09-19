// Cross-provider model ranking: stacked horizontal bars showing token or cost
// breakdown per model. Each segment represents a token type (input/output/
// reasoning/cache) or cost component. Rich tooltips show the full breakdown.

import { useMemo } from 'react';
import type { TopModelEntry } from '@/api/types';
import { formatCost, formatTokens } from '@/lib/format';
import { COST_SEGMENT_KEYS, TOKEN_SEGMENT_KEYS } from './segmentDefs';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

export type TopMetric = 'tokens' | 'cost';

interface Segment {
  key: string;
  label: string;
  color: string;
  get: (m: TopModelEntry) => number;
}

const ACCESSORS: Record<string, (m: TopModelEntry) => number> = {
  tokens_input: (m) => m.tokens_input,
  tokens_output: (m) => m.tokens_output,
  tokens_reasoning: (m) => m.tokens_reasoning,
  tokens_cache_read: (m) => m.tokens_cache_read,
  tokens_cache_create: (m) => m.tokens_cache_create,
  cost_input: (m) => m.cost_input,
  cost_output: (m) => m.cost_output,
  cost_cache_read: (m) => m.cost_cache_read,
  cost_cache_create: (m) => m.cost_cache_create,
};

function getSegments(t: ReturnType<typeof useChartTokens>, metric: TopMetric): Segment[] {
  const keys = metric === 'cost' ? COST_SEGMENT_KEYS : TOKEN_SEGMENT_KEYS;
  return keys.map((k, i) => ({
    key: k.key,
    label: k.label,
    color: t.series[i],
    get: ACCESSORS[k.key],
  }));
}

function totalValue(m: TopModelEntry, metric: TopMetric, excludeCache: boolean): number {
  if (metric === 'cost') {
    return (
      m.cost_input +
      m.cost_output +
      (excludeCache ? 0 : m.cost_cache_read + m.cost_cache_create)
    );
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
          if (metric === 'cost' && Math.abs(m.cost_usd - totalValue(m, metric, excludeCache)) > 0.001) {
            lines.push(`<span style="opacity:.6">Reported: ${fmt(m.cost_usd)}</span>`);
          }
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
