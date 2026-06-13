import { renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import { baseAxisStyle, baseTooltip, readChartTokens, useChartTokens } from './theme';

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
