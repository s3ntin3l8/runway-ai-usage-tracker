import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TopProjectEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TopProjectsCard } from './TopProjectsCard';
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

const mockProject: TopProjectEntry = {
  project: 'my-app',
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
  cost_input: 0.10,
  cost_output: 0.05,
  cost_cache_read: 0,
  cost_cache_create: 0,
  providers: ['anthropic'],
};

const mockProjectWithCache: TopProjectEntry = {
  ...mockProject,
  project: 'cache-app',
  tokens_cache_read: 2000,
  tokens_cache_create: 1000,
  cost_cache: 0.03,
  cost_cache_read: 0.02,
  cost_cache_create: 0.01,
};

function makeResponse(projects: TopProjectEntry[], metric: string) {
  return { projects, metric, generated_at: new Date().toISOString() };
}

function capturedOption(): Record<string, unknown> {
  const el = screen.getByTestId('echart');
  return JSON.parse(el.getAttribute('data-option')!);
}

function stackedSeriesTotal(option: Record<string, unknown>): number {
  const series = option.series as Array<{ data: number[] }>;
  return series.reduce((sum, s) => sum + (s.data[0] ?? 0), 0);
}

describe('TopProjectsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'tokens'));
  });

  it('renders RankBar with correct rows after data loads', async () => {
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const option = capturedOption();
    const yAxis = option.yAxis as { data: string[] };
    expect(yAxis.data).toContain('my-app');
  });

  it('default metric is tokens', async () => {
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top projects metric' });
    const tokensTab = within(tabs).getByRole('tab', { name: 'Tokens' });
    expect(tokensTab).toHaveAttribute('data-state', 'active');
    expect(api.fetchTopProjects).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'tokens' }),
    );
  });

  it('metric tab switch updates query', async () => {
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'cost'));
    const tabs = screen.getByRole('tablist', { name: 'Top projects metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    expect(api.fetchTopProjects).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'cost' }),
    );
  });

  it('projectRow maps tokens correctly', async () => {
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    // tokens_input(10000) + tokens_output(5000) + tokens_reasoning(0) + cache(0) = 15000
    expect(stackedSeriesTotal(capturedOption())).toBe(15000);
  });

  it('projectRow maps cost correctly', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'cost'));
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top projects metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    await waitFor(() => expect(stackedSeriesTotal(capturedOption())).toBeCloseTo(0.15));
  });

  it('projectRow maps sessions correctly', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'sessions'));
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top projects metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Sessions' }));
    await waitFor(() => {
      const option = capturedOption();
      const series = option.series as Array<{ data: number[] }>;
      expect(series[0].data).toContain(3);
    });
  });

  it('excludeCache zeros token cache segments', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(
      makeResponse([mockProjectWithCache], 'tokens'),
    );
    localStorage.setItem('runway_exclude_cache', '1');
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const option = capturedOption();
    const series = option.series as Array<{ name: string; data: number[] }>;
    const cacheReadSeg = series.find((s) => s.name === 'Cache read');
    const cacheCreateSeg = series.find((s) => s.name === 'Cache create');
    expect(cacheReadSeg?.data[0]).toBe(0);
    expect(cacheCreateSeg?.data[0]).toBe(0);
    expect(series.find((s) => s.name === 'Input')?.data[0]).toBe(10000);
    expect(series.find((s) => s.name === 'Output')?.data[0]).toBe(5000);
    // Total = input + output + reasoning (no cache) = 15000
    expect(stackedSeriesTotal(option)).toBe(15000);
  });

  it('excludeCache zeros cost cache segments', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(
      makeResponse([mockProjectWithCache], 'cost'),
    );
    localStorage.setItem('runway_exclude_cache', '1');
    renderWithProviders(<TopProjectsCard days={7} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tabs = screen.getByRole('tablist', { name: 'Top projects metric' });
    await userEvent.click(within(tabs).getByRole('tab', { name: 'Cost' }));
    await waitFor(() => {
      const option = capturedOption();
      const series = option.series as Array<{ name: string; data: number[] }>;
      const cacheReadSeg = series.find((s) => s.name === 'Cache read');
      const cacheCreateSeg = series.find((s) => s.name === 'Cache create');
      expect(cacheReadSeg?.data[0]).toBe(0);
      expect(cacheCreateSeg?.data[0]).toBe(0);
      expect(series.find((s) => s.name === 'Input')?.data[0]).toBeCloseTo(0.10);
      expect(series.find((s) => s.name === 'Output')?.data[0]).toBeCloseTo(0.05);
    });
  });

  it('shows skeleton during loading', () => {
    vi.mocked(api.fetchTopProjects).mockReturnValue(new Promise(() => {}));
    renderWithProviders(<TopProjectsCard days={7} />);
    expect(screen.queryByTestId('echart')).not.toBeInTheDocument();
    expect(document.querySelector('.shimmer-bg')).toBeInTheDocument();
  });

  it('shows empty state when no data', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([], 'tokens'));
    renderWithProviders(<TopProjectsCard days={7} />);
    expect(
      await screen.findByText(/No project-attributed usage in this range/),
    ).toBeInTheDocument();
  });
});
