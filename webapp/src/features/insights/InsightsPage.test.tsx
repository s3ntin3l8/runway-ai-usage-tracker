import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { InsightsPage } from './InsightsPage';
import { INSIGHTS_TOP_N } from './queries';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
// Stub the chart so jsdom never touches canvas.
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => <div data-testid="echart" />,
}));
vi.mock('@/components/charts/theme', async () => {
  // The rest of the module stays real: the charts cap their series with the
  // helpers it exports.
  const actual = await vi.importActual<typeof import('@/components/charts/theme')>(
    '@/components/charts/theme',
  );
  return {
    ...actual,
    useChartTokens: () => ({
      series: ['#111'],
      accent: '#0af',
      fgMuted: '#888',
      axis: '#555',
      monoFamily: 'monospace',
    }),
    baseTooltip: () => ({}),
    baseAxisStyle: () => ({}),
  };
});

describe('InsightsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.fetchGlobalStats).mockResolvedValue({
      lifetime: {
        tokens_total: 0,
        tokens_input: 0,
        tokens_output: 0,
        tokens_cache_read: 0,
        tokens_cache_create: 0,
        tokens_reasoning: 0,
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
    // The rankings are empty, and each response says so: the untruncated
    // total is 0 and the cap that was applied is the usual one.
    vi.mocked(api.fetchTopModels).mockResolvedValue({
      models: [],
      metric: 'tokens',
      total: 0,
      limit: INSIGHTS_TOP_N,
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopProjects).mockResolvedValue({
      projects: [],
      metric: 'tokens',
      total: 0,
      limit: INSIGHTS_TOP_N,
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopTools).mockResolvedValue({
      tools: [],
      total: 0,
      limit: INSIGHTS_TOP_N,
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopSessions).mockResolvedValue({
      sessions: [],
      metric: 'tokens',
      total: 0,
      limit: INSIGHTS_TOP_N,
      generated_at: new Date().toISOString(),
    });
    vi.mocked(api.fetchTopHosts).mockResolvedValue({
      hosts: [],
      metric: 'tokens',
      total: 0,
      limit: INSIGHTS_TOP_N,
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
    // The mocked rankings are empty, so the title says there is nothing —
    // a title that read "Top 12 sessions" would claim twelve that do not
    // exist.
    expect(await screen.findByText(/No sessions · Last 7 days/)).toBeInTheDocument();
    expect(screen.getByText(/No hosts · Last 7 days/)).toBeInTheDocument();
    // The lifetime strip stays lifetime-scoped, and says so next to the
    // numbers — the range picker in the header does not apply to it.
    expect(screen.getByText('Global insights')).toBeInTheDocument();
    expect(screen.getByText(/all time — not affected by the range filter/i)).toBeInTheDocument();
  });

  it('re-scopes the new rankings when the last-24-hours range is picked', async () => {
    renderWithProviders(<InsightsPage />);
    await screen.findByText(/No sessions · Last 7 days/);

    await userEvent.click(screen.getByRole('button', { name: 'Last 7 days' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Last 24 hours' }));

    await waitFor(() => {
      expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
      expect(api.fetchTopHosts).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
    });
    expect(await screen.findByText(/No sessions · Last 24 hours/)).toBeInTheDocument();
    expect(screen.getByText(/No hosts · Last 24 hours/)).toBeInTheDocument();
  });
  it('shows an error with a retry when a ranking request fails', async () => {
    // A failed request must not read as "there is no data".
    vi.mocked(api.fetchTopModels).mockRejectedValue(new Error('boom'));
    renderWithProviders(<InsightsPage />);
    expect(await screen.findByText(/could not load this data/i)).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
});
