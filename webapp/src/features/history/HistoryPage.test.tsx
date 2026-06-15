import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type {
  FleetEntry,
  FleetResponse,
  HistoryChartResponse,
  HistoryDeltas,
  HistoryWindowRow,
  LimitCard,
} from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { HistoryPage } from './HistoryPage';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
// Stub the chart so jsdom never touches canvas; the option-builder still runs
// because HistoryChart imports this same module.
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => <div data-testid="echart" />,
}));
// useChartTokens reads the theme context; stub the theme bridge so the page
// renders without a ThemeProvider wrapper.
vi.mock('@/components/charts/theme', () => ({
  useChartTokens: () => ({ series: ['#111'], accent: '#0af', fgMuted: '#888', axis: '#555' }),
  baseTooltip: () => ({}),
  baseAxisStyle: () => ({}),
}));

const card = (o: Partial<LimitCard> = {}): LimitCard => ({
  service_name: 'Claude',
  account_label: 'me',
  pct_used: 50,
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

const chartWithData: HistoryChartResponse = {
  series: [
    {
      key: 'claude:weekly',
      provider_id: 'claude',
      window_type: 'weekly',
      model_id: '',
      label: 'Claude weekly',
      points: [{ ts: '2026-06-10T00:00:00Z', pct_used: 40 }],
    },
  ],
};

const deltas: HistoryDeltas = {
  token_delta_total: 12345,
  cost_delta_total: 6.5,
  critical_series_count: 2,
};

const windowRow = (o: Partial<HistoryWindowRow> = {}): HistoryWindowRow => ({
  provider_id: 'claude',
  account_id: 'default',
  service_name: 'Claude',
  window_type: 'weekly',
  window_start: '2026-06-01T00:00:00Z',
  window_end: '2026-06-08T00:00:00Z',
  is_open: false,
  pct_used: 75,
  tokens_total: 99999,
  cost_usd: 3.2,
  top_model: 'opus',
  ...o,
});

function primeDefaults() {
  vi.mocked(api.fetchFleetUsage).mockResolvedValue(fleetResponse([fleetEntry()]));
  vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
    providers: [{ provider_id: 'claude', name: 'Claude' } as never],
  });
  vi.mocked(api.fetchAnomalies).mockResolvedValue({
    as_of: new Date().toISOString(),
    lookback_days: 30,
    z_threshold: 3,
    anomalies: [],
  });
  vi.mocked(api.fetchHistoryChart).mockResolvedValue(chartWithData);
  vi.mocked(api.fetchHistoryDeltas).mockResolvedValue(deltas);
  vi.mocked(api.fetchHistoryWindows).mockResolvedValue({ windows: [windowRow()] });
  vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
    fill_series: [],
    fill_by_model: [],
  });
}

describe('HistoryPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    primeDefaults();
  });

  it('renders the chart and the delta stat tiles', async () => {
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByTestId('echart')).toBeInTheDocument();
    expect(screen.getByText(/Tokens \(7d\)/)).toBeInTheDocument();
    expect(screen.getByText(/Cost \(7d\)/)).toBeInTheDocument();
    expect(screen.getByText('Critical series')).toBeInTheDocument();
  });

  it('renders a window row in the quota-windows table', async () => {
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByText('Quota windows')).toBeInTheDocument();
    // Service column shows the provider name; top_model shows in a column.
    expect(await screen.findByText('opus')).toBeInTheDocument();
  });

  it('opens the window detail sheet when a row is clicked', async () => {
    renderWithProviders(<HistoryPage />);
    const cell = await screen.findByText('opus');
    const row = cell.closest('tr')!;
    await userEvent.click(row);
    // Sheet title = "Claude · weekly".
    expect(await screen.findByText(/Claude · weekly/)).toBeInTheDocument();
  });

  it('shows the no-data state when the chart has no points', async () => {
    vi.mocked(api.fetchHistoryChart).mockResolvedValue({ series: [], bars: [] });
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByText(/no data points in this range/i)).toBeInTheDocument();
  });

  it('shows the empty windows state when none are returned', async () => {
    vi.mocked(api.fetchHistoryWindows).mockResolvedValue({ windows: [] });
    renderWithProviders(<HistoryPage />);
    expect(await screen.findByText(/no windows in this range/i)).toBeInTheDocument();
  });

  it('switches the time range, refetching the chart for the new window', async () => {
    renderWithProviders(<HistoryPage />);
    await screen.findByTestId('echart');
    await userEvent.click(screen.getByRole('tab', { name: '30d' }));
    // The deltas/chart hooks key on `days`; a 30d refetch must fire.
    expect(api.fetchHistoryDeltas).toHaveBeenCalledWith(
      expect.objectContaining({ days: 30 }),
    );
  });

  it('switches the metric to tokens', async () => {
    renderWithProviders(<HistoryPage />);
    await screen.findByTestId('echart');
    // Scope to the chart's metric toggle — the Top Models card has its own
    // "Tokens" tab, so an unscoped query would be ambiguous.
    const metricTabs = screen.getByRole('tablist', { name: 'Chart metric' });
    await userEvent.click(within(metricTabs).getByRole('tab', { name: 'Tokens' }));
    expect(api.fetchHistoryChart).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'tokens' }),
    );
  });

  it('renders the anomalies table when spikes exist', async () => {
    vi.mocked(api.fetchAnomalies).mockResolvedValue({
      as_of: new Date().toISOString(),
      lookback_days: 30,
      z_threshold: 3,
      anomalies: [
        {
          provider_id: 'claude',
          account_id: 'default',
          model_id: 'opus',
          today_tokens: 1000,
          today_cost_usd: 2,
          historical_mean_tokens: 100,
          historical_stddev_tokens: 10,
          z_score_tokens: 5.4,
          verdict: 'spike',
        },
      ],
    });
    renderWithProviders(<HistoryPage />);
    const heading = await screen.findByText(/anomalies \(today vs/i);
    const table = heading.closest('div')!.parentElement!;
    expect(within(table).getByText(/5\.4σ/)).toBeInTheDocument();
  });
});
