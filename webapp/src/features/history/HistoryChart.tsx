// Main history chart: % fill curves (metric=percent) or stacked
// token/cost buckets (metric=tokens|cost).

import { useMemo } from 'react';
import type { HistoryChartResponse } from '@/api/types';
import { EChart } from '@/components/charts/EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from '@/components/charts/theme';
import { formatTokens } from '@/lib/format';
import { formatLocalDateTime } from '@/lib/tz';
import type { Metric } from './queries';

export function HistoryChart({
  data,
  metric,
  className = 'h-72',
  excludeCache = false,
}: {
  data: HistoryChartResponse;
  metric: Metric;
  className?: string;
  // Drop the cache portion from each bar via the segment's value_cache (cache
  // tokens for metric=tokens, cache cost for metric=cost). Not applicable to percent.
  excludeCache?: boolean;
}) {
  const t = useChartTokens();

  const option = useMemo(() => {
    if (metric === 'percent') {
      const series = (data.series ?? []).map((s, i) => ({
        name: s.label,
        type: 'line' as const,
        showSymbol: false,
        connectNulls: false,
        data: s.points.map((p) => [Date.parse(p.ts), p.pct_used]),
        lineStyle: { width: 1.5, color: t.series[i % t.series.length] },
        itemStyle: { color: t.series[i % t.series.length] },
      }));
      return {
        tooltip: {
          trigger: 'axis' as const,
          ...baseTooltip(t),
          valueFormatter: (v: number | null) => (v == null ? '—' : `${Number(v).toFixed(0)}%`),
        },
        legend: {
          bottom: 0,
          type: 'scroll' as const,
          icon: 'circle',
          itemWidth: 8,
          itemHeight: 8,
          textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
        },
        grid: { left: 40, right: 16, top: 12, bottom: 40 },
        xAxis: {
          type: 'time' as const,
          ...baseAxisStyle(t),
          splitLine: { show: false },
        },
        yAxis: {
          type: 'value' as const,
          max: 100,
          ...baseAxisStyle(t),
          axisLabel: { color: t.axis, fontSize: 10, fontFamily: t.fontFamily, formatter: '{value}%' },
        },
        series,
      };
    }

    // tokens / cost: stacked buckets per label
    const bars = data.bars ?? [];
    const labels = Array.from(new Set(bars.flatMap((b) => b.segments.map((s) => s.label))));
    const categories = bars.map((b) => b.ts);
    const dropCache = excludeCache && (metric === 'tokens' || metric === 'cost');
    const series = labels.map((label, i) => ({
      name: label,
      type: 'bar' as const,
      stack: 'total',
      barMaxWidth: 18,
      itemStyle: { color: t.series[i % t.series.length], borderRadius: [0, 0, 0, 0] },
      data: bars.map((b) => {
        const seg = b.segments.find((s) => s.label === label);
        if (!seg) return 0;
        return dropCache ? seg.value - (seg.value_cache ?? 0) : seg.value;
      }),
    }));
    return {
      tooltip: {
        trigger: 'axis' as const,
        ...baseTooltip(t),
        valueFormatter: (v: number) =>
          metric === 'cost' ? `$${Number(v).toFixed(2)}` : formatTokens(Number(v)),
      },
      legend: {
        bottom: 0,
        type: 'scroll' as const,
        icon: 'circle',
        itemWidth: 8,
        itemHeight: 8,
        textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
      },
      grid: { left: 52, right: 16, top: 12, bottom: 40 },
      xAxis: {
        type: 'category' as const,
        data: categories,
        ...baseAxisStyle(t),
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          formatter: (value: string) =>
            formatLocalDateTime(value, { month: 'short', day: 'numeric', hour: '2-digit' }),
        },
      },
      yAxis: {
        type: 'value' as const,
        ...baseAxisStyle(t),
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          formatter: (v: number) => (metric === 'cost' ? `$${v}` : formatTokens(v)),
        },
      },
      series,
    };
  }, [data, metric, t, excludeCache]);

  return <EChart option={option} className={className} />;
}
