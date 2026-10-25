// Cross-provider model ranking: stacked horizontal bars showing token or cost
// breakdown per model. Each segment represents a token type (input/output/
// reasoning/cache) or cost component. Rich tooltips show the full breakdown.

import { useMemo } from 'react';
import type { TopModelEntry } from '@/api/types';
import { formatCost, formatTokens } from '@/lib/format';
import { COST_SEGMENT_KEYS, TOKEN_SEGMENT_KEYS } from './segmentDefs';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
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
    // Colour by segment key, not by position: the key list is fixed and the
    // cache segments are filtered out of the series later, so a segment
    // keeps its colour whether or not cache is excluded.
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

function isCacheKey(key: string): boolean {
  return key.startsWith('tokens_cache') || key.startsWith('cost_cache');
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

  const fmt = useMemo(
    () => (metric === 'cost' ? (v: number) => formatCost(v) : (v: number) => formatTokens(v)),
    [metric],
  );

  // The segments are filtered to the ones actually drawn, so the legend and
  // the text alternative describe the chart as it is rendered.
  const segments = useMemo(() => {
    const all = getSegments(t, metric);
    return all.filter((seg) => !excludeCache || !isCacheKey(seg.key));
  }, [t, metric, excludeCache]);

  // Filter to models with data and sort ascending (ECharts draws bottom-up).
  const visible = useMemo(
    () =>
      models
        .filter((m) => totalValue(m, metric, excludeCache) > 0)
        .sort((a, b) => totalValue(a, metric, excludeCache) - totalValue(b, metric, excludeCache)),
    [models, metric, excludeCache],
  );

  const option = useMemo(() => {
    const categoryData = visible.map((m) => m.model_id);

    // Build one series per segment, each stacked on 'total'.
    const series = segments.map((seg, i) => ({
      name: seg.label,
      type: 'bar' as const,
      stack: 'total',
      barMaxWidth: 18,
      data: visible.map((m) => seg.get(m)),
      itemStyle: {
        color: seg.color,
        // Round only the end-cap segment's right corners.
        borderRadius: i === segments.length - 1 ? [0, 3, 3, 0] : 0,
      },
    }));

    return {
      // The legend needs its own strip of grid: the stacked bars carry no
      // visible segment names, and on touch there is no hover tooltip to
      // ask which block is which. The strip is tall enough for the legend to
      // wrap to two lines on a narrow card without covering the x-axis
      // labels (one line ≈ 24px, two ≈ 48px).
      grid: { left: 8, right: 16, top: 8, bottom: 48, containLabel: true },
      legend: {
        bottom: 0,
        icon: 'square',
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
      },
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
  }, [visible, segments, metric, excludeCache, t, fmt]);

  return (
    <>
      <EChart option={option} className={className ?? 'h-72'} ariaLabel="Top models chart" />
      {/* The bar lengths and their segment split live only in the canvas and
          the hover tooltip, so they need a text alternative. */}
      <AccessibleSummary
        caption={metric === 'cost' ? 'Top models by cost' : 'Top models by tokens'}
        entries={visible.map((m) => ({
          label: m.model_id,
          value: fmt(totalValue(m, metric, excludeCache)),
          note: m.providers.length ? `via ${m.providers.join(', ')}` : undefined,
          details: segments
            .map((seg) => ({ label: seg.label, value: seg.get(m) }))
            .filter((d) => d.value > 0)
            .map((d) => ({ label: d.label, value: fmt(d.value) })),
        }))}
      />
    </>
  );
}
