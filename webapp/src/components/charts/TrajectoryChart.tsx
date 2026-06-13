// Forecast trajectory: observed pct-used series + projected glide path to
// the window reset, with the 100% limit marked.

import { useMemo } from 'react';
import type { ForecastEntry } from '@/api/types';
import { formatLocalDateTime } from '@/lib/tz';
import { EChart } from './EChart';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

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
      grid: { left: 40, right: 16, top: 16, bottom: 28 },
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
            label: {
              formatter: 'limit',
              color: t.critical,
              fontSize: 10,
              fontFamily: t.fontFamily,
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
