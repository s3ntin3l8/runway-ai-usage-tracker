import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { InsightsPage } from './InsightsPage';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
// Stub the chart so jsdom never touches canvas.
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => <div data-testid="echart" />,
}));
vi.mock('@/components/charts/theme', () => ({
  useChartTokens: () => ({
    series: ['#111'],
    accent: '#0af',
    fgMuted: '#888',
    axis: '#555',
    monoFamily: 'monospace',
  }),
  baseTooltip: () => ({}),
  baseAxisStyle: () => ({}),
}));

describe('InsightsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.fetchGlobalStats).mockResolvedValue({
      lifetime: {
        tokens_total: 0,
        tokens_cache: 0,
        cost_usd: 0,
        cost_cache: 0,
        msgs: 0,
      },
      sessions: { count: 0, avg_cost: 0, avg_tokens: 0 },
      cache_hit_ratio: 0,
      distinct_models: 0,
      distinct_providers: 0,
      busiest_day: null,
      busiest_hour: null,
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchHistoryChart).mockResolvedValue({ bars: [] });
    vi.mocked(api.fetchHistoryDeltas).mockResolvedValue({} as never);
    vi.mocked(api.fetchTopModels).mockResolvedValue({
      models: [],
      metric: 'tokens',
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopProjects).mockResolvedValue({
      projects: [],
      metric: 'tokens',
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopTools).mockResolvedValue({
      tools: [],
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopSessions).mockResolvedValue({
      sessions: [],
      metric: 'tokens',
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopHosts).mockResolvedValue({
      hosts: [],
      metric: 'tokens',
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
    vi.mocked(api.fetchUsageSources).mockResolvedValue({
      sidecar_ids: [],
      source_apps: [],
    });
  });

  it('shows the session and host rankings next to the other insights', async () => {
    renderWithProviders(<InsightsPage />);
    expect(await screen.findByText(/Top sessions · Last 7 days/)).toBeInTheDocument();
    expect(screen.getByText(/Top hosts · Last 7 days/)).toBeInTheDocument();
    // The lifetime strip stays lifetime-scoped.
    expect(screen.getByText(/Global insights · All time/)).toBeInTheDocument();
  });

  it('re-scopes the new rankings when the last-24-hours range is picked', async () => {
    renderWithProviders(<InsightsPage />);
    await screen.findByText(/Top sessions · Last 7 days/);

    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Last 24 hours' }));

    await waitFor(() => {
      expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
      expect(api.fetchTopHosts).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
    });
    expect(await screen.findByText(/Top sessions · Last 24 hours/)).toBeInTheDocument();
    expect(screen.getByText(/Top hosts · Last 24 hours/)).toBeInTheDocument();
  });
});
