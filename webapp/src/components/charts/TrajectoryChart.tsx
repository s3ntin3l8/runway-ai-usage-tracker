// Forecast trajectory: observed pct-used series + projected glide path to
// the window reset, with the 100% limit marked.

import { useMemo } from 'react';
import type { ForecastEntry } from '@/api/types';
import { formatLocalDateTime } from '@/lib/tz';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

// The 'limit' label sits above the 100% line inside the plot, so the top grid
// margin is derived from it: glyph + 1px padding each side + ECharts' default
// 5px label distance + headroom.
const LIMIT_LABEL_FONT_SIZE = 10;
const LIMIT_LABEL_PADDING = 1;
const GRID_TOP = LIMIT_LABEL_FONT_SIZE + 2 * LIMIT_LABEL_PADDING + 5 + 3;

export function TrajectoryChart({
  forecast,
  className,
}: {
  forecast: ForecastEntry;
  className?: string;
}) {
  const t = useChartTokens();
  const option = useMemo(() => {
    const observed = (forecast.series ?? []).map((p) => [Date.parse(p.ts), p.pct]);
    const last = observed[observed.length - 1];
    const projection: [number, number][] = [];
    if (last && forecast.reset_at && forecast.projected_pct != null) {
      projection.push([last[0], last[1]] as [number, number]);
      projection.push([Date.parse(forecast.reset_at), forecast.projected_pct]);
    }
    const danger = forecast.status === 'risk' || forecast.status === 'exhausted';
    return {
      tooltip: {
        trigger: 'axis',
        ...baseTooltip(t),
        valueFormatter: (v: number) => `${Number(v).toFixed(1)}%`,
      },
      grid: { left: 40, right: 16, top: GRID_TOP, bottom: 28 },
      xAxis: {
        type: 'time',
        ...baseAxisStyle(t),
        splitLine: { show: false },
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          formatter: (value: number) =>
            formatLocalDateTime(new Date(value).toISOString(), {
              weekday: 'short',
              hour: '2-digit',
              minute: '2-digit',
            }),
        },
      },
      yAxis: {
        type: 'value',
        max: (extent: { max: number }) => Math.max(100, Math.ceil(extent.max / 10) * 10),
        ...baseAxisStyle(t),
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          formatter: '{value}%',
        },
      },
      series: [
        {
          name: 'Used',
          type: 'line',
          showSymbol: false,
          data: observed,
          lineStyle: { color: t.accent, width: 2 },
          itemStyle: { color: t.accent },
          areaStyle: { color: t.accent, opacity: 0.08 },
          markLine: {
            symbol: 'none',
            silent: true,
            // Default 'end' places the label past the line's right end,
            // outside the plot, where the narrow grid margin clips it.
            label: {
              formatter: 'limit',
              position: 'insideEndTop',
              color: t.critical,
              fontSize: LIMIT_LABEL_FONT_SIZE,
              fontFamily: t.fontFamily,
              // Inside the plot the projection can cross the label (y-max
              // above 100); a surface backdrop keeps the text clean.
              backgroundColor: t.surface,
              padding: LIMIT_LABEL_PADDING,
            },
            lineStyle: { color: t.critical, type: 'dashed', opacity: 0.6 },
            data: [{ yAxis: 100 }],
          },
        },
        {
          name: 'Projected',
          type: 'line',
          showSymbol: false,
          data: projection,
          lineStyle: {
            color: danger ? t.critical : t.warning,
            width: 2,
            type: 'dashed',
          },
          itemStyle: { color: danger ? t.critical : t.warning },
        },
      ],
    };
  }, [forecast, t]);

  return <EChart option={option} className={className ?? 'h-56'} />;
}
