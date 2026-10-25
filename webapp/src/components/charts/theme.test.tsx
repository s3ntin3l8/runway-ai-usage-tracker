import { renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import {
  baseAxisStyle,
  baseTooltip,
  capChartSlices,
  MAX_CHART_SERIES,
  otherSeriesLabel,
  readChartTokens,
  splitChartSeries,
  useChartTokens,
} from './theme';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

describe('readChartTokens', () => {
  it('returns the full token shape with the static font families', () => {
    const t = readChartTokens();
    expect(t.series).toHaveLength(6);
    expect(t.heat).toHaveLength(5);
    expect(t.fontFamily).toContain('Inter');
    expect(t.monoFamily).toContain('JetBrains Mono');
    // jsdom resolves CSS vars to '' but the keys must exist.
    expect(t).toHaveProperty('accent');
    expect(t).toHaveProperty('critical');
  });
});

describe('useChartTokens', () => {
  it('returns chart tokens inside a ThemeProvider', () => {
    const { result } = renderHook(() => useChartTokens(), { wrapper });
    expect(result.current.series).toHaveLength(6);
    expect(typeof result.current.fontFamily).toBe('string');
  });
});

describe('option fragment builders', () => {
  it('baseTooltip pulls surface/edge/fg from the tokens', () => {
    const t = readChartTokens();
    const tip = baseTooltip(t);
    expect(tip.backgroundColor).toBe(t.surface);
    expect(tip.borderColor).toBe(t.edge);
    expect(tip.textStyle.color).toBe(t.fg);
  });

  it('baseAxisStyle wires grid and axis colors', () => {
    const t = readChartTokens();
    const axis = baseAxisStyle(t);
    expect(axis.axisLine.lineStyle.color).toBe(t.grid);
    expect(axis.axisLabel.color).toBe(t.axis);
    expect(axis.axisTick.show).toBe(false);
  });
});

describe('the series cap', () => {
  it('is the size of the categorical palette', () => {
    expect(MAX_CHART_SERIES).toBe(6);
    expect(readChartTokens().series).toHaveLength(MAX_CHART_SERIES);
  });

  it('splits a long list into the series that keep a colour and the overflow', () => {
    const { kept, overflow, hiddenCount } = splitChartSeries([
      'a',
      'b',
      'c',
      'd',
      'e',
      'f',
      'g',
      'h',
    ]);
    expect(kept).toEqual(['a', 'b', 'c', 'd', 'e', 'f']);
    expect(overflow).toEqual(['g', 'h']);
    expect(hiddenCount).toBe(2);
  });

  it('keeps every series when the list fits the palette', () => {
    const { kept, overflow, hiddenCount } = splitChartSeries(['a', 'b']);
    expect(kept).toEqual(['a', 'b']);
    expect(overflow).toEqual([]);
    expect(hiddenCount).toBe(0);
  });

  it('labels the aggregate with the number of series it stands for', () => {
    expect(otherSeriesLabel(3)).toBe('Other (3)');
  });
});

describe('capChartSlices', () => {
  const slices = [
    { name: 'a', value: 60 },
    { name: 'b', value: 50 },
    { name: 'c', value: 40 },
    { name: 'd', value: 30 },
    { name: 'e', value: 20 },
    { name: 'f', value: 10 },
    { name: 'g', value: 8 },
    { name: 'h', value: 2 },
  ];

  it('leaves a list that fits the palette alone', () => {
    const capped = capChartSlices(slices.slice(0, 3));
    expect(capped.slices).toEqual(slices.slice(0, 3));
    expect(capped.hiddenCount).toBe(0);
    expect(capped.total).toBe(150);
  });

  it('sums the overflow into one "Other (N)" slice', () => {
    const capped = capChartSlices(slices);
    expect(capped.slices).toHaveLength(MAX_CHART_SERIES + 1);
    expect(capped.slices[6]).toEqual({ name: 'Other (2)', value: 10 });
    expect(capped.hiddenCount).toBe(2);
  });

  it('keeps the total of every slice, not just the drawn ones', () => {
    const capped = capChartSlices(slices);
    expect(capped.total).toBe(220);
    expect(capped.slices.reduce((sum, s) => sum + s.value, 0)).toBe(capped.total);
  });
});
