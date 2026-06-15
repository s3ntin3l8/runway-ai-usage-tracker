import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { CostTab } from './CostTab';
import * as api from '@/api/endpoints';
import {
  costForecast,
  currentPeriod,
  cumulativeResponse,
  emptyCumulative,
  historyChart,
  pastPeriod,
} from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('@/features/history/HistoryChart', () => ({
  HistoryChart: () => <div data-testid="history-chart" />,
}));
// CostDonut renders ECharts (no canvas in jsdom): stub to a marker.
vi.mock('@/components/charts/CostDonut', () => ({
  CostDonut: () => <div data-testid="cost-donut" />,
}));

describe('CostTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The exclude-cache pref persists to localStorage; reset it between tests.
    localStorage.clear();
    vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(true));
  });

  it('renders the stat tiles with formatted cost', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    renderWithProviders(
      <CostTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );

    expect(await screen.findByText('Spend (MTD)')).toBeInTheDocument();
    expect(screen.getByText('Projected EOM')).toBeInTheDocument();
    expect(screen.getByText('Lifetime')).toBeInTheDocument();
    expect(await screen.findByText(/20d left/)).toBeInTheDocument();
  });

  it('renders the per-model split table with a row', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    renderWithProviders(
      <CostTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );

    expect(await screen.findByText(/^Cost by model ·/)).toBeInTheDocument();
    expect(await screen.findByText('claude-opus')).toBeInTheDocument();
    expect(screen.getByText(/^Cost by sidecar ·/)).toBeInTheDocument();
    expect(await screen.findByText('laptop')).toBeInTheDocument();
    // Split token columns replace the old single "Tokens" column.
    expect(screen.getAllByText('Input').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Output').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Cache read').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Cache write').length).toBeGreaterThan(0);
    // Each split card pairs a cost donut with its table.
    expect(screen.getAllByTestId('cost-donut').length).toBe(2);
  });

  it('hides the cache columns when "Exclude cache" is toggled on', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    renderWithProviders(
      <CostTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );

    expect((await screen.findAllByText('Cache read')).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('switch', { name: /exclude cache/i }));
    expect(screen.queryByText('Cache read')).not.toBeInTheDocument();
    expect(screen.queryByText('Cache write')).not.toBeInTheDocument();
    // Input/Output columns remain.
    expect(screen.getAllByText('Input').length).toBeGreaterThan(0);
  });

  it('shows the empty split message with no month bucket', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(emptyCumulative());
    renderWithProviders(
      <CostTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );
    expect((await screen.findAllByText(/no cost data in/i)).length).toBeGreaterThan(0);
  });

  it('falls back to recorded spend and hides projections for a past month', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    renderWithProviders(
      <CostTab providerId="anthropic" accountId="me@example.com" period={pastPeriod('2026-01')} />,
    );

    // Spend tile is month-scoped; EOM/burn are not applicable.
    expect(await screen.findByText(/^Spend ·/)).toBeInTheDocument();
    expect((await screen.findAllByText('current month only')).length).toBeGreaterThan(0);
    // Past month reads the tz-correct month-scoped cumulative bucket.
    await waitFor(() =>
      expect(api.fetchCumulative).toHaveBeenCalledWith(
        expect.objectContaining({ period_type: 'month', period_key: '2026-01' }),
      ),
    );
  });
});
