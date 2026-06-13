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

import { ModelDonut } from './ModelDonut';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

type Series = { data: { name: string; value: number }[] }[];

describe('ModelDonut', () => {
  it('builds one slice per model, summed across token kinds, sorted desc and zeros dropped', () => {
    const byModel: Record<string, CumulativeModelBucket> = {
      small: { tokens_input: 10, tokens_output: 5 },
      big: { tokens_input: 100, tokens_cache_read: 50, tokens_reasoning: 50 },
      empty: { tokens_input: 0 },
    };
    const { getByTestId } = render(<ModelDonut byModel={byModel} />, { wrapper });
    expect(getByTestId('echart')).toBeInTheDocument();
    const series = captured.option!.series as Series;
    const data = series[0].data;
    expect(data).toEqual([
      { name: 'big', value: 200 },
      { name: 'small', value: 15 },
    ]);
  });

  it('produces an empty data set when there are no tokens', () => {
    render(<ModelDonut byModel={{ a: {} }} />, { wrapper });
    const series = captured.option!.series as Series;
    expect(series[0].data).toEqual([]);
  });
});
