// Hour-of-day × day-of-week token heatmap (7×24 grid from /usage/heatmap).
// dow follows the SQLite convention: 0=Sunday … 6=Saturday.

import { useMemo } from 'react';
import type { HeatmapCell } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { EChart } from './EChart';
import { baseTooltip, useChartTokens } from './theme';

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export function UsageHeatmap({ cells, className }: { cells: HeatmapCell[]; className?: string }) {
  const t = useChartTokens();
  const option = useMemo(() => {
    // Token volume per hour is heavily skewed (one busy hour dwarfs the rest),
    // which flattens a linear color scale. Drive the color by sqrt(tokens) for
    // a perceptual spread, but keep the true count (index 3) for tooltip/legend.
    const data = cells.map((c) => [c.hour, c.dow, Math.sqrt(c.tokens), c.tokens]);
    const max = Math.sqrt(Math.max(1, ...cells.map((c) => c.tokens)));
    return {
      tooltip: {
        ...baseTooltip(t),
        formatter: (p: { value: [number, number, number, number] }) =>
          `${DAYS[p.value[1]]} ${String(p.value[0]).padStart(2, '0')}:00 — ${formatTokens(p.value[3])} tokens`,
      },
      grid: { left: 44, right: 12, top: 12, bottom: 64 },
      xAxis: {
        type: 'category',
        data: Array.from({ length: 24 }, (_, h) => String(h)),
        name: 'Hour of day',
        nameLocation: 'middle',
        nameGap: 26,
        nameTextStyle: { color: t.axis, fontSize: 10, fontFamily: t.fontFamily },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          interval: 2,
        },
        splitArea: { show: false },
      },
      yAxis: {
        type: 'category',
        data: DAYS,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: t.axis, fontSize: 10, fontFamily: t.fontFamily },
      },
      visualMap: {
        // Map on the sqrt-scaled value (index 2), NOT the raw count we stash at
        // index 3 for the tooltip — otherwise ECharts defaults to the last
        // dimension and every active cell clamps to max (uniform color).
        dimension: 2,
        min: 0,
        max,
        calculable: false,
        orient: 'horizontal',
        left: 'center',
        bottom: 2,
        itemHeight: 120,
        textStyle: { color: t.axis, fontSize: 10, fontFamily: t.fontFamily },
        // Legend maps the sqrt-scaled domain, so square the ticks back to the
        // real token count.
        formatter: (v: number) => formatTokens(v * v),
        // Sequential ramp from design tokens — light→dark in light mode,
        // dark→bright in dark mode, so intensity reads on either surface.
        inRange: { color: t.heat },
      },
      series: [
        {
          type: 'heatmap',
          data,
          itemStyle: { borderColor: t.surface, borderWidth: 1.5, borderRadius: 2 },
          emphasis: { itemStyle: { borderColor: t.fg } },
        },
      ],
    };
  }, [cells, t]);

  return <EChart option={option} className={className ?? 'h-72'} />;
}
