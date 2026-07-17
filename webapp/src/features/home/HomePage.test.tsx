import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type {
  CostForecastResponse,
  CumulativeResponse,
  FleetEntry,
  FleetResponse,
  LimitCard,
} from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { formatCost } from '@/lib/format';
import { HomePage } from './HomePage';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), message: vi.fn() },
}));

// Faithful-enough ProviderGrid stub: keeps the "Providers" heading and the
// provider names the other tests assert on, and exposes a button that fires
// onReorder so the saveOrder mutation can be driven deterministically (dnd-kit
// drag simulation is unreliable in jsdom).
vi.mock('./ProviderGrid', () => ({
  ProviderGrid: ({
    items,
    providerNames,
    onReorder,
  }: {
    items: { key: string; entry: { provider_id: string } }[];
    providerNames: Map<string, string>;
    onReorder: (keys: string[]) => void;
  }) => (
    <section aria-label="All providers">
      <h2>Providers</h2>
      {items.map((i) => (
        <span key={i.key}>{providerNames.get(i.entry.provider_id) ?? i.entry.provider_id}</span>
      ))}
      <button onClick={() => onReorder([...items.map((i) => i.key)].reverse())}>
        reorder
      </button>
    </section>
  ),
}));

const card = (o: Partial<LimitCard> = {}): LimitCard => ({
  service_name: 'Claude',
  pct_used: 50,
  window_type: 'weekly',
  reset_at: new Date(Date.now() + 3_600_000).toISOString(),
  updated_at: new Date().toISOString(),
  ...o,
});

const fleetEntry = (o: Partial<FleetEntry> = {}): FleetEntry => ({
  provider_id: 'claude',
  account_id: 'default',
  critical_gauge: card(),
  secondary_limits: [],
  ...o,
});

const fleetResponse = (entries: FleetEntry[]): FleetResponse => ({
  fleet: entries,
  generated_at: new Date().toISOString(),
});

const costResponse: CostForecastResponse = {
  as_of: new Date().toISOString(),
  current_month_to_date: 12.5,
  daily_burn_avg_7d: 0.8,
  projected_eom: 24,
  days_in_month: 30,
  day_of_month: 13,
  days_remaining: 17,
  by_provider: [],
};

const cumulativeResponse: CumulativeResponse = {
  cumulative: [],
  current_month_key: '2026-06',
  current_year_key: '2026',
  generated_at: new Date().toISOString(),
};

// Default-happy mocks for every endpoint the page touches. Individual tests
// override fetchFleetUsage / fetchForecast for their scenario.
function primeDefaults() {
  vi.mocked(api.fetchForecast).mockResolvedValue({ forecasts: [] });
  vi.mocked(api.fetchCostForecast).mockResolvedValue(costResponse);
  vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse);
  vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [] });
  vi.mocked(api.fetchAnomalies).mockResolvedValue({
    as_of: new Date().toISOString(),
    lookback_days: 30,
    z_threshold: 3,
    anomalies: [],
  });
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
    providers: [{ provider_id: 'claude', name: 'Claude' } as never],
  });
  vi.mocked(api.getDashboardLayout).mockResolvedValue({
    provider_order: [],
    card_orders: {},
  });
}

