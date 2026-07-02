import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { HistoryChartResponse, HistoryDeltas } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { OverallChartCard } from './OverallChartCard';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
// Stub the chart so jsdom never touches canvas.
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => <div data-testid="echart" />,
}));
vi.mock('@/components/charts/theme', () => ({
  useChartTokens: () => ({ series: ['#111'], accent: '#0af', fgMuted: '#888', axis: '#555' }),
  baseTooltip: () => ({}),
  baseAxisStyle: () => ({}),
}));

const bars: HistoryChartResponse = {
  bars: [
    {
      date: '2026-06-10',
      ts: '2026-06-10T00:00:00Z',
      segments: [{ provider_id: 'claude', model_id: '', label: 'Claude', value: 1000 }],
    },
  ],
};

const deltas: HistoryDeltas = {
  token_delta_total: 5_000_000,
  token_cache_total: 4_000_000,
  cost_delta_total: 6.5,
  cost_cache_total: 4.5,
  critical_series_count: 0,
};

describe('OverallChartCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.fetchHistoryChart).mockResolvedValue(bars);
    vi.mocked(api.fetchHistoryDeltas).mockResolvedValue(deltas);
  });

  it('renders the range-total tiles and the stacked chart', async () => {
    renderWithProviders(<OverallChartCard days={7} />);
    expect(await screen.findByTestId('echart')).toBeInTheDocument();
    expect(screen.getByText(/Tokens \(7d\)/)).toBeInTheDocument();
    expect(screen.getByText(/Cost \(7d\)/)).toBeInTheDocument();
    // Fetches cross-provider, stacked per provider.
    expect(api.fetchHistoryChart).toHaveBeenCalledWith(
      expect.objectContaining({ days: 7, metric: 'tokens', group: 'provider' }),
    );
  });

  it('refetches with cost when the metric toggle flips', async () => {
    renderWithProviders(<OverallChartCard days={7} />);
    await screen.findByTestId('echart');
    const tabs = screen.getByRole('tablist', { name: 'Overall metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    expect(api.fetchHistoryChart).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'cost', group: 'provider' }),
    );
  });

  it('shows cache-inclusive token and cost totals by default', async () => {
    renderWithProviders(<OverallChartCard days={7} />);
    await screen.findByTestId('echart');
    // token_delta_total is cache-inclusive: 5M
    expect(screen.getByText('5.00M')).toBeInTheDocument();
    // cost_delta_total is cache-inclusive: $6.50
    expect(screen.getByText(/\$6\.50/)).toBeInTheDocument();
  });

  it('subtracts cache from token and cost totals when exclude-cache is on', async () => {
    localStorage.setItem('runway_exclude_cache', '1');
    renderWithProviders(<OverallChartCard days={7} />);
    await screen.findByTestId('echart');
    // 5M tokens - 4M cache = 1M
    expect(screen.getByText('1.00M')).toBeInTheDocument();
    // $6.50 - $4.50 cache = $2.00
    expect(screen.getByText(/\$2\.00/)).toBeInTheDocument();
  });

  it('shows the empty state when no bars are returned', async () => {
    vi.mocked(api.fetchHistoryChart).mockResolvedValue({ bars: [] });
    renderWithProviders(<OverallChartCard days={7} />);
    expect(await screen.findByText(/no usage in this range/i)).toBeInTheDocument();
  });
});
