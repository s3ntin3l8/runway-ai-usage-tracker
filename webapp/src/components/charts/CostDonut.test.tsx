import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { CumulativeModelBucket } from '@/api/types';
import { formatCost } from '@/lib/format';

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

  it('shows calculated non-cache value when excludeCache is set', () => {
    const data: Record<string, CumulativeModelBucket> = {
      opus: { cost_usd: 10, cost_input: 1, cost_output: 2, cost_cache: 7 },
      sonnet: { cost_usd: 4, cost_input: 2, cost_output: 1, cost_cache: 1 },
    };
    render(<CostDonut data={data} excludeCache />, { wrapper });
    const series = captured.option!.series as Series;
    // Component estimates remain usable even when the total is reported.
    expect(series[0].data).toEqual([
      { name: 'opus', value: 3 },
      { name: 'sonnet', value: 3 },
    ]);
  });

  it('labels the chart and exposes every cost as text', () => {
    const data: Record<string, CumulativeModelBucket> = {
      big: { cost_usd: 12 },
      small: { cost_usd: 1.5 },
    };
    const { getByTestId, getByText } = render(<CostDonut data={data} />, { wrapper });
    expect(getByTestId('echart')).toHaveAttribute('aria-label', 'Cost by model chart');
    expect(getByText(`big: ${formatCost(12)}`)).toBeInTheDocument();
    expect(getByText(`small: ${formatCost(1.5)}`)).toBeInTheDocument();
  });
});

describe('CostDonut series cap', () => {
  // Eight entries: the palette has six colours, so the last two share one
  // "Other (2)" slice.
  const data: Record<string, CumulativeModelBucket> = {
    m1: { cost_usd: 8 },
    m2: { cost_usd: 7 },
    m3: { cost_usd: 6 },
    m4: { cost_usd: 5 },
    m5: { cost_usd: 4 },
    m6: { cost_usd: 3 },
    m7: { cost_usd: 2 },
    m8: { cost_usd: 1 },
  };

  it('aggregates the entries past the sixth into one slice', () => {
    render(<CostDonut data={data} />, { wrapper });
    const series = captured.option!.series as Series;
    const slices = series[0].data;
    expect(slices).toHaveLength(7);
    expect(slices.slice(0, 6).map((d) => d.name)).toEqual([
      'm1',
      'm2',
      'm3',
      'm4',
      'm5',
      'm6',
    ]);
    expect(slices[6]).toEqual({ name: 'Other (2)', value: 3 });
  });

  it('keeps the centre total the true total of every entry', () => {
    render(<CostDonut data={data} />, { wrapper });
    const series = captured.option!.series as {
      label: { formatter: () => string };
    }[];
    // 8 + 7 + … + 1 — the two aggregated entries are in the total.
    expect(series[0].label.formatter()).toBe(formatCost(36));
  });

  it('summarises only the slices the chart draws', () => {
    const { getByText, queryByText } = render(<CostDonut data={data} />, { wrapper });
    expect(getByText(`Other (2): ${formatCost(3)}`)).toBeInTheDocument();
    expect(queryByText(`m7: ${formatCost(2)}`)).not.toBeInTheDocument();
    expect(queryByText(`m8: ${formatCost(1)}`)).not.toBeInTheDocument();
  });
});

describe('modelCost', () => {
  it('returns the full cost when not excluding cache', () => {
    expect(modelCost({ cost_usd: 5, cost_cache: 2 }, false)).toBe(5);
  });

  it('uses token-priced non-cache components when excluding', () => {
    expect(modelCost({ cost_usd: 5, cost_input: 2, cost_output: 1, cost_cache: 2 }, true)).toBe(3);
  });

  it('returns zero when no non-cache components have been priced', () => {
    expect(modelCost({ cost_usd: 1, cost_cache: 4 }, true)).toBe(0);
  });
});
