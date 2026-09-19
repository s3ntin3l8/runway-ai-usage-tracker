// Generic horizontal ranking bar — one bar per row, largest at the top.
// Supports simple single-value bars AND stacked bars with breakdown data.
// Shared by Top Projects and Top Tools cards.

import { useMemo } from 'react';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

export interface RankSegment {
  key: string;
  label: string;
  color: string;
  value: number;
}

export interface RankRow {
  label: string;
  value: number;
  sub?: string; // optional dim line in the tooltip
  segments?: RankSegment[]; // when present, renders stacked bars
}

export function RankBar({
  rows,
  format,
  className,
}: {
  rows: RankRow[];
  format: (v: number) => string;
  className?: string;
}) {
  const t = useChartTokens();
  const hasStacked = rows.some((r) => r.segments && r.segments.length > 0);

  const option = useMemo(() => {
    // ECharts category axis draws bottom-up; sort ascending so the largest
    // lands at the top via the natural order.
    const sorted = [...rows].filter((r) => r.value > 0).sort((a, b) => a.value - b.value);
    const categoryData = sorted.map((r) => r.label);

    if (!hasStacked) {
      // Simple single-value bars (legacy path).
      return {
        grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
        tooltip: {
          ...baseTooltip(t),
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          formatter: (params: { name: string; value: number; dataIndex: number }[]) => {
            const p = params[0];
            const sub = sorted[p.dataIndex]?.sub;
            const subLine = sub ? `<br/><span style="opacity:.6">${sub}</span>` : '';
            return `${p.name}: ${format(p.value)}${subLine}`;
          },
        },
        xAxis: {
          type: 'value',
          ...baseAxisStyle(t),
          axisLabel: { ...baseAxisStyle(t).axisLabel, formatter: (v: number) => format(v) },
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
        series: [
          {
            type: 'bar',
            data: sorted.map((r) => r.value),
            barMaxWidth: 18,
            itemStyle: { color: t.accent, borderRadius: [0, 3, 3, 0] },
            emphasis: { itemStyle: { color: t.series[0] } },
          },
        ],
      };
    }

    // Stacked bars: collect all unique segment keys across rows.
    const segKeys = new Map<string, { label: string; color: string }>();
    for (const r of sorted) {
      for (const s of r.segments ?? []) {
        if (!segKeys.has(s.key)) segKeys.set(s.key, { label: s.label, color: s.color });
      }
    }
    const segmentDefs = Array.from(segKeys.entries()).map(([key, meta]) => ({ key, ...meta }));

    const series = segmentDefs.map((seg, i) => ({
      name: seg.label,
      type: 'bar' as const,
      stack: 'total',
      barMaxWidth: 18,
      data: sorted.map((r) => {
        const s = r.segments?.find((x) => x.key === seg.key);
        return s?.value ?? 0;
      }),
      itemStyle: {
        color: seg.color,
        borderRadius: i === segmentDefs.length - 1 ? [0, 3, 3, 0] : 0,
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
          const r = sorted[idx];
          if (!r) return '';

          const lines: string[] = [`<b>${r.label}</b>`];
          for (const p of params) {
            const val = p.value as number;
            if (val > 0) {
              lines.push(`${p.seriesName}: ${format(val)}`);
            }
          }
          lines.push(`<span style="opacity:.6">Total: ${format(r.value)}</span>`);
          if (r.sub) {
            lines.push(`<span style="opacity:.6">${r.sub}</span>`);
          }
          return lines.join('<br/>');
        },
      },
      xAxis: {
        type: 'value',
        ...baseAxisStyle(t),
        axisLabel: { ...baseAxisStyle(t).axisLabel, formatter: (v: number) => format(v) },
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
  }, [rows, format, t, hasStacked]);

  return <EChart option={option} className={className ?? 'h-72'} />;
}
