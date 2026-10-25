import { screen } from '@testing-library/react';
import type { HistoryWindowRow } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { WindowDetailSheet } from './WindowDetailSheet';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
let lastOption: Record<string, unknown> | null = null;
vi.mock('@/components/charts/EChart', () => ({
  EChart: ({ option }: { option: Record<string, unknown> }) => {
    lastOption = option;
    return <div data-testid="echart" />;
  },
}));
vi.mock('@/components/charts/theme', async () => {
  // The real cap helpers stay: the test asserts on the cap itself, not on a
  // copy of it. Only the tokens and the option fragments are stubbed.
  const actual = await vi.importActual<typeof import('@/components/charts/theme')>(
    '@/components/charts/theme',
  );
  return {
    ...actual,
    useChartTokens: () => ({
      series: ['#a', '#b', '#c', '#d', '#e', '#f'],
      accent: '#0af',
      fgMuted: '#888',
      axis: '#555',
    }),
    baseTooltip: () => ({}),
    baseAxisStyle: () => ({}),
  };
});

const row = (o: Partial<HistoryWindowRow> = {}): HistoryWindowRow => ({
  provider_id: 'claude',
  account_id: 'default',
  service_name: 'Claude',
  window_type: 'weekly',
  window_start: '2026-06-01T00:00:00Z',
  window_end: '2026-06-08T00:00:00Z',
  ...o,
});

describe('WindowDetailSheet', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders nothing visible when row is null', () => {
    renderWithProviders(<WindowDetailSheet row={null} onClose={vi.fn()} />);
    expect(screen.queryByText(/weekly/)).not.toBeInTheDocument();
  });

  it('renders the fill chart when detail data has points', async () => {
    vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
      fill_series: [{ ts: '2026-06-02T00:00:00Z', pct_used: 30 }],
      fill_by_model: [
        { model_id: 'opus', series: [{ ts: '2026-06-02T00:00:00Z', pct_used: 20 }] },
      ],
      by_model: [],
    });
    renderWithProviders(<WindowDetailSheet row={row()} onClose={vi.fn()} />);
    expect(await screen.findByTestId('echart')).toBeInTheDocument();
    expect(screen.getByText(/Claude · weekly/)).toBeInTheDocument();
  });

  it('shows the no-fill-data message when the series is empty', async () => {
    vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
      fill_series: [],
      fill_by_model: [],
      by_model: [],
    });
    renderWithProviders(<WindowDetailSheet row={row()} onClose={vi.fn()} />);
    expect(await screen.findByText(/no fill data recorded/i)).toBeInTheDocument();
  });

  it('shows a no-boundaries message when the window has no bounds', () => {
    renderWithProviders(
      <WindowDetailSheet row={row({ window_start: null, window_end: null })} onClose={vi.fn()} />,
    );
    expect(screen.getByText(/no boundaries for this window/i)).toBeInTheDocument();
  });

  it('renders token breakdown when tokens_total > 0', async () => {
    vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
      fill_series: [],
      fill_by_model: [],
      by_model: [],
    });
    renderWithProviders(
      <WindowDetailSheet
        row={row({
          tokens_total: 10000,
          tokens_input: 5000,
          tokens_output: 3000,
          tokens_reasoning: 500,
          tokens_cache_read: 1000,
          tokens_cache_create: 500,
        })}
        onClose={vi.fn()}
      />,
    );
    await screen.findByText(/Token breakdown/);
    expect(screen.getByText(/Input/)).toBeInTheDocument();
    expect(screen.getByText(/Output/)).toBeInTheDocument();
    expect(screen.getByText(/Cache read/)).toBeInTheDocument();
    expect(screen.getByText(/Cache create/)).toBeInTheDocument();
    expect(screen.getByText(/Reasoning/)).toBeInTheDocument();
  });

  it('renders per-model table when by_model has entries', async () => {
    vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
      fill_series: [],
      fill_by_model: [],
      by_model: [
        {
          model_id: 'claude-opus',
          tokens_total: 8000,
          tokens_input: 4000,
          tokens_output: 2000,
          tokens_cache_read: 1500,
          tokens_cache_create: 500,
          tokens_reasoning: 0,
          cost_usd: 0.5,
          cost_input: 0.3,
          cost_output: 0.15,
          cost_cache_read: 0.05,
          cost_cache_create: 0,
          msgs: 10,
        },
      ],
    });
    renderWithProviders(<WindowDetailSheet row={row()} onClose={vi.fn()} />);
    await screen.findByText(/Per-model breakdown/);
    expect(screen.getByText('claude-opus')).toBeInTheDocument();
  });

  it('caps the model lines at the palette size and says how many it left out', async () => {
    // Eight models: the palette has six colours, so the last two are left
    // out. A fill curve is a percentage of that model's OWN limit, so the
    // overflow cannot be summed into one curve — two models at 10% each
    // would draw a 20% curve that is 10% of neither.
    vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
      fill_series: [{ ts: '2026-06-02T00:00:00Z', pct_used: 30 }],
      fill_by_model: Array.from({ length: 8 }, (_, i) => ({
        model_id: `m${i + 1}`,
        series: [{ ts: '2026-06-02T00:00:00Z', pct_used: 10 }],
      })),
      by_model: [],
    });
    renderWithProviders(<WindowDetailSheet row={row()} onClose={vi.fn()} />);
    await screen.findByTestId('echart');
    const series = (lastOption as { series: { name: string; data: number[][] }[] }).series;
    // The total line and six model lines. Nothing is aggregated.
    expect(series).toHaveLength(7);
    expect(series.slice(1, 7).map((s) => s.name)).toEqual(['m1', 'm2', 'm3', 'm4', 'm5', 'm6']);
    expect(series.map((s) => s.name)).not.toContain('Other (2)');
    // And the chart says what it left out.
    expect(screen.getByText('+2 more models not shown')).toBeInTheDocument();
  });
});
