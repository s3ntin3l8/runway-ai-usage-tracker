import { render, screen } from '@testing-library/react';
import type { HistoryChartResponse } from '@/api/types';
import { HistoryChart } from './HistoryChart';

// Capture the option the chart builds so we can assert the branch logic
// without a real ECharts instance.
let lastOption: Record<string, unknown> | null = null;
vi.mock('@/components/charts/EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    lastOption = option;
    return <div data-testid="echart" />;
  },
}));
vi.mock('@/components/charts/theme', async () => {
  // The real cap helpers stay: the tests below assert on the cap itself, not
  // on a copy of it. Only the tokens and the option fragments are stubbed.
  const actual = await vi.importActual<typeof import('@/components/charts/theme')>(
    '@/components/charts/theme',
  );
  return {
    ...actual,
    useChartTokens: () => ({
      series: ['#a', '#b', '#c', '#d', '#e', '#f'],
      accent: '#0af',
      fgMuted: '#888',
      axis: '#555',
      fontFamily: 'sans',
    }),
    baseTooltip: () => ({}),
    baseAxisStyle: () => ({}),
  };
});

type BarSeries = { name: string; data: number[]; itemStyle: { color: string } }[];

const SEGMENT_LABELS = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'];

// Eight segment labels in one bucket: the values run 10 … 80, and the two
// labels past the palette size carry a cache portion of their own.
function barData(): HistoryChartResponse {
  return {
    bars: [
      {
        date: '2026-06-10',
        ts: '2026-06-10T00:00:00Z',
        segments: SEGMENT_LABELS.map((label, i) => ({
          provider_id: 'claude',
          model_id: label,
          label,
          value: (i + 1) * 10,
          value_cache: label === 'g' ? 20 : label === 'h' ? 30 : 0,
        })),
      },
    ],
  };
}

function percentData(count: number): HistoryChartResponse {
  return {
    series: Array.from({ length: count }, (_, i) => ({
      key: `k${i}`,
      provider_id: 'claude',
      window_type: 'weekly',
      model_id: `m${i}`,
      label: `series ${i}`,
      points: [{ ts: '2026-06-10T00:00:00Z', pct_used: 10 }],
    })),
  };
}

describe('HistoryChart', () => {
  beforeEach(() => {
    lastOption = null;
  });

  it('builds line series for the percent metric', () => {
    const data: HistoryChartResponse = {
      series: [
        {
          key: 'k',
          provider_id: 'claude',
          window_type: 'weekly',
          model_id: '',
          label: 'Claude weekly',
          points: [
            { ts: '2026-06-10T00:00:00Z', pct_used: 40 },
            { ts: '2026-06-11T00:00:00Z', pct_used: 55 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="percent" />);
    const series = (lastOption as { series: { type: string }[] }).series;
    expect(series[0].type).toBe('line');
  });

  it('builds stacked bars for the tokens metric', () => {
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'opus', label: 'opus', value: 100 },
            { provider_id: 'claude', model_id: 'sonnet', label: 'sonnet', value: 50 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="tokens" />);
    const series = (lastOption as { series: { type: string; stack: string }[] }).series;
    expect(series[0].type).toBe('bar');
    expect(series[0].stack).toBe('total');
  });

  it('subtracts value_cache from token bars when excludeCache is set', () => {
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'opus', label: 'opus', value: 100, value_cache: 30 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="tokens" excludeCache />);
    const series = (lastOption as { series: { data: number[] }[] }).series;
    expect(series[0].data[0]).toBe(70);
  });

  it('keeps the full token value when excludeCache is off', () => {
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'opus', label: 'opus', value: 100, value_cache: 30 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="tokens" />);
    const series = (lastOption as { series: { data: number[] }[] }).series;
    expect(series[0].data[0]).toBe(100);
  });

  it('builds bars for the cost metric (currency value formatter)', () => {
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [{ provider_id: 'claude', model_id: 'opus', label: 'opus', value: 3.5 }],
        },
      ],
    };
    render(<HistoryChart data={data} metric="cost" />);
    expect((lastOption as { series: unknown[] }).series).toHaveLength(1);
  });

  it('renders a missing cost bucket as a dash rather than zero', () => {
    // The audit finding: a null bucket rendered "$0.00", which reads as a
    // real measurement of no spend instead of a bucket with no data.
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [{ provider_id: 'claude', model_id: 'opus', label: 'opus', value: 3.5 }],
        },
        {
          date: '2026-06-11',
          ts: '2026-06-11T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'sonnet', label: 'sonnet', value: 1.25 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="cost" />);
    const series = (lastOption as { series: { data: (number | null)[] }[] }).series;
    // The opus series has no segment in the second bucket. That bucket is
    // missing, so it must not be drawn as a zero-height bar — the chart has
    // to produce the null the formatter renders as a dash.
    expect(series[0].data).toEqual([3.5, null]);
    const formatter = (
      lastOption as { tooltip: { valueFormatter: (v: number | null) => string } }
    ).tooltip.valueFormatter;
    expect(formatter(null)).toBe('—');
    expect(formatter(0)).toBe('$0.00');
    expect(formatter(3.5)).toBe('$3.50');
  });

  it('leaves a missing bucket out of the series rather than drawing it as zero', () => {
    // The formatter can say "—", but only if the data actually carries a
    // null. A zero in the series is a measurement of nothing spent, and the
    // bar would be drawn at zero height as though the bucket had been
    // measured.
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [{ provider_id: 'claude', model_id: 'opus', label: 'opus', value: 3.5 }],
        },
        {
          date: '2026-06-11',
          ts: '2026-06-11T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'sonnet', label: 'sonnet', value: 1.25 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="cost" />);
    const series = (lastOption as { series: { data: (number | null)[] }[] }).series;
    // The opus series has no segment in the second bucket. That bucket is
    // missing, so it must not be drawn as a zero-height bar — the chart has
    // to produce the null the formatter renders as a dash.
    expect(series[0].data).toEqual([3.5, null]);
  });

  it('subtracts value_cache (cache cost) from cost bars when excludeCache is set', () => {
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'opus', label: 'opus', value: 3.5, value_cache: 2 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="cost" excludeCache />);
    const series = (lastOption as { series: { data: number[] }[] }).series;
    expect(series[0].data[0]).toBe(1.5);
  });

  it('keeps the full cost when excludeCache is off', () => {
    const data: HistoryChartResponse = {
      bars: [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [
            { provider_id: 'claude', model_id: 'opus', label: 'opus', value: 3.5, value_cache: 2 },
          ],
        },
      ],
    };
    render(<HistoryChart data={data} metric="cost" />);
    const series = (lastOption as { series: { data: number[] }[] }).series;
    expect(series[0].data[0]).toBe(3.5);
  });
});

