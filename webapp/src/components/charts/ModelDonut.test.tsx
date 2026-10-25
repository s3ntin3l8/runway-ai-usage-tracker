import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { CumulativeModelBucket } from '@/api/types';
import { formatTokens } from '@/lib/format';

// Capture the option built by the chart instead of rendering real ECharts.
const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  // The mock forwards ariaLabel the way the real EChart does, so the tests
  // can assert that each chart hands its label down.
  EChart: ({
    option,
    ariaLabel,
  }: {
    option: Record<string, unknown>;
    ariaLabel?: string;
  }) => {
    captured.option = option;
    return (
      <div
        data-testid="echart"
        role={ariaLabel ? 'img' : undefined}
        aria-label={ariaLabel}
      />
    );
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

  it('labels the chart and exposes every model total as text', () => {
    const byModel: Record<string, CumulativeModelBucket> = {
      small: { tokens_input: 10, tokens_output: 5 },
      big: { tokens_input: 100, tokens_cache_read: 50, tokens_reasoning: 50 },
    };
    const { getByTestId, getByText } = render(<ModelDonut byModel={byModel} />, { wrapper });
    expect(getByTestId('echart')).toHaveAttribute('aria-label', 'Tokens by model chart');
    expect(getByText('big: 200')).toBeInTheDocument();
    expect(getByText('small: 15')).toBeInTheDocument();
  });
});

describe('ModelDonut series cap', () => {
  // Eight models: the palette has six colours, so the last two share one
  // "Other (2)" slice.
  const byModel: Record<string, CumulativeModelBucket> = {
    m1: { tokens_input: 80 },
    m2: { tokens_input: 70 },
    m3: { tokens_input: 60 },
    m4: { tokens_input: 50 },
    m5: { tokens_input: 40 },
    m6: { tokens_input: 30 },
    m7: { tokens_input: 20 },
    m8: { tokens_input: 10 },
  };

  it('aggregates the models past the sixth into one slice', () => {
    render(<ModelDonut byModel={byModel} />, { wrapper });
    const series = captured.option!.series as Series;
    const data = series[0].data;
    expect(data).toHaveLength(7);
    expect(data.slice(0, 6).map((d) => d.name)).toEqual([
      'm1',
      'm2',
      'm3',
      'm4',
      'm5',
      'm6',
    ]);
    expect(data[6]).toEqual({ name: 'Other (2)', value: 30 });
  });

  it('keeps the centre total the true total of every model', () => {
    render(<ModelDonut byModel={byModel} />, { wrapper });
    const series = captured.option!.series as {
      label: { formatter: () => string };
      data: { name: string; value: number }[];
    }[];
    // 80 + 70 + … + 10 — the two aggregated models are in the total.
    expect(series[0].label.formatter()).toBe(formatTokens(360));
  });

  it('summarises only the slices the chart draws', () => {
    const { getByText, queryByText } = render(<ModelDonut byModel={byModel} />, { wrapper });
    expect(getByText('Other (2): 30')).toBeInTheDocument();
    expect(queryByText('m7: 20')).not.toBeInTheDocument();
    expect(queryByText('m8: 10')).not.toBeInTheDocument();
  });
});
