import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TopHostEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TopHostsCard } from './TopHostsCard';
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

const mockHost: TopHostEntry = {
  sidecar_id: 'workstation',
  ts_start: '2026-05-21T10:00:00+00:00',
  ts_end: '2026-05-21T12:00:00+00:00',
  msgs: 10,
  sessions: 3,
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
};

function makeResponse(hosts: TopHostEntry[], metric: string) {
  return { hosts, metric, generated_at: new Date().toISOString() };
}

function capturedOption(): Record<string, unknown> {
  const el = screen.getByTestId('echart');
  return JSON.parse(el.getAttribute('data-option')!);
}

function stackedSeriesTotal(option: Record<string, unknown>): number {
  const series = option.series as Array<{ data: number[] }>;
  return series.reduce((sum, s) => sum + (s.data[0] ?? 0), 0);
}

describe('TopHostsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.fetchTopHosts).mockResolvedValue(makeResponse([mockHost], 'tokens'));
    vi.mocked(api.fetchSidecars).mockResolvedValue({ sidecars: [] });
  });

  it('labels a registered host with its fleet name', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'workstation', custom_name: 'My Laptop' }],
    });
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const yAxis = capturedOption().yAxis as { data: string[] };
    expect(yAxis.data).toContain('My Laptop');
  });

  it('falls back to the raw host id for an unregistered host', async () => {
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const yAxis = capturedOption().yAxis as { data: string[] };
    expect(yAxis.data).toContain('workstation');
  });

  it('defaults to the tokens metric', async () => {
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top hosts metric' });
    expect(within(tabs).getByRole('tab', { name: 'Tokens' })).toHaveAttribute('data-state', 'active');
    expect(api.fetchTopHosts).toHaveBeenCalledWith(expect.objectContaining({ metric: 'tokens' }));
  });

  it('switches to the cost metric', async () => {
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top hosts metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    expect(api.fetchTopHosts).toHaveBeenCalledWith(expect.objectContaining({ metric: 'cost' }));
  });

  it('scopes the query to the shared range', async () => {
    renderWithProviders(<TopHostsCard range={{ days: 1 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    expect(api.fetchTopHosts).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
  });

  it('maps the token totals', async () => {
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    expect(stackedSeriesTotal(capturedOption())).toBe(15000);
  });

  it('maps the cost totals', async () => {
    vi.mocked(api.fetchTopHosts).mockResolvedValue(makeResponse([mockHost], 'cost'));
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top hosts metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    await waitFor(() => expect(stackedSeriesTotal(capturedOption())).toBeCloseTo(0.15));
  });

  it('shows skeleton during loading', () => {
    vi.mocked(api.fetchTopHosts).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    expect(screen.queryByTestId('echart')).not.toBeInTheDocument();
    expect(document.querySelector('.shimmer-bg')).toBeInTheDocument();
  });

  it('shows empty state when no data', async () => {
    vi.mocked(api.fetchTopHosts).mockResolvedValue(makeResponse([], 'tokens'));
    renderWithProviders(<TopHostsCard range={{ days: 7 }} />);
    expect(await screen.findByText(/No host-attributed usage in this range/)).toBeInTheDocument();
  });
});
