// Sub-card components: ProviderKpis, ProviderAlerts, ProviderTrendCard,
// QuotaWindowRow, RecentSessions.
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { ProviderKpis } from './ProviderKpis';
import { ProviderAlerts } from './ProviderAlerts';
import { ProviderTrendCard } from './ProviderTrendCard';
import { QuotaWindowRow } from './QuotaWindowRow';
import { RecentSessions } from './RecentSessions';
import * as api from '@/api/endpoints';
import {
  anomaliesResponse,
  costForecast,
  cumulativeResponse,
  errorEvents,
  emptyEvents,
  fleetEntry,
  forecastEntry,
  forecastResponse,
  historyChart,
  limitCard,
  session,
} from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('@/features/history/HistoryChart', () => ({
  HistoryChart: () => <div data-testid="history-chart" />,
}));

describe('ProviderKpis', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchForecast).mockResolvedValue(forecastResponse([forecastEntry()]));
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
  });

  it('renders the six KPI tiles with values', async () => {
    renderWithProviders(<ProviderKpis entry={fleetEntry()} />);
    expect(await screen.findByText('Current')).toBeInTheDocument();
    expect(screen.getByText('Projected at reset')).toBeInTheDocument();
    expect(screen.getByText('Spend (MTD)')).toBeInTheDocument();
    expect(screen.getByText('Daily burn (7d)')).toBeInTheDocument();
    expect(screen.getByText('Tokens (month)')).toBeInTheDocument();
    expect(screen.getByText('Cache hit')).toBeInTheDocument();
  });
});

describe('ProviderAlerts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchAnomalies).mockResolvedValue(anomaliesResponse());
    vi.mocked(api.fetchEvents).mockResolvedValue(emptyEvents());
  });

  it('renders nothing when there are no spikes or errors', async () => {
    const { container } = renderWithProviders(
      <ProviderAlerts providerId="anthropic" accountId="me@example.com" />,
    );
    // Wait a tick for queries to settle; nothing renders.
    await Promise.resolve();
    expect(container.querySelector('[class*="critical"]')).toBeNull();
  });

  it('surfaces a recent-errors banner', async () => {
    vi.mocked(api.fetchEvents).mockResolvedValue(errorEvents());
    renderWithProviders(<ProviderAlerts providerId="anthropic" accountId="me@example.com" />);
    expect(await screen.findByText(/error in the last 24h/i)).toBeInTheDocument();
    expect(screen.getByText(/overloaded/i)).toBeInTheDocument();
  });

  it('surfaces a usage-spike banner', async () => {
    vi.mocked(api.fetchAnomalies).mockResolvedValue(
      anomaliesResponse({
        anomalies: [
          {
            provider_id: 'anthropic',
            account_id: 'me@example.com',
            model_id: 'claude-opus',
            today_tokens: 50000,
            today_cost_usd: 5,
            historical_mean_tokens: 1000,
            historical_stddev_tokens: 200,
            z_score_tokens: 4.2,
            verdict: 'spike',
          },
        ],
      }),
    );
    renderWithProviders(<ProviderAlerts providerId="anthropic" accountId="me@example.com" />);
    expect(await screen.findByText(/usage spike on/i)).toBeInTheDocument();
  });
});

describe('ProviderTrendCard', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the chart when bars exist', async () => {
    vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(true));
    renderWithProviders(
      <ProviderTrendCard
        providerId="anthropic"
        accountId="me@example.com"
        metric="tokens"
        title="Tokens per day"
      />,
    );
    expect(await screen.findByTestId('history-chart')).toBeInTheDocument();
    expect(screen.getByText('Tokens per day')).toBeInTheDocument();
  });

  it('shows the no-data message and switches ranges', async () => {
    vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(false));
    renderWithProviders(
      <ProviderTrendCard
        providerId="anthropic"
        accountId="me@example.com"
        metric="cost"
        title="Cost per day"
      />,
    );
    expect(await screen.findByText(/no data in this range/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: '7d' }));
    // refetch happens for the new range
    expect(api.fetchHistoryChart).toHaveBeenCalledWith(expect.objectContaining({ days: 7 }));
  });
});

describe('QuotaWindowRow', () => {
  it('shows a projected-by-reset summary for a healthy window', () => {
    const card = limitCard({ pct_used: 40 });
    renderWithProviders(
      <QuotaWindowRow
        card={card}
        siblings={[card]}
        forecast={forecastEntry({ projected_pct: 65, glide_pct: 50, status: 'ok' })}
      />,
    );
    expect(screen.getByText(/65% by reset/)).toBeInTheDocument();
  });

  it('warns with a run-out time when at risk', () => {
    const card = limitCard({ pct_used: 90 });
    const hit = new Date(Date.now() + 7_200_000).toISOString();
    renderWithProviders(
      <QuotaWindowRow
        card={card}
        siblings={[card]}
        forecast={forecastEntry({ status: 'risk', projected_limit_hit_at: hit })}
      />,
    );
    expect(screen.getByText(/runs out/i)).toBeInTheDocument();
  });

  it('derives a pacing verdict from glide vs used', () => {
    const card = limitCard({ pct_used: 80 });
    renderWithProviders(
      <QuotaWindowRow
        card={card}
        siblings={[card]}
        forecast={forecastEntry({ glide_pct: 50, projected_pct: 90 })}
      />,
    );
    expect(screen.getByText(/ahead of pace/i)).toBeInTheDocument();
  });
});

describe('RecentSessions', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders cards for recent sessions', async () => {
    vi.mocked(api.fetchSessions).mockResolvedValue({
      sessions: [session({ session_id: 'feedface0000' })],
    } as never);
    renderWithProviders(<RecentSessions providerId="anthropic" accountId="me@example.com" />);
    expect(await screen.findByText('feedface')).toBeInTheDocument();
    expect(screen.getByText('Recent sessions')).toBeInTheDocument();
  });

  it('shows the empty state with no sessions', async () => {
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [] } as never);
    renderWithProviders(<RecentSessions providerId="anthropic" accountId="me@example.com" />);
    expect(await screen.findByText(/no sessions yet/i)).toBeInTheDocument();
  });

  it('labels the originating sidecar when more than one host feeds the fleet', async () => {
    vi.mocked(api.fetchSessions).mockResolvedValue({
      sessions: [session({ session_id: 'feedface0000', sidecar_id: 'laptop' })],
    } as never);
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [
        { sidecar_id: 'laptop', custom_name: 'My Laptop' },
        { sidecar_id: 'desktop', hostname: 'work-desktop' },
      ],
    } as never);
    renderWithProviders(<RecentSessions providerId="anthropic" accountId="me@example.com" />);
    expect(await screen.findByText('My Laptop')).toBeInTheDocument();
  });

  it('hides the sidecar label on a single-host fleet', async () => {
    vi.mocked(api.fetchSessions).mockResolvedValue({
      sessions: [session({ session_id: 'feedface0000', sidecar_id: 'laptop' })],
    } as never);
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'laptop', custom_name: 'My Laptop' }],
    } as never);
    renderWithProviders(<RecentSessions providerId="anthropic" accountId="me@example.com" />);
    await screen.findByText('feedface');
    expect(screen.queryByText('My Laptop')).not.toBeInTheDocument();
  });
});
