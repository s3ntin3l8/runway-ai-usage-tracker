import { screen, waitFor } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { ActivityTab } from './ActivityTab';
import * as api from '@/api/endpoints';
import {
  currentPeriod,
  cumulativeResponse,
  emptyCumulative,
  heatmapResponse,
  historyChart,
  pastPeriod,
  session,
} from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('@/components/charts/TokenDonut', () => ({
  TokenDonut: () => <div data-testid="token-donut" />,
}));
vi.mock('@/components/charts/ModelDonut', () => ({
  ModelDonut: () => <div data-testid="model-donut" />,
}));
vi.mock('@/components/charts/UsageHeatmap', () => ({
  UsageHeatmap: () => <div data-testid="heatmap" />,
}));
vi.mock('@/features/history/HistoryChart', () => ({
  HistoryChart: () => <div data-testid="history-chart" />,
}));

describe('ActivityTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(true));
  });

  it('renders token composition and per-model donuts with month data', async () => {
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    vi.mocked(api.fetchHeatmap).mockResolvedValue(heatmapResponse(true));
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [session()] } as never);
    renderWithProviders(
      <ActivityTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );

    expect(await screen.findByText(/^Token composition ·/)).toBeInTheDocument();
    expect(await screen.findByTestId('token-donut')).toBeInTheDocument();
    expect(await screen.findByTestId('model-donut')).toBeInTheDocument();
  });

  it('renders the heatmap when there is activity', async () => {
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    vi.mocked(api.fetchHeatmap).mockResolvedValue(heatmapResponse(true));
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [] } as never);
    renderWithProviders(
      <ActivityTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );
    expect(await screen.findByTestId('heatmap')).toBeInTheDocument();
  });

  it('shows empty messages when there is no month or heatmap data', async () => {
    vi.mocked(api.fetchCumulative).mockResolvedValue(emptyCumulative());
    vi.mocked(api.fetchHeatmap).mockResolvedValue(heatmapResponse(false));
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [] } as never);
    renderWithProviders(
      <ActivityTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );

    expect(await screen.findByText(/no usage recorded in/i)).toBeInTheDocument();
    expect(await screen.findByText(/no event activity in the last 14 days/i)).toBeInTheDocument();
    expect(await screen.findByText(/no sessions recorded/i)).toBeInTheDocument();
  });

  it('renders the top-sessions table when sessions exist', async () => {
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    vi.mocked(api.fetchHeatmap).mockResolvedValue(heatmapResponse(true));
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [session()] } as never);
    renderWithProviders(
      <ActivityTab providerId="anthropic" accountId="me@example.com" period={currentPeriod()} />,
    );
    expect(await screen.findByText('Top sessions (7 days)')).toBeInTheDocument();
    expect(await screen.findByText('Session')).toBeInTheDocument();
  });

  it('scopes heatmap and sessions to the selected past month', async () => {
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
    vi.mocked(api.fetchHeatmap).mockResolvedValue(heatmapResponse(true));
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [] } as never);
    const period = pastPeriod('2026-01');
    renderWithProviders(
      <ActivityTab providerId="anthropic" accountId="me@example.com" period={period} />,
    );

    await waitFor(() =>
      expect(api.fetchHeatmap).toHaveBeenCalledWith(
        expect.objectContaining({ since: period.range.since, until: period.range.until }),
      ),
    );
    // A past month reads the tz-correct month-scoped cumulative bucket.
    expect(api.fetchCumulative).toHaveBeenCalledWith(
      expect.objectContaining({ period_type: 'month', period_key: '2026-01' }),
    );
    expect(api.fetchSessions).toHaveBeenCalledWith(
      expect.objectContaining({ since: period.range.since, until: period.range.until }),
    );
  });
});
