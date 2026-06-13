import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { HeatmapCell } from '@/api/types';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
  },
}));

import { UsageHeatmap } from './UsageHeatmap';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

describe('UsageHeatmap', () => {
  it('maps cells to [hour, dow, sqrt(tokens), tokens] tuples and a sqrt-scaled visualMap max', () => {
    const cells: HeatmapCell[] = [
      { hour: 9, dow: 1, tokens: 100 },
      { hour: 14, dow: 3, tokens: 4 },
    ];
    render(<UsageHeatmap cells={cells} />, { wrapper });
    const opt = captured.option!;
    const series = opt.series as { data: number[][] }[];
    expect(series[0].data).toEqual([
      [9, 1, 10, 100],
      [14, 3, 2, 4],
    ]);
    const visualMap = opt.visualMap as { max: number; dimension: number };
    expect(visualMap.dimension).toBe(2);
    expect(visualMap.max).toBe(10); // sqrt(100)
  });

  it('clamps the visualMap max to sqrt(1) for an empty grid', () => {
    render(<UsageHeatmap cells={[]} />, { wrapper });
    const visualMap = captured.option!.visualMap as { max: number };
    expect(visualMap.max).toBe(1);
  });
});
