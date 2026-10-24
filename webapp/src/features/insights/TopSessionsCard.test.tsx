import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TopSessionEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TopSessionsCard } from './TopSessionsCard';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('@/components/charts/EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => (
    <div data-testid="echart" data-option={JSON.stringify(option)} />
  ),
}));
vi.mock('@/components/charts/theme', () => ({
  useChartTokens: () => ({
    series: ['#e11d48', '#2563eb', '#16a34a', '#ca8a04', '#9333ea'],
    fgMuted: '#9ca3af',
    monoFamily: 'monospace',
  }),
  baseTooltip: () => ({}),
  baseAxisStyle: () => ({}),
}));

const mockSession: TopSessionEntry = {
  session_id: 'mvs_0123456789abcdef',
  project: 'my-app',
  cwd: '/repo/my-app',
  sidecar_id: 'workstation',
  ts_start: '2026-05-21T10:00:00+00:00',
  ts_end: '2026-05-21T12:00:00+00:00',
  msgs: 10,
  tokens_total: 15000,
  tokens_input: 10000,
  tokens_output: 5000,
  tokens_cache_read: 0,
  tokens_cache_create: 0,
  tokens_reasoning: 0,
  cost_usd: 0.15,
  cost_cache: 0,
  cost_input: 0.1,
  cost_output: 0.05,
  cost_cache_read: 0,
  cost_cache_create: 0,
  providers: ['anthropic'],
  models: ['opus-4.8'],
};

function makeResponse(sessions: TopSessionEntry[], metric: string) {
  return { sessions, metric, generated_at: new Date().toISOString() };
}

function capturedOption(): Record<string, unknown> {
  const el = screen.getByTestId('echart');
  return JSON.parse(el.getAttribute('data-option')!);
}

function stackedSeriesTotal(option: Record<string, unknown>): number {
  const series = option.series as Array<{ data: number[] }>;
  return series.reduce((sum, s) => sum + (s.data[0] ?? 0), 0);
}

describe('TopSessionsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.fetchTopSessions).mockResolvedValue(makeResponse([mockSession], 'tokens'));
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
  });

  it('renders the session project as the row label', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const yAxis = capturedOption().yAxis as { data: string[] };
    expect(yAxis.data).toContain('my-app');
  });

  it('falls back to the session id when the session has no project', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(
      makeResponse([{ ...mockSession, project: null }], 'tokens'),
    );
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const yAxis = capturedOption().yAxis as { data: string[] };
    expect(yAxis.data).toContain('89abcdef');
  });

  it('defaults to the tokens metric', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top sessions metric' });
    expect(within(tabs).getByRole('tab', { name: 'Tokens' })).toHaveAttribute('data-state', 'active');
    expect(api.fetchTopSessions).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'tokens' }),
    );
  });

  it('switches to the cost metric', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top sessions metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ metric: 'cost' }));
  });

  it('scopes the query to the shared range', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 1 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
  });

  it('maps the token totals', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    expect(stackedSeriesTotal(capturedOption())).toBe(15000);
  });

  it('maps the cost totals', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(makeResponse([mockSession], 'cost'));
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top sessions metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    await waitFor(() => expect(stackedSeriesTotal(capturedOption())).toBeCloseTo(0.15));
  });

  it('shows skeleton during loading', () => {
    vi.mocked(api.fetchTopSessions).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    expect(screen.queryByTestId('echart')).not.toBeInTheDocument();
    expect(document.querySelector('.shimmer-bg')).toBeInTheDocument();
  });

  it('shows empty state when no data', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(makeResponse([], 'tokens'));
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    expect(await screen.findByText(/No session-attributed usage in this range/)).toBeInTheDocument();
  });
});