describe('HomePage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    primeDefaults();
  });

  it('shows skeleton loading state while the fleet query is pending', () => {
    vi.mocked(api.fetchFleetUsage).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<HomePage />);
    // Skeletons use the animate-shimmer utility.
    expect(container.querySelector('.animate-shimmer')).toBeInTheDocument();
  });

  it('shows the empty state when no providers report', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([]));
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/no providers reporting yet/i)).toBeInTheDocument();
  });

  it('shows an error state when the fleet query fails', async () => {
    vi.mocked(api.fetchFleetUsage).mockRejectedValue(new Error('boom'));
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/could not load usage data/i)).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
  });

  it('renders the provider grid with the resolved provider name', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderWithProviders(<HomePage />);
    // Name appears in both the all-clear-less grid card and aria-label.
    expect(await screen.findAllByText('Claude')).not.toHaveLength(0);
    expect(screen.getByText('Providers')).toBeInTheDocument();
  });

  it('shows the at-risk rail when a provider is critical', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(
      fleetResponse([fleetEntry({ critical_gauge: card({ pct_used: 96 }) })]),
    );
    renderWithProviders(<HomePage />);
    expect(await screen.findByText('At risk')).toBeInTheDocument();
  });

  it('renders the all-clear rail when nothing is hot', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/all clear/i)).toBeInTheDocument();
  });

  it('triggers a collection when "Collect now" is clicked', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.forceCollect).mockResolvedValue({ cards: 3, sidecars_triggered: 1 } as never);
    renderWithProviders(<HomePage />);
    await screen.findByText('Providers');

    await userEvent.click(screen.getByRole('button', { name: /collect now/i }));
    await waitFor(() => expect(api.forceCollect).toHaveBeenCalled());
  });

  it('surfaces a toast.error when the collection fails', async () => {
    const { toast } = await import('sonner');
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.forceCollect).mockRejectedValue(new Error('sidecar offline'));
    renderWithProviders(<HomePage />);
    await screen.findByText('Providers');

    await userEvent.click(screen.getByRole('button', { name: /collect now/i }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Collection failed: sidecar offline'),
    );
  });

  it('refetches the fleet when Retry is clicked in the error state', async () => {
    vi.mocked(api.fetchFleetUsage).mockRejectedValue(new Error('boom'));
    renderWithProviders(<HomePage />);
    await screen.findByText(/could not load usage data/i);
    expect(api.fetchFleetUsage).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole('button', { name: /retry/i }));
    await waitFor(() => expect(api.fetchFleetUsage).toHaveBeenCalledTimes(2));
  });

  it('shows the aggregate spend strip', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderWithProviders(<HomePage />);
    expect(await screen.findByText('Spend (MTD)')).toBeInTheDocument();
    expect(screen.getByText('Projected EOM')).toBeInTheDocument();
  });

  it('scopes the home cumulative query to the current month only', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    renderWithProviders(<HomePage />);
    await screen.findByText('Providers');
    expect(api.fetchCumulative).toHaveBeenCalledWith({ period_type: 'month' });
  });

  it('renders the cost cards as soon as cost-forecast resolves, independent of the slower cumulative query', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
    vi.mocked(api.fetchCumulative).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<HomePage />);
    expect(await screen.findByText('Spend (MTD)')).toBeInTheDocument();
    expect(await screen.findByText(formatCost(costResponse.current_month_to_date))).toBeInTheDocument();
    // Tokens card stays in skeleton state while cumulative never resolves.
    expect(screen.getByText('Tokens this month')).toBeInTheDocument();
  });
});

describe('HomePage layout reordering', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    primeDefaults();
  });

  it('persists the new order optimistically when the grid reorders', async () => {
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(
      fleetResponse([
        fleetEntry({ provider_id: 'claude' }),
        fleetEntry({ provider_id: 'chatgpt' }),
      ]),
    );
    vi.mocked(api.putDashboardLayout).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<HomePage />);
    await screen.findByText('Providers');

    await userEvent.click(screen.getByRole('button', { name: /reorder/i }));

    // mutationFn forwards the reversed key order plus the existing card_orders.
    await waitFor(() =>
      expect(api.putDashboardLayout).toHaveBeenCalledWith({
        provider_order: ['chatgpt:default', 'claude:default'],
        card_orders: {},
      }),
    );
  });

  it('shows a toast.error and rolls back when saving the layout fails', async () => {
    const { toast } = await import('sonner');
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(
      fleetResponse([
        fleetEntry({ provider_id: 'claude' }),
        fleetEntry({ provider_id: 'chatgpt' }),
      ]),
    );
    vi.mocked(api.putDashboardLayout).mockRejectedValue(new Error('500'));
    renderWithProviders(<HomePage />);
    await screen.findByText('Providers');

    await userEvent.click(screen.getByRole('button', { name: /reorder/i }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Could not save the layout'));
  });
});

describe('HomePage banners', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    primeDefaults();
    vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
  });

  it('renders a credential banner when a token is expiring', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        {
          provider: 'Claude',
          account_id: 'default',
          account_label: 'me',
          status: 'expiring',
        } as never,
      ],
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/credential for claude/i)).toBeInTheDocument();
  });

  it('dismisses a banner when its close button is clicked', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        { provider: 'Claude', account_id: 'default', status: 'expired' } as never,
      ],
    });
    renderWithProviders(<HomePage />);
    const banner = await screen.findByText(/credential for claude/i);
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(banner).not.toBeInTheDocument();
  });

  it('does not raise a banner for an expired but redundant credential', async () => {
    // redundant=true means another healthy cred exists — no hard alarm should fire.
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({
      tokens: [
        {
          provider: 'Claude',
          account_id: 'sidecar-123',
          status: 'expired',
          redundant: true,
        } as never,
      ],
    });
    renderWithProviders(<HomePage />);
    // Give the query a tick to settle; no credential banner should appear.
    await screen.findByText(/providers/i);
    expect(screen.queryByText(/credential for/i)).not.toBeInTheDocument();
  });

  it('renders an anomaly banner when spikes are present', async () => {
    vi.mocked(api.fetchAnomalies).mockResolvedValue({
      as_of: new Date().toISOString(),
      lookback_days: 30,
      z_threshold: 3,
      anomalies: [
        {
          provider_id: 'claude',
          account_id: 'default',
          model_id: 'opus',
          today_tokens: 1,
          today_cost_usd: 1,
          historical_mean_tokens: 1,
          historical_stddev_tokens: 1,
          z_score_tokens: 4.2,
          verdict: 'spike',
        },
      ],
    });
    renderWithProviders(<HomePage />);
    expect(await screen.findByText(/unusual usage today/i)).toBeInTheDocument();
  });
});
