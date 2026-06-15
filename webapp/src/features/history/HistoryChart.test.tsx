import { render } from '@testing-library/react';
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
vi.mock('@/components/charts/theme', () => ({
  useChartTokens: () => ({
    series: ['#a', '#b', '#c'],
    accent: '#0af',
    fgMuted: '#888',
    axis: '#555',
    fontFamily: 'sans',
  }),
  baseTooltip: () => ({}),
  baseAxisStyle: () => ({}),
}));

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
