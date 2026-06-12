// Bridge between the CSS design tokens and ECharts options. Chart builders
// call useChartTokens() so every chart re-themes when [data-theme] flips,
// without duplicating palette values outside tokens.css.

import { useMemo } from 'react';
import { useTheme } from '@/hooks/useTheme';

export interface ChartTokens {
  series: string[];
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
    series: ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6'].map(
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