describe('HistoryChart series cap', () => {
  beforeEach(() => {
    lastOption = null;
  });

  it('gives the first six segments their own colour and aggregates the rest', () => {
    render(<HistoryChart data={barData()} metric="tokens" />);
    const series = (lastOption as { series: BarSeries }).series;
    // Six coloured segments plus one "Other" segment, never seven coloured
    // ones (the old code repeated the first colour for the seventh).
    expect(series).toHaveLength(7);
    expect(series.slice(0, 6).map((s) => s.name)).toEqual(['a', 'b', 'c', 'd', 'e', 'f']);
    expect(series[6].name).toBe('Other (2)');
    const colours = series.slice(0, 6).map((s) => s.itemStyle.color);
    expect(new Set(colours).size).toBe(6);
  });

  it('sums the aggregated segments into the "Other" segment', () => {
    render(<HistoryChart data={barData()} metric="tokens" />);
    const series = (lastOption as { series: BarSeries }).series;
    // 'g' (70) and 'h' (80) share the aggregate.
    expect(series[6].data).toEqual([150]);
  });

  it('excludes the cache portion from the aggregate as well', () => {
    render(<HistoryChart data={barData()} metric="tokens" excludeCache />);
    const series = (lastOption as { series: BarSeries }).series;
    // 'g' (70 - 20) and 'h' (80 - 30).
    expect(series[6].data).toEqual([100]);
    // The drawn segments keep their own exclusion.
    expect(series[0].data).toEqual([10]);
  });

  it('caps the percent curves without aggregating them', () => {
    render(<HistoryChart data={percentData(8)} metric="percent" />);
    const series = (lastOption as { series: { name: string }[] }).series;
    expect(series).toHaveLength(6);
    expect(series.map((s) => s.name)).toEqual([
      'series 0',
      'series 1',
      'series 2',
      'series 3',
      'series 4',
      'series 5',
    ]);
  });

  it('says how many series the percent chart left out', () => {
    render(<HistoryChart data={percentData(8)} metric="percent" />);
    expect(screen.getByText('+2 more series not shown')).toBeInTheDocument();
  });

  it('says nothing about missing series when all of them fit', () => {
    render(<HistoryChart data={percentData(6)} metric="percent" />);
    expect(screen.queryByText(/more series not shown/)).not.toBeInTheDocument();
  });
});
