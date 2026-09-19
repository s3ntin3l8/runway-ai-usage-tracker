import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { TopModelEntry } from '@/api/types';

const captured: { option?: Record<string, unknown> } = {};
vi.mock('./EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" />;
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
});
