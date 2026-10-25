import { screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TopProjectEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { TopProjectsCard } from './TopProjectsCard';
import { INSIGHTS_TOP_N } from './queries';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
// The option is captured as an object as well as serialized: JSON drops the
// tooltip formatter, and the formatter is where the disclosed totals live.
const captured: { option?: Record<string, unknown> } = {};
vi.mock('@/components/charts/EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    captured.option = option;
    return <div data-testid="echart" data-option={JSON.stringify(option)} />;
  },
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

// The response carries the untruncated total and the cap that was applied, so
// the card's title can say how much of the ranking it is not showing.
function makeResponse(projects: TopProjectEntry[], metric: string, total = projects.length) {
  return {
    projects,
    metric,
    total,
    limit: INSIGHTS_TOP_N,
    generated_at: new Date().toISOString(),
  };
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
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const option = capturedOption();
    const yAxis = option.yAxis as { data: string[] };
    expect(yAxis.data).toContain('my-app');
  });

  it('names the scope in the title, and says how much of the ranking is hidden', async () => {
    // The Activity tab scopes the card to one provider, and used to override
    // the title with a bare "Top projects" that stated neither a cap nor a
    // total.
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'tokens', 34));
    renderWithProviders(
      <TopProjectsCard range={{ days: 7 }} providerId="anthropic" scopeLabel="March 2026" />,
    );
    expect(
      await screen.findByText(`Top ${INSIGHTS_TOP_N} of 34 projects · March 2026`),
    ).toBeInTheDocument();
  });

  it('default metric is tokens', async () => {
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    const tokensOption = within(metricGroup).getByRole('radio', { name: 'Tokens' });
    expect(tokensOption).toBeChecked();
    expect(api.fetchTopProjects).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'tokens' }),
    );
  });

  it('metric tab switch updates query', async () => {
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'cost'));
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    expect(api.fetchTopProjects).toHaveBeenCalledWith(
      expect.objectContaining({ metric: 'cost' }),
    );
  });

  it('projectRow maps tokens correctly', async () => {
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    // tokens_input(10000) + tokens_output(5000) + tokens_reasoning(0) + cache(0) = 15000
    expect(stackedSeriesTotal(capturedOption())).toBe(15000);
  });

  it('projectRow maps cost correctly', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'cost'));
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    await waitFor(() => expect(stackedSeriesTotal(capturedOption())).toBeCloseTo(0.15));
  });

  it('projectRow maps sessions correctly', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'sessions'));
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Sessions' }));
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
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
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
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
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
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    expect(screen.queryByTestId('echart')).not.toBeInTheDocument();
    expect(document.querySelector('.shimmer-bg')).toBeInTheDocument();
  });

  it('gives a segment the same colour in the token and the cost breakdown', async () => {
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const tokenOption = capturedOption();
    const tokenCacheRead = (
      tokenOption.series as { name: string; itemStyle?: { color: string } }[]
    ).find((s) => s.name === 'Cache read');

    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    await waitFor(() => {
      const costOption = capturedOption();
      const costCacheRead = (
        costOption.series as { name: string; itemStyle?: { color: string } }[]
      ).find((s) => s.name === 'Cache read');
      expect(costCacheRead?.itemStyle?.color).toBeTruthy();
      // "Cache read" is the same segment in both breakdowns, so it is the
      // same colour in both.
      expect(costCacheRead?.itemStyle?.color).toBe(tokenCacheRead?.itemStyle?.color);
    });
  });

  it('discloses the backend cost total when it differs from the drawn segments', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(
      // The provider reports its own total; the components add up to less.
      makeResponse([{ ...mockProject, cost_usd: 0.20 }], 'cost'),
    );
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    await waitFor(() => {
      const tooltip = captured.option!.tooltip as {
        formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
      };
      const result = tooltip.formatter([{ seriesName: 'Input', value: 0.1, dataIndex: 0 }]);
      expect(result).toContain('Total: $0.15');
      expect(result).toContain('Reported: $0.20');
    });
  });

  it('prints only the total when the backend cost total matches the drawn segments', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([mockProject], 'cost'));
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    await waitFor(() => expect(screen.getByTestId('echart')).toBeInTheDocument());
    const metricGroup = screen.getByRole('group', { name: 'Top projects metric' });
    await userEvent.click(within(metricGroup).getByRole('radio', { name: 'Cost' }));
    await waitFor(() => {
      const tooltip = captured.option!.tooltip as {
        formatter: (params: { seriesName: string; value: number; dataIndex: number }[]) => string;
      };
      const result = tooltip.formatter([{ seriesName: 'Input', value: 0.1, dataIndex: 0 }]);
      expect(result).toContain('Total: $0.15');
      expect(result).not.toContain('Reported');
    });
  });

  it('shows empty state when no data', async () => {
    vi.mocked(api.fetchTopProjects).mockResolvedValue(makeResponse([], 'tokens'));
    renderWithProviders(<TopProjectsCard range={{ days: 7 }} />);
    expect(
      await screen.findByText(/No project-attributed usage in this range/),
    ).toBeInTheDocument();
  });
});
