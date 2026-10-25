import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { TopModelEntry } from '@/api/types';

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

import { TopModelsBar } from './TopModelsBar';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

const mockModel: TopModelEntry = {
  model_id: 'claude-sonnet',
  msgs: 10,
  tokens_total: 15000,
  tokens_input: 10000,
  tokens_output: 5000,
  tokens_cache_read: 0,
  tokens_cache_create: 0,
  tokens_reasoning: 0,
  cost_usd: 0.15,
  cost_cache: 0,
  cost_input: 0.10,
  cost_output: 0.05,
  cost_cache_read: 0,
  cost_cache_create: 0,
  providers: ['anthropic'],
};

describe('TopModelsBar', () => {
  afterEach(() => {
    captured.option = undefined;
  });

  it('renders stacked segments for tokens metric', () => {
    render(
      <TopModelsBar models={[mockModel]} metric="tokens" />,
      { wrapper },
    );
    const series = captured.option!.series as { name: string; data: number[] }[];
    expect(series).toHaveLength(5);
    expect(series.map((s) => s.name)).toEqual([
      'Input',
      'Output',
      'Reasoning',
      'Cache read',
      'Cache create',
    ]);
    expect(series.map((s) => s.data)).toEqual([
      [10000],
      [5000],
      [0],
      [0],
      [0],
    ]);
  });

  it('renders stacked segments for cost metric', () => {
    render(
      <TopModelsBar models={[mockModel]} metric="cost" />,
      { wrapper },
    );
    const series = captured.option!.series as { name: string; data: number[] }[];
    expect(series).toHaveLength(4);
    expect(series.map((s) => s.name)).toEqual([
      'Input',
      'Output',
      'Cache read',
      'Cache create',
    ]);
    expect(series.map((s) => s.data)).toEqual([
      [0.10],
      [0.05],
      [0],
      [0],
    ]);
  });

  it('filters zero-value models', () => {
    const zeroModel: TopModelEntry = {
      ...mockModel,
      model_id: 'empty-model',
      tokens_input: 0,
      tokens_output: 0,
      tokens_reasoning: 0,
      tokens_cache_read: 0,
      tokens_cache_create: 0,
      cost_input: 0,
      cost_output: 0,
      cost_cache_read: 0,
      cost_cache_create: 0,
    };
    render(
      <TopModelsBar models={[mockModel, zeroModel]} metric="tokens" />,
      { wrapper },
    );
    const categoryData = (captured.option!.yAxis as { data: string[] }).data;
    expect(categoryData).toEqual(['claude-sonnet']);
    expect(categoryData).not.toContain('empty-model');
  });

  it('excludeCache removes cache segments for tokens', () => {
    render(
      <TopModelsBar
        models={[mockModel]}
        metric="tokens"
        excludeCache
      />,
      { wrapper },
    );
    const series = captured.option!.series as { name: string }[];
    expect(series).toHaveLength(3);
    expect(series.map((s) => s.name)).toEqual(['Input', 'Output', 'Reasoning']);
  });

  it('excludeCache removes cache segments for cost', () => {
    render(
      <TopModelsBar
        models={[mockModel]}
        metric="cost"
        excludeCache
      />,
      { wrapper },
    );
    const series = captured.option!.series as { name: string }[];
    expect(series).toHaveLength(2);
    expect(series.map((s) => s.name)).toEqual(['Input', 'Output']);
  });

  it('totalValue matches sum of segments for tokens metric', () => {
    const model: TopModelEntry = {
      ...mockModel,
      tokens_input: 8000,
      tokens_output: 4000,
      tokens_reasoning: 2000,
      tokens_cache_read: 1500,
      tokens_cache_create: 500,
    };
    render(
      <TopModelsBar models={[model]} metric="tokens" />,
      { wrapper },
    );
    const series = captured.option!.series as { data: number[] }[];
    const segmentSum = series.reduce((acc, s) => acc + s.data[0], 0);
    expect(segmentSum).toBe(8000 + 4000 + 2000 + 1500 + 500);
  });

  it('totalValue matches sum of segments for cost metric', () => {
    const model: TopModelEntry = {
      ...mockModel,
      cost_input: 0.08,
      cost_output: 0.04,
      cost_cache_read: 0.03,
      cost_cache_create: 0.01,
    };
    render(
      <TopModelsBar models={[model]} metric="cost" />,
      { wrapper },
    );
    const series = captured.option!.series as { data: number[] }[];
    const segmentSum = series.reduce((acc, s) => acc + s.data[0], 0);
    expect(segmentSum).toBeCloseTo(0.08 + 0.04 + 0.03 + 0.01);
  });

  it('empty models array produces empty chart config', () => {
    render(
      <TopModelsBar models={[]} metric="tokens" />,
      { wrapper },
    );
    const series = captured.option!.series as { data: number[] }[];
    expect(series.every((s) => s.data.length === 0)).toBe(true);
    const categoryData = (captured.option!.yAxis as { data: string[] }).data;
    expect(categoryData).toEqual([]);
  });

  it('sort order places largest bar first', () => {
    const small: TopModelEntry = {
      ...mockModel,
      model_id: 'small',
      tokens_input: 1000,
      tokens_output: 500,
    };
    const large: TopModelEntry = {
      ...mockModel,
      model_id: 'large',
      tokens_input: 10000,
      tokens_output: 5000,
    };
    render(
      <TopModelsBar models={[small, large]} metric="tokens" />,
      { wrapper },
    );
    const categoryData = (captured.option!.yAxis as { data: string[] }).data;
    // ECharts draws bottom-up, so ascending sort puts largest last in the array
    // which appears first visually at the top. The first entry in data is the
    // smallest (bottom), last is largest (top).
    expect(categoryData[categoryData.length - 1]).toBe('large');
  });

  it('gives the stacked bars a legend naming the segments', () => {
    document.documentElement.style.setProperty('--fg-muted', '#9a9aa2');
    render(<TopModelsBar models={[mockModel]} metric="tokens" />, { wrapper });
    const legend = captured.option!.legend as {
      bottom: number;
      icon: string;
      textStyle: { color: string };
    };
    expect(legend.bottom).toBe(0);
    expect(legend.icon).toBe('square');
    // The legend text follows the theme token, so it reads on both themes.
    expect(legend.textStyle.color).toBe('#9a9aa2');
    document.documentElement.style.removeProperty('--fg-muted');
  });

  it('drops the cache segments from the legend when excludeCache is set', () => {
    render(<TopModelsBar models={[mockModel]} metric="tokens" excludeCache />, { wrapper });
    const series = captured.option!.series as { name: string }[];
    // The legend follows the series that are actually drawn.
    expect(series.map((s) => s.name)).not.toContain('Cache read');
    const grid = captured.option!.grid as { bottom: number };
    // Two wrapped legend lines (≈48px) must not cover the x-axis labels.
    expect(grid.bottom).toBeGreaterThanOrEqual(48);
  });

  it('labels the chart and exposes the model and segment values as text', () => {
    const { getByTestId, getByText } = render(
      <TopModelsBar models={[mockModel]} metric="tokens" />,
      { wrapper },
    );
    expect(getByTestId('echart')).toHaveAttribute('aria-label', 'Top models chart');
    expect(getByText(/claude-sonnet: 15K \(via anthropic\)/)).toBeInTheDocument();
    expect(getByText('Input: 10K')).toBeInTheDocument();
    expect(getByText('Output: 5K')).toBeInTheDocument();
  });

  it('renders through the shared RankBar, legend and tooltip included', () => {
    render(<TopModelsBar models={[mockModel]} metric="tokens" />, { wrapper });
    // The legend is RankBar's, not a second copy of it.
    const legend = captured.option!.legend as { bottom: number; icon: string };
    expect(legend.icon).toBe('square');
    expect(legend.bottom).toBe(0);
    // ...and so is the tooltip: it names the row, the segments, the total
    // and the providers.
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([
      { seriesName: 'Input', value: 10000, dataIndex: 0 },
      { seriesName: 'Output', value: 5000, dataIndex: 0 },
    ]);
    expect(result).toContain('<b>claude-sonnet</b>');
    expect(result).toContain('Input: 10K');
    expect(result).toContain('Output: 5K');
    expect(result).toContain('Total: 15K');
    expect(result).toContain('via anthropic');
  });

  it('discloses the backend cost total when it differs from the drawn segments', () => {
    const model: TopModelEntry = {
      ...mockModel,
      // The provider reports its own total; the components add up to less.
      cost_usd: 0.20,
      cost_input: 0.10,
      cost_output: 0.05,
    };
    render(<TopModelsBar models={[model]} metric="cost" />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([
      { seriesName: 'Input', value: 0.10, dataIndex: 0 },
      { seriesName: 'Output', value: 0.05, dataIndex: 0 },
    ]);
    // The bar draws $0.15; the backend says $0.20. Both numbers are shown.
    expect(result).toContain('Total: $0.15');
    expect(result).toContain('Reported: $0.20');
  });

  it('prints only the total when the backend cost total matches the drawn segments', () => {
    render(<TopModelsBar models={[mockModel]} metric="cost" />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([
      { seriesName: 'Input', value: 0.10, dataIndex: 0 },
      { seriesName: 'Output', value: 0.05, dataIndex: 0 },
    ]);
    expect(result).toContain('Total: $0.15');
    expect(result).not.toContain('Reported');
  });

  it('does not disclose a reported total for the tokens metric', () => {
    render(<TopModelsBar models={[mockModel]} metric="tokens" />, { wrapper });
    const tooltip = captured.option!.tooltip as {
      formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
    };
    const result = tooltip.formatter([{ seriesName: 'Input', value: 10000, dataIndex: 0 }]);
    expect(result).toContain('Total: 15K');
    expect(result).not.toContain('Reported');
  });

  it('keeps the metric-specific caption on the text alternative', () => {
    const { getByText } = render(<TopModelsBar models={[mockModel]} metric="cost" />, { wrapper });
    expect(getByText('Top models by cost')).toBeInTheDocument();
  });
});
