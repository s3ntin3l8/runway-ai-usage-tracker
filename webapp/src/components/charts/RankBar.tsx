// Generic horizontal ranking bar — one bar per row, largest at the top. Shared
// by the Top Projects and Top Tools cards (Top Models predates it and keeps its
// own variant). Pass pre-sorted rows; `format` renders the value + tooltip.

import { useMemo } from 'react';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

export interface RankRow {
  label: string;
  value: number;
  sub?: string; // optional dim line in the tooltip (e.g. "via anthropic, gemini")
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

  const option = useMemo(() => {
    // ECharts category axis draws bottom-up; sort ascending so the largest
    // lands at the top via the natural order.
    const sorted = [...rows].filter((r) => r.value > 0).sort((a, b) => a.value - b.value);
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
        data: sorted.map((r) => r.label),
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
  }, [rows, format, t]);

  return <EChart option={option} className={className ?? 'h-72'} />;
}
