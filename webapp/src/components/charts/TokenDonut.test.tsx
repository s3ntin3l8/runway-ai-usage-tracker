import { render } from '@testing-library/react';
import type { ReactNode } from 'react';
import { ThemeProvider } from '@/hooks/useTheme';
import type { CumulativeModelBucket } from '@/api/types';

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

import { TokenDonut } from './TokenDonut';

function wrapper({ children }: { children: ReactNode }) {
  return <ThemeProvider>{children}</ThemeProvider>;
}

type Slice = { name: string; value: number; itemStyle?: { color: string } };
type Series = { data: Slice[] }[];

// jsdom resolves CSS custom properties to '' unless they are set, so the
// palette is written explicitly and the resolved colour is what the canvas
// would paint.
const PALETTE = ['#c1', '#c2', '#c3', '#c4', '#c5', '#c6'];

function colorOf(data: Slice[], name: string): string | undefined {
  return data.find((d) => d.name === name)?.itemStyle?.color;
}

describe('TokenDonut', () => {
  beforeEach(() => {
    PALETTE.forEach((c, i) => document.documentElement.style.setProperty(`--chart-${i + 1}`, c));
  });

  afterEach(() => {
    PALETTE.forEach((_, i) =>
      document.documentElement.style.removeProperty(`--chart-${i + 1}`),
    );
  });

  it('renders one slice per non-zero token category', () => {
    const bucket: CumulativeModelBucket = {
      tokens_input: 100,
      tokens_output: 40,
      tokens_cache_read: 0,
      tokens_reasoning: 10,
    };
    render(<TokenDonut bucket={bucket} />, { wrapper });
    const data = (captured.option!.series as Series)[0].data;
    expect(data).toEqual([
      { name: 'Input', value: 100, itemStyle: { color: '#c1' } },
      { name: 'Output', value: 40, itemStyle: { color: '#c2' } },
      { name: 'Reasoning', value: 10, itemStyle: { color: '#c3' } },
    ]);
  });

  it('drops every slice for an empty bucket', () => {
    render(<TokenDonut bucket={{}} />, { wrapper });
    expect((captured.option!.series as Series)[0].data).toEqual([]);
  });

  it('keeps a slice colour when cache is excluded', () => {
    const bucket: CumulativeModelBucket = {
      tokens_input: 100,
      tokens_output: 40,
      tokens_cache_read: 70,
      tokens_cache_create: 30,
      tokens_reasoning: 10,
    };
    render(<TokenDonut bucket={bucket} />, { wrapper });
    const withCache = (captured.option!.series as Series)[0].data;
    render(<TokenDonut bucket={bucket} excludeCache />, { wrapper });
    const withoutCache = (captured.option!.series as Series)[0].data;

    // The slice list shrinks, but a slice keeps the colour it owns.
    expect(colorOf(withCache, 'Reasoning')).toBe('#c3');
    expect(colorOf(withoutCache, 'Reasoning')).toBe('#c3');
    expect(colorOf(withCache, 'Cache read')).toBe('#c4');
    expect(colorOf(withoutCache, 'Input')).toBe('#c1');
  });

  it('labels the chart and exposes every slice value as text', () => {
    const bucket: CumulativeModelBucket = { tokens_input: 1000, tokens_output: 40 };
    const { getByTestId, getByText } = render(<TokenDonut bucket={bucket} />, { wrapper });
    expect(getByTestId('echart')).toHaveAttribute('aria-label', 'Token composition chart');
    expect(getByText('Input: 1K')).toBeInTheDocument();
    expect(getByText('Output: 40')).toBeInTheDocument();
  });
});
