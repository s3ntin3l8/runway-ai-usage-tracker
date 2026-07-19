import { screen, within } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import { OverviewTab } from './OverviewTab';
import * as api from '@/api/endpoints';
import {
  anomaliesResponse,
  costForecast,
  cumulativeResponse,
  emptyCumulative,
  emptyEvents,
  fleetEntry,
  forecastEntry,
  forecastResponse,
  historyChart,
  limitCard,
  session,
} from './test-fixtures';

vi.mock('@/api/endpoints');

// Chart leaves render ECharts (no canvas in jsdom): stub to a marker.
vi.mock('@/components/charts/TrajectoryChart', () => ({
  TrajectoryChart: () => <div data-testid="trajectory" />,
}));
vi.mock('@/components/charts/TokenDonut', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/components/charts/TokenDonut')>()),
  TokenDonut: () => <div data-testid="token-donut" />,
}));
vi.mock('@/components/charts/ModelDonut', () => ({
  ModelDonut: () => <div data-testid="model-donut" />,
}));
vi.mock('@/features/history/HistoryChart', () => ({
  HistoryChart: () => <div data-testid="history-chart" />,
}));

function mockAll() {
  vi.mocked(api.fetchForecast).mockResolvedValue(forecastResponse([forecastEntry()]));
  vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
  vi.mocked(api.fetchCumulative).mockResolvedValue(cumulativeResponse());
  vi.mocked(api.fetchAnomalies).mockResolvedValue(anomaliesResponse());
  vi.mocked(api.fetchEvents).mockResolvedValue(emptyEvents());
  vi.mocked(api.fetchHistoryChart).mockResolvedValue(historyChart(true));
  vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [] } as never);
}

describe('OverviewTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAll();
  });

  it('renders the KPI strip and quota windows', async () => {
    renderWithProviders(<OverviewTab entry={fleetEntry()} />);
    expect(await screen.findByText('Quota windows')).toBeInTheDocument();
    expect(screen.getByText('Current window')).toBeInTheDocument();
    // KPI labels
    expect(screen.getByText('Current')).toBeInTheDocument();
    expect(screen.getByText('Cache hit')).toBeInTheDocument();
  });

  it('shows the trajectory chart once a forecast resolves', async () => {
    renderWithProviders(<OverviewTab entry={fleetEntry()} />);
    expect(await screen.findAllByTestId('trajectory')).not.toHaveLength(0);
  });

  it('selects the critical gauge variant when pools share a window_type', async () => {
    // Antigravity shape: two pools (gemini/frontier) share window_type 'weekly'.
    // The empty frontier pool sorts first; a window_type-only match would pick
    // it (insufficient_data, no projection). findForecast must match the gauge's
    // own variant and surface the data-rich gemini forecast.
    vi.mocked(api.fetchForecast).mockResolvedValue(
      forecastResponse([
        forecastEntry({
          window_type: 'weekly',
          variant: 'frontier',
          status: 'insufficient_data',
          projected_pct: null,
        }),
        forecastEntry({
          window_type: 'weekly',
          variant: 'gemini',
          status: 'risk',
          projected_pct: 88,
        }),
      ]),
    );
    const entry = fleetEntry({
      critical_gauge: limitCard({ window_type: 'weekly', variant: 'gemini', pct_used: 49 }),
    });
    renderWithProviders(<OverviewTab entry={entry} />);
    // "Current window" header (OverviewTab) resolves the gemini forecast…
    expect(await screen.findByText(/projected 88% at reset/i)).toBeInTheDocument();
    // …and so does the "Projected at reset" KPI tile (ProviderKpis), which uses
    // the same selection logic and would otherwise show '—' for the empty pool.
    expect(await screen.findByText('88%')).toBeInTheDocument();
  });

  it('renders the token-mix donut when there is month usage', async () => {
    renderWithProviders(<OverviewTab entry={fleetEntry()} />);
    expect(await screen.findByText('Token mix (month)')).toBeInTheDocument();
    expect(await screen.findAllByTestId('token-donut')).not.toHaveLength(0);
  });

  it('falls back to an empty token-mix message with no month bucket', async () => {
    vi.mocked(api.fetchCumulative).mockResolvedValue(emptyCumulative());
    renderWithProviders(<OverviewTab entry={fleetEntry()} />);
    expect(await screen.findByText(/no usage this month/i)).toBeInTheDocument();
  });

  it('respects the exclude-cache toggle in the tokens-kind "Token usage" total', async () => {
    // Same excludeCache-respecting total as the ProviderKpis "Tokens (total)" tile
    // rendered above it — scope queries to this card so the two "1K"s don't collide.
    const tokenEntry = fleetEntry({
      critical_gauge: limitCard({
        pct_used: undefined,
        is_unlimited: true,
        token_usage: { input: 100, output: 50, reasoning: 10, cache_read: 700, cache_create: 140 },
      }),
    });

    localStorage.setItem('runway_exclude_cache', '0');
    const { unmount } = renderWithProviders(<OverviewTab entry={tokenEntry} />);
    const cardOff = (await screen.findByText('Token usage')).closest('.rounded-md') as HTMLElement;
    expect(within(cardOff).getByText('1K')).toBeInTheDocument();
    unmount();

    localStorage.setItem('runway_exclude_cache', '1');
    renderWithProviders(<OverviewTab entry={tokenEntry} />);
    const cardOn = (await screen.findByText('Token usage')).closest('.rounded-md') as HTMLElement;
    expect(within(cardOn).getByText('160')).toBeInTheDocument();
  });

  it('renders secondary limit rows in the quota card', async () => {
    const entry = fleetEntry({
      secondary_limits: [limitCard({ service_name: 'Sonnet', window_type: 'daily', pct_used: 10 })],
    });
    renderWithProviders(<OverviewTab entry={entry} />);
    // Both critical + secondary cards render gauges; just assert the card body exists.
    expect(await screen.findByText('Quota windows')).toBeInTheDocument();
  });

  it('uses the per-model active-window split when one sidecar feeds it', async () => {
    const entry = fleetEntry({
      window_aggregations: {
        longest: {
          window_type: 'weekly',
          window_start: '2026-06-01T00:00:00Z',
          window_end: '2026-06-08T00:00:00Z',
          token_usage: { input: 1, output: 1, total: 2 } as never,
          by_model: { 'claude-opus': { tokens_input: 100, msgs: 2 } },
          by_sidecar: { laptop: { tokens_input: 100, msgs: 2 } },
        },
      },
    });
    renderWithProviders(<OverviewTab entry={entry} />);
    expect(await screen.findByText('Active window by model')).toBeInTheDocument();
  });

  it('switches to the per-source split with more than one sidecar', async () => {
    const entry = fleetEntry({
      window_aggregations: {
        longest: {
          window_type: 'weekly',
          window_start: '2026-06-01T00:00:00Z',
          window_end: '2026-06-08T00:00:00Z',
          token_usage: { input: 1, output: 1, total: 2 } as never,
          by_model: { 'claude-opus': { tokens_input: 100 } },
          by_sidecar: { laptop: { tokens_input: 50 }, desktop: { tokens_input: 50 } },
        },
      },
    });
    renderWithProviders(<OverviewTab entry={entry} />);
    expect(await screen.findByText('Active window by source')).toBeInTheDocument();
  });

  it('renders recent sessions when present', async () => {
    vi.mocked(api.fetchSessions).mockResolvedValue({ sessions: [session()] } as never);
    renderWithProviders(<OverviewTab entry={fleetEntry()} />);
    expect(await screen.findByText('Recent sessions')).toBeInTheDocument();
  });
});
