// Forecast trajectory: observed pct-used series + projected glide path to
// the window reset, with the 100% limit marked.

import { useMemo } from 'react';
import type { ForecastEntry } from '@/api/types';
import { formatLocalDateTime } from '@/lib/tz';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
import { baseAxisStyle, baseTooltip, useChartTokens } from './theme';

// The 'limit' label sits above the 100% line inside the plot, so the top grid
// margin is derived from it: glyph + 1px padding each side + the label's
// distance from the line + headroom.
const LIMIT_LABEL_FONT_SIZE = 10;
const LIMIT_LABEL_PADDING = 1;
const LIMIT_LABEL_DISTANCE = 5;
const GRID_TOP_HEADROOM = 3;
export const GRID_TOP =
  LIMIT_LABEL_FONT_SIZE + 2 * LIMIT_LABEL_PADDING + LIMIT_LABEL_DISTANCE + GRID_TOP_HEADROOM;

// The bottom margin has to fit the x-axis labels AND the legend strip that
// sits under them.
const GRID_BOTTOM = 48;

function formatPoint(ts: number): string {
  return formatLocalDateTime(new Date(ts).toISOString(), {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function TrajectoryChart({
  forecast,
  className,
}: {
  forecast: ForecastEntry;
  className?: string;
}) {
  const t = useChartTokens();
  const observed = useMemo(
    () => (forecast.series ?? []).map((p) => [Date.parse(p.ts), p.pct] as [number, number]),
    [forecast.series],
  );
  const projection = useMemo<[number, number][]>(() => {
    const last = observed[observed.length - 1];
    if (!last || !forecast.reset_at || forecast.projected_pct == null) return [];
    return [
      [last[0], last[1]],
      [Date.parse(forecast.reset_at), forecast.projected_pct],
    ];
  }, [observed, forecast.reset_at, forecast.projected_pct]);
  const option = useMemo(() => {
    const danger = forecast.status === 'risk' || forecast.status === 'exhausted';
    return {
      tooltip: {
        trigger: 'axis',
        ...baseTooltip(t),
        valueFormatter: (v: number) => `${Number(v).toFixed(1)}%`,
      },
      // The two series are only distinguishable by their line style (solid
      // vs dashed), so the legend has to name them — there is no other cue
      // on the chart itself.
      legend: {
        bottom: 0,
        itemWidth: 12,
        itemHeight: 8,
        textStyle: { color: t.fgMuted, fontSize: 11, fontFamily: t.fontFamily },
      },
      grid: { left: 40, right: 16, top: GRID_TOP, bottom: GRID_BOTTOM },
      xAxis: {
        type: 'time',
        ...baseAxisStyle(t),
        splitLine: { show: false },
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          formatter: (value: number) => formatPoint(value),
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
              distance: LIMIT_LABEL_DISTANCE,
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
  }, [observed, projection, forecast.status, t]);

  return (
    <>
      <EChart option={option} className={className ?? 'h-56'} ariaLabel="Quota usage trajectory chart" />
      {/* The observed and projected percentages live only in the canvas and
          the hover tooltip, so they need a text alternative. */}
      <AccessibleSummary
        caption="Quota usage trajectory"
        entries={[
          ...observed.map(([ts, pct]) => ({ label: formatPoint(ts), value: `${pct}%` })),
          // The projection repeats the last observed point as its origin;
          // only the endpoint is new information.
          ...projection.slice(1).map(([ts, pct]) => ({
            label: formatPoint(ts),
            value: `${pct}%`,
            note: 'projected',
          })),
        ]}
      />
    </>
  );
}
