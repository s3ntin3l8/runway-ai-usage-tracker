// Hour-of-day × day-of-week token heatmap (7×24 grid from /usage/heatmap).
// dow follows the SQLite convention: 0=Sunday … 6=Saturday.

import { useMemo } from 'react';
import type { HeatmapCell } from '@/api/types';
import { formatTokens } from '@/lib/format';
import { useIsDesktop } from '@/hooks/useMediaQuery';
import { EChart } from './EChart';
import { AccessibleSummary } from './accessibleSummary';
import { baseTooltip, useChartTokens } from './theme';

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
// Desktop x-axis labels. Only every other one is drawn (see `interval: 2`).
const HOURS = Array.from({ length: 24 }, (_, h) => String(h));
// Phone y-axis labels: padded so the hours line up as a column.
const PADDED_HOURS = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0'));

export function UsageHeatmap({ cells, className }: { cells: HeatmapCell[]; className?: string }) {
  const t = useChartTokens();
  // Below Tailwind's lg breakpoint the 24 hour columns squeeze into ~13px
  // each on a 390px phone, so the grid is transposed: the 7 days run across
  // the top and the 24 hours run down the side, which gives every cell
  // ~40px of width instead of ~13px.
  const isDesktop = useIsDesktop();
  const option = useMemo(() => {
    // Token volume per hour is heavily skewed (one busy hour dwarfs the rest),
    // which flattens a linear color scale. Drive the color by sqrt(tokens) for
    // a perceptual spread, but keep the true count (index 3) for tooltip/legend.
    //
    // The tuple follows the axis order: [hour, dow, …] on desktop,
    // [dow, hour, …] on a phone.
    const data = cells.map((c) =>
      isDesktop
        ? [c.hour, c.dow, Math.sqrt(c.tokens), c.tokens]
        : [c.dow, c.hour, Math.sqrt(c.tokens), c.tokens],
    );
    const max = Math.sqrt(Math.max(1, ...cells.map((c) => c.tokens)));
    return {
      tooltip: {
        ...baseTooltip(t),
        formatter: (p: { value: [number, number, number, number] }) => {
          // Read the two dimensions in the order the current layout stores
          // them, so both layouts spell out the same "Wed 14:00" text.
          const [first, second] = p.value;
          const [dow, hour] = isDesktop ? [second, first] : [first, second];
          return `${DAYS[dow]} ${String(hour).padStart(2, '0')}:00 — ${formatTokens(p.value[3])} tokens`;
        },
      },
      // The phone layout moves the labels to the left edge, so the grid needs
      // less room there ("00"…"23" are narrower than "Hour of day" below).
      grid: isDesktop
        ? { left: 44, right: 12, top: 12, bottom: 84 }
        : { left: 38, right: 12, top: 12, bottom: 84 },
      xAxis: {
        type: 'category',
        data: isDesktop ? HOURS : DAYS,
        name: isDesktop ? 'Hour of day' : 'Day of week',
        nameLocation: 'middle',
        nameGap: 26,
        nameTextStyle: { color: t.axis, fontSize: 10, fontFamily: t.fontFamily },
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          // Every other hour on desktop (they would collide); all 7 days on a
          // phone (they fit comfortably across the top).
          interval: isDesktop ? 2 : 0,
        },
        splitArea: { show: false },
      },
      yAxis: {
        type: 'category',
        data: isDesktop ? DAYS : PADDED_HOURS,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: {
          color: t.axis,
          fontSize: 10,
          fontFamily: t.fontFamily,
          // All 24 hours are labelled on a phone — they are the axis now.
          ...(isDesktop ? {} : { interval: 0 }),
        },
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
        left: 8,
        bottom: 0,
        itemHeight: 180,
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
  }, [cells, t, isDesktop]);

  return (
    <>
      {/* The ramp runs dark→bright in the dark theme and light→dark in the
          light one, so the caption names the encoding rather than a
          direction. Without it the scale bar is a row of numbers that
          says nothing about what it scales. */}
      <p className="mb-1 text-[10px] text-fg-subtle">
        Shade shows token volume — the stronger the colour, the more tokens.
      </p>
      {/* 24 stacked hour rows need more than the desktop canvas height; a
          caller-provided className always wins over the default. */}
      <EChart
        option={option}
        className={className ?? (isDesktop ? 'h-80' : 'h-[420px]')}
        ariaLabel="Activity by hour chart"
      />
      {/* The heatmap is pure pixels — the colour ramp says "how busy", not
          "how many" — so every cell needs its count spelled out as text. */}
      <AccessibleSummary
        caption="Token usage by day of week and hour"
        entries={cells.map((c) => ({
          label: `${DAYS[c.dow]} ${String(c.hour).padStart(2, '0')}:00`,
          value: formatTokens(c.tokens),
        }))}
      />
    </>
  );
}
