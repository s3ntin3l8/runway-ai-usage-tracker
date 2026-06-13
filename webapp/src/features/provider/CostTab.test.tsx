import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { CostTab } from './CostTab';
import * as api from '@/api/endpoints';
import { costForecast, cumulativeResponse, emptyCumulative, historyChart } from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('@/features/history/HistoryChart', () => ({
  HistoryChart: () => <div data-testid="history-chart" />,
}));

describe('CostTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(true));
  });

  it('renders the stat tiles with formatted cost', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    renderWithProviders(<CostTab providerId="anthropic" accountId="me@example.com" />);

    expect(await screen.findByText('Spend (MTD)')).toBeInTheDocument();
    expect(screen.getByText('Projected EOM')).toBeInTheDocument();
    expect(screen.getByText('Lifetime')).toBeInTheDocument();
    expect(await screen.findByText(/20d left/)).toBeInTheDocument();
  });

  it('renders the per-model split table with a row', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    renderWithProviders(<CostTab providerId="anthropic" accountId="me@example.com" />);

    expect(await screen.findByText('Cost by model (this month)')).toBeInTheDocument();
    expect(await screen.findByText('claude-opus')).toBeInTheDocument();
    expect(screen.getByText('Cost by sidecar (this month)')).toBeInTheDocument();
    expect(await screen.findByText('laptop')).toBeInTheDocument();
  });

  it('shows the empty split message with no month bucket', async () => {
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(emptyCumulative());
    renderWithProviders(<CostTab providerId="anthropic" accountId="me@example.com" />);
    expect((await screen.findAllByText(/no cost data this month/i)).length).toBeGreaterThan(0);
  });
});
