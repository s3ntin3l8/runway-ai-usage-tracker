import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TopSessionEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TopSessionsCard } from './TopSessionsCard';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
// Stub the ranking bar so the rows the card builds are directly readable —
// the label and the subtitle (host, host count, providers) are what this
// card is for, and they only reach the tooltip inside the real chart.
vi.mock('@/components/charts/RankBar', () => ({
  RankBar: ({
    rows,
  }: {
    rows: Array<{ label: string; value: number; sub?: string }>;
  }) => (
    <div data-testid="rankbar">
      {rows.map((row) => (
        <div key={row.label} data-testid="rankrow" data-label={row.label} data-value={row.value}>
          {row.sub}
        </div>
      ))}
    </div>
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
  hosts: 1,
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

function firstRow(): HTMLElement {
  return screen.getByTestId('rankrow');
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
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(firstRow()).toHaveAttribute('data-label', 'my-app');
  });

  it('falls back to the session id when the session has no project', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(
      makeResponse([{ ...mockSession, project: null }], 'tokens'),
    );
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(firstRow()).toHaveAttribute('data-label', '89abcdef');
  });

  it('names the host and the providers in the subtitle', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(firstRow()).toHaveTextContent('89abcdef · workstation · via anthropic');
  });

  it('says so when a session ran on more than one host', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(
      makeResponse([{ ...mockSession, hosts: 3 }], 'tokens'),
    );
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(firstRow()).toHaveTextContent('3 hosts');
  });

  it('uses the fleet display name for the host', async () => {
    vi.mocked(api.fetchSidecars).mockResolvedValue({
      sidecars: [{ sidecar_id: 'workstation', custom_name: 'My Laptop' }],
    });
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(firstRow()).toHaveTextContent('My Laptop');
  });

  it('defaults to the tokens metric', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top sessions metric' });
    expect(within(metricGroup).getByRole('radio', { name: 'Tokens' })).toBeChecked();
    expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ metric: 'tokens' }));
  });

  it('switches to the cost metric', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top sessions metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ metric: 'cost' }));
  });

  it('scopes the query to the shared range', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 1 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(api.fetchTopSessions).toHaveBeenCalledWith(expect.objectContaining({ days: 1 }));
  });

  it('maps the token totals', async () => {
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    expect(firstRow()).toHaveAttribute('data-value', '15000');
  });

  it('maps the cost totals', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(makeResponse([mockSession], 'cost'));
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('rankbar')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top sessions metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    await waitFor(() => expect(Number(firstRow().getAttribute('data-value'))).toBeCloseTo(0.15));
  });

  it('shows skeleton during loading', () => {
    vi.mocked(api.fetchTopSessions).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    expect(screen.queryByTestId('rankbar')).not.toBeInTheDocument();
    expect(document.querySelector('.shimmer-bg')).toBeInTheDocument();
  });

  it('shows empty state when no data', async () => {
    vi.mocked(api.fetchTopSessions).mockResolvedValue(makeResponse([], 'tokens'));
    renderWithProviders(<TopSessionsCard range={{ days: 7 }} />);
    expect(await screen.findByText(/No session-attributed usage in this range/)).toBeInTheDocument();
  });
});
