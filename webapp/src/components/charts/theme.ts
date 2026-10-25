// Bridge between the CSS design tokens and ECharts options. Chart builders
// call useChartTokens() so every chart re-themes when [data-theme] flips,
// without duplicating palette values outside tokens.css.

import { useMemo } from 'react';
import { useTheme } from '@/hooks/useTheme';

// The categorical palette. Its size is the ceiling on how many series a chart
// may colour: past it a colour would have to repeat, and two series that look
// identical read as one.
const SERIES_TOKENS = [
  '--chart-1',
  '--chart-2',
  '--chart-3',
  '--chart-4',
  '--chart-5',
  '--chart-6',
];

/** How many series a chart may give a colour of their own. */
export const MAX_CHART_SERIES = SERIES_TOKENS.length;

/** The bucket that stands in for the series a chart could not draw. */
export const OTHER_SERIES_NAME = 'Other';

/** "Other (3)" — the aggregate says how many series it stands for. */
export function otherSeriesLabel(count: number): string {
  return `${OTHER_SERIES_NAME} (${count})`;
}

export interface ChartSeriesCap<T> {
  /** The series that keep a colour of their own. */
  kept: T[];
  /** The series that are aggregated into the "Other" bucket. */
  overflow: T[];
  /** How many series the "Other" bucket stands for. */
  hiddenCount: number;
}

/**
 * Split a series list into the ones that get their own colour and the ones
 * that have to be aggregated into an "Other" bucket. Charts call this instead
 * of wrapping the palette (`palette[i % palette.length]`), which handed the
 * seventh series the first series' colour.
 */
export function splitChartSeries<T>(
  series: T[],
  max: number = MAX_CHART_SERIES,
): ChartSeriesCap<T> {
  const kept = series.slice(0, max);
  const overflow = series.slice(max);
  return { kept, overflow, hiddenCount: overflow.length };
}

export interface ChartSlice {
  name: string;
  value: number;
}

export interface CappedSlices {
  /** The slices the chart draws: the kept ones plus the "Other" bucket. */
  slices: ChartSlice[];
  /** How many slices the "Other" bucket stands for. */
  hiddenCount: number;
  /** The sum of every slice, including the ones the bucket stands for. */
  total: number;
}

/**
 * Cap value slices (donut wedges) at the palette size: the first ones keep
 * their own colour, the rest are summed into a single "Other (N)" slice. The
 * total stays the sum of every slice, so a capped chart still reports the
 * real amount.
 */
export function capChartSlices(
  slices: ChartSlice[],
  max: number = MAX_CHART_SERIES,
): CappedSlices {
  const { kept, overflow, hiddenCount } = splitChartSeries(slices, max);
  const total = slices.reduce((sum, s) => sum + s.value, 0);
  if (overflow.length === 0) return { slices: kept, hiddenCount, total };
  return {
    slices: [
      ...kept,
      {
        name: otherSeriesLabel(overflow.length),
        value: overflow.reduce((sum, s) => sum + s.value, 0),
      },
    ],
    hiddenCount,
    total,
  };
}

export interface ChartTokens {
  series: string[];
  heat: string[];
  grid: string;
  axis: string;
  fg: string;
  fgMuted: string;
  surface: string;
  edge: string;
  accent: string;
  critical: string;
  warning: string;
  ok: string;
  fontFamily: string;
  monoFamily: string;
}

function cssVar(styles: CSSStyleDeclaration, name: string): string {
  return styles.getPropertyValue(name).trim();
}

export function readChartTokens(): ChartTokens {
  const styles = getComputedStyle(document.documentElement);
  return {
    series: SERIES_TOKENS.map((v) => cssVar(styles, v)),
    heat: ['--chart-heat-0', '--chart-heat-1', '--chart-heat-2', '--chart-heat-3', '--chart-heat-4'].map(
      (v) => cssVar(styles, v),
    ),
    grid: cssVar(styles, '--chart-grid'),
    axis: cssVar(styles, '--chart-axis'),
    fg: cssVar(styles, '--fg'),
    fgMuted: cssVar(styles, '--fg-muted'),
    surface: cssVar(styles, '--surface-1'),
    edge: cssVar(styles, '--edge'),
    accent: cssVar(styles, '--accent'),
    critical: cssVar(styles, '--critical'),
    warning: cssVar(styles, '--warning'),
    ok: cssVar(styles, '--ok'),
    fontFamily: "'Inter Variable', system-ui, sans-serif",
    monoFamily: "'JetBrains Mono Variable', monospace",
  };
}

export function useChartTokens(): ChartTokens {
  const { resolved } = useTheme();
  // resolved is the dependency that invalidates the memo on theme switch
  return useMemo(() => readChartTokens(), [resolved]);
}

// Shared option fragments builders can spread in.
export function baseTooltip(t: ChartTokens) {
  return {
    backgroundColor: t.surface,
    borderColor: t.edge,
    textStyle: { color: t.fg, fontSize: 12, fontFamily: t.fontFamily },
  };
}

export function baseAxisStyle(t: ChartTokens) {
  return {
    axisLine: { lineStyle: { color: t.grid } },
    axisTick: { show: false },
    axisLabel: { color: t.axis, fontSize: 11, fontFamily: t.fontFamily },
    splitLine: { lineStyle: { color: t.grid } },
  };
}
