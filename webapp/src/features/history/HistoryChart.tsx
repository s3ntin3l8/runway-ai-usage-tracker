// Main history chart: % fill curves (metric=percent) or stacked
// token/cost buckets (metric=tokens|cost).

import { useMemo } from 'react';
import type { HistoryChartResponse } from '@/api/types';
import { EChart } from '@/components/charts/EChart';
import {
  baseAxisStyle,
  baseTooltip,
  otherSeriesLabel,
  splitChartSeries,
  useChartTokens,
} from '@/components/charts/theme';
import { formatAxisCost, formatCost, formatTokens } from '@/lib/format';
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

  // The palette has six colours, so the series are capped instead of wrapping
  // the palette (which gave the seventh series the first series' colour). The
  // bar mode aggregates the overflow into one "Other" segment; the percent
  // mode keeps the first six curves and says how many it left out.
  const percentCap = useMemo(() => splitChartSeries(data.series ?? []), [data]);
  const barCap = useMemo(() => {
    const labels = Array.from(
      new Set((data.bars ?? []).flatMap((b) => b.segments.map((s) => s.label))),
    );
    return splitChartSeries(labels);
  }, [data]);

  const option = useMemo(() => {
    if (metric === 'percent') {
      const series = percentCap.kept.map((s, i) => ({
        name: s.label,
        type: 'line' as const,
        showSymbol: false,
        connectNulls: false,
        data: s.points.map((p) => [Date.parse(p.ts), p.pct_used]),
        lineStyle: { width: 1.5, color: t.series[i] },
        itemStyle: { color: t.series[i] },
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
    const categories = bars.map((b) => b.ts);
    const dropCache = excludeCache && (metric === 'tokens' || metric === 'cost');
    // One segment's value for a bucket, cache-aware: excluding the cache drops
    // the segment's cache portion, exactly as the drawn segments do.
    //
    // A bucket with no such segment is MISSING, not zero. Returning 0 would
    // draw a zero-height bar and the tooltip would read "$0.00" — which
    // says the bucket cost nothing rather than that it is not there. `null`
    // leaves the stack alone and the tooltip says "—".
    const segmentValue = (
      label: string,
      bar: (typeof bars)[number],
    ): number | null => {
      const seg = bar.segments.find((s) => s.label === label);
      if (!seg) return null;
      return dropCache ? seg.value - (seg.value_cache ?? 0) : seg.value;
    };
    const series = barCap.kept.map((label, i) => ({
      name: label,
      type: 'bar' as const,
      stack: 'total',
      barMaxWidth: 18,
      itemStyle: { color: t.series[i], borderRadius: [0, 0, 0, 0] },
      data: bars.map((b) => segmentValue(label, b)),
    }));
    if (barCap.overflow.length > 0) {
      // The labels past the palette size share one segment: it sums their
      // values per bucket, cache exclusion included, and the legend says how
      // many series it stands for.
      series.push({
        name: otherSeriesLabel(barCap.hiddenCount),
        type: 'bar' as const,
        stack: 'total',
        barMaxWidth: 18,
        // Grey, not a seventh categorical colour: this is the remainder.
        itemStyle: { color: t.fgMuted, borderRadius: [0, 0, 0, 0] },
        data: bars.map((b) => {
          const values = barCap.overflow
            .map((label) => segmentValue(label, b))
            .filter((value): value is number => value !== null);
          // No overflow model in this bucket: the remainder is missing here,
          // not zero.
          return values.length === 0 ? null : values.reduce((sum, value) => sum + value, 0);
        }),
      });
    }
    return {
      tooltip: {
        trigger: 'axis' as const,
        ...baseTooltip(t),
        // formatCost, not a hand-rolled `$${v.toFixed(2)}`: a bucket with no
        // data is missing, not zero, and the shared helper says so. The
        // percent branch above already distinguishes the two.
        valueFormatter: (v: number | null) =>
          metric === 'cost' ? formatCost(v) : formatTokens(v),
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
          // A hand-rolled `$${v}` prints the raw number and drops the cents;
          // formatAxisCost is the compact, locale-pinned tick.
          formatter: (v: number) => (metric === 'cost' ? formatAxisCost(v) : formatTokens(v)),
        },
      },
      series,
    };
  }, [data, metric, t, excludeCache, percentCap, barCap]);

  // The percent mode cannot aggregate: a fill curve is a percentage of a
  // limit, and summing several of them into one curve would draw a shape
  // that no series has. It caps instead, and says how many series are out of
  // the picture. The bar mode aggregates, and its legend carries the count.
  const hiddenSeries = metric === 'percent' ? percentCap.hiddenCount : 0;

  return (
    <>
      <EChart option={option} className={className} />
      {hiddenSeries > 0 ? (
        <p className="mt-1 text-[11px] text-fg-subtle">+{hiddenSeries} more series not shown</p>
      ) : null}
    </>
  );
}
