// Sub-card components: ProviderKpis, ProviderAlerts, ProviderTrendCard,
// QuotaWindowRow, RecentSessions.
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { ProviderKpis } from './ProviderKpis';
import { ProviderAlerts } from './ProviderAlerts';
import { ProviderTrendCard } from './ProviderTrendCard';
import { QuotaWindowRow } from './QuotaWindowRow';
import { RecentSessions } from './RecentSessions';
import { resolveScope } from './period';
import * as api from '@/api/endpoints';
import {
  anomaliesResponse,
  costForecast,
  cumulativeResponse,
  currentPeriod,
  errorEvents,
  emptyEvents,
  fleetEntry,
  forecastEntry,
  forecastResponse,
  historyChart,
  limitCard,
  pastPeriod,
  rollingScope,
  session,
} from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('@/features/history/HistoryChart', () => ({
  HistoryChart: () => <div data-testid="history-chart" />,
}));

// Live-month scope: KPI tiles show the MTD/EOM projection labels.
const scope = currentPeriod();

describe('ProviderKpis', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchForecast).mockResolvedValue(forecastResponse([forecastEntry()]));
    vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
    vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
  });

  it('renders the six KPI tiles with values', async () => {
    renderWithProviders(
      <ProviderKpis entry={fleetEntry({ billing_type: 'subscription' })} scope={scope} />,
    );
    expect(await screen.findByText('Current')).toBeInTheDocument();
    expect(screen.getByText('Projected at reset')).toBeInTheDocument();
    expect(screen.getByText('Estimated usage value (MTD)')).toBeInTheDocument();
    expect(screen.getByText('Daily usage value (7d)')).toBeInTheDocument();
    expect(screen.getByText(`Tokens · ${scope.label}`)).toBeInTheDocument();
    expect(screen.getByText(`Cache hit · ${scope.label}`)).toBeInTheDocument();
  });

  it('labels pay-as-you-go account values as spend', async () => {
    renderWithProviders(<ProviderKpis entry={fleetEntry({ billing_type: 'pay_as_you_go' })} scope={scope} />);
    expect(await screen.findByText('Spend (MTD)')).toBeInTheDocument();
    expect(screen.getByText('Daily burn (7d)')).toBeInTheDocument();
  });

  it('labels unknown account values neutrally', async () => {
    renderWithProviders(<ProviderKpis entry={fleetEntry({ billing_type: 'unknown' })} scope={scope} />);
    expect(await screen.findByText('Usage value (MTD)')).toBeInTheDocument();
  });

  it('falls back to recorded range spend outside the live month', async () => {
    const past = resolveScope({ days: 30 });
    renderWithProviders(
      <ProviderKpis entry={fleetEntry({ billing_type: 'subscription' })} scope={past} />,
    );
    expect(await screen.findByText(`Estimated usage value · ${past.label}`)).toBeInTheDocument();
    expect(screen.getByText('current month only')).toBeInTheDocument();
  });

  // Pins the three-way enable gates in ProviderKpis.tsx: per scope exactly one
  // of the month / range query paths may fetch, alongside the always-on live
  // lifetime bucket. Flipping any `enabled` flag fails these.
  describe('cumulative query gating', () => {
    it('requests only the live bucket for the live-month scope', async () => {
      renderWithProviders(
        <ProviderKpis entry={fleetEntry({ billing_type: 'subscription' })} scope={currentPeriod()} />,
      );
      expect(await screen.findByText('Current')).toBeInTheDocument();
      expect(api.fetchCumulative).toHaveBeenCalledWith({
        provider_id: 'anthropic',
        account_id: 'me@example.com',
      });
      expect(api.fetchCumulative).not.toHaveBeenCalledWith(
        expect.objectContaining({ period_type: expect.anything() }),
      );
      expect(api.fetchCumulative).not.toHaveBeenCalledWith(
        expect.objectContaining({ since: expect.anything() }),
      );
    });

    it('requests only the month bucket for a past-month scope', async () => {
      renderWithProviders(
        <ProviderKpis
          entry={fleetEntry({ billing_type: 'subscription' })}
          scope={pastPeriod('2026-01')}
        />,
      );
      expect(await screen.findByText('Current')).toBeInTheDocument();
      expect(api.fetchCumulative).toHaveBeenCalledWith(
        expect.objectContaining({ period_type: 'month', period_key: '2026-01' }),
      );
      expect(api.fetchCumulative).not.toHaveBeenCalledWith(
        expect.objectContaining({ since: expect.anything() }),
      );
    });

    it('requests only the range bucket for a rolling scope', async () => {
      renderWithProviders(
        <ProviderKpis entry={fleetEntry({ billing_type: 'subscription' })} scope={rollingScope(30)} />,
      );
      expect(await screen.findByText('Current')).toBeInTheDocument();
      expect(api.fetchCumulative).toHaveBeenCalledWith(
        expect.objectContaining({ since: expect.any(String), until: expect.any(String) }),
      );
      expect(api.fetchCumulative).not.toHaveBeenCalledWith(
        expect.objectContaining({ period_type: expect.anything() }),
      );
    });
  });

  describe('tokens kind (unlimited / passive provider)', () => {
    const tokenFleetEntry = () =>
      fleetEntry({
        provider_id: 'opencode-free',
        critical_gauge: {
          service_name: 'Opencode Free',
          provider_id: 'opencode-free',
          account_id: 'me@example.com',
          is_unlimited: true,
          window_type: 'lifetime',
          token_usage: {
            input: 45_000_000,
            output: 600_000,
            reasoning: 38_000,
            cache_read: 700_000_000,
            cache_create: 43_000_000,
            total: 45_638_000,
          },
          msgs: 1000,
        },
      });

    it('shows the selected-month tokens when excludeCache is off', async () => {
      const monthKey = new Date().toISOString().slice(0, 7);
      const response = cumulativeResponse({ current_month_key: monthKey });
      response.cumulative[0].provider_id = 'opencode-free';
      response.cumulative[0][monthKey] = {
        tokens_input: 45_000_000,
        tokens_output: 600_000,
        tokens_reasoning: 38_000,
        tokens_cache_read: 700_000_000,
        tokens_cache_create: 43_000_000,
      };
      vi.mocked(api.fetchCumulative).mockResolvedValue(response);
      renderWithProviders(
        <ProviderKpis entry={tokenFleetEntry()} scope={scope} excludeCache={false} />,
      );
      expect(await screen.findByText('788.64M')).toBeInTheDocument();
    });

    it('excludes cache tokens from the selected-month total when excludeCache is on', async () => {
      const monthKey = new Date().toISOString().slice(0, 7);
      const response = cumulativeResponse({ current_month_key: monthKey });
      response.cumulative[0].provider_id = 'opencode-free';
      response.cumulative[0][monthKey] = {
        tokens_input: 45_000_000,
        tokens_output: 600_000,
        tokens_reasoning: 38_000,
        tokens_cache_read: 700_000_000,
        tokens_cache_create: 43_000_000,
      };
      vi.mocked(api.fetchCumulative).mockResolvedValue(response);
      renderWithProviders(<ProviderKpis entry={tokenFleetEntry()} scope={scope} excludeCache />);
      expect(await screen.findByText('45.64M')).toBeInTheDocument();
    });
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

  it('shows the no-data message for the default range', async () => {
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
    // Default fallback range (last 7 days) drives the initial fetch.
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

  it('shows a Stale badge when card.stale is true', () => {
    const card = limitCard({ pct_used: 40, stale: true });
    renderWithProviders(
      <QuotaWindowRow card={card} siblings={[card]} forecast={null} />,
    );
    expect(screen.getByText('Stale')).toBeInTheDocument();
  });

  it('shows a Stale badge when collection_failing is true without stale', () => {
    const card = limitCard({ pct_used: 40, collection_failing: true });
    renderWithProviders(
      <QuotaWindowRow card={card} siblings={[card]} forecast={null} />,
    );
    expect(screen.getByText('Stale')).toBeInTheDocument();
  });

  it('does not show a Stale badge for non-stale cards', () => {
    const card = limitCard({ pct_used: 40 });
    renderWithProviders(
      <QuotaWindowRow card={card} siblings={[card]} forecast={null} />,
    );
    expect(screen.queryByText('Stale')).not.toBeInTheDocument();
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
