import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { CumulativeModelBucket } from '@/api/types';

// Capture the option built by the chart instead of rendering real ECharts.
const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
  },
}));

import { CostDonut, modelCost } from './CostDonut';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

type Series = { data: { name: string; value: number }[] }[];

describe('CostDonut', () => {
  it('builds one slice per key sized by cost_usd, sorted desc and zeros dropped', () => {
    const data: Record<string, CumulativeModelBucket> = {
      small: { cost_usd: 1.5, tokens_input: 999 },
      big: { cost_usd: 12 },
      free: { cost_usd: 0, tokens_input: 100 },
    };
    const { getByTestId } = render(<CostDonut data={data} />, { wrapper });
    expect(getByTestId('echart')).toBeInTheDocument();
    const series = captured.option!.series as Series;
    expect(series[0].data).toEqual([
      { name: 'big', value: 12 },
      { name: 'small', value: 1.5 },
    ]);
  });

  it('produces an empty data set when there is no cost', () => {
    render(<CostDonut data={{ a: { tokens_input: 100 } }} />, { wrapper });
    const series = captured.option!.series as Series;
    expect(series[0].data).toEqual([]);
  });

  it('subtracts cost_cache from slices when excludeCache is set', () => {
    const data: Record<string, CumulativeModelBucket> = {
      opus: { cost_usd: 10, cost_cache: 7 },
      sonnet: { cost_usd: 4, cost_cache: 1 },
    };
    render(<CostDonut data={data} excludeCache />, { wrapper });
    const series = captured.option!.series as Series;
    // opus 10−7=3, sonnet 4−1=3 (sort is stable for ties → insertion order).
    expect(series[0].data).toEqual([
      { name: 'opus', value: 3 },
      { name: 'sonnet', value: 3 },
    ]);
  });
});

describe('modelCost', () => {
  it('returns the full cost when not excluding cache', () => {
    expect(modelCost({ cost_usd: 5, cost_cache: 2 }, false)).toBe(5);
  });

  it('drops the cache portion when excluding', () => {
    expect(modelCost({ cost_usd: 5, cost_cache: 2 }, true)).toBe(3);
  });

  it('clamps at 0 when an estimated cache cost exceeds the total', () => {
    expect(modelCost({ cost_usd: 1, cost_cache: 4 }, true)).toBe(0);
  });
});
