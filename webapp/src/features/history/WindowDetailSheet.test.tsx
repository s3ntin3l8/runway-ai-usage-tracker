import { screen } from '@testing-library/react';
import type { HistoryWindowRow } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { WindowDetailSheet } from './WindowDetailSheet';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('@/components/charts/EChart', () => ({
  EChart: () => <div data-testid="echart" />,
}));
vi.mock('@/components/charts/theme', () => ({
  useChartTokens: () => ({ series: ['#a', '#b'], accent: '#0af', fgMuted: '#888', axis: '#555' }),
  baseTooltip: () => ({}),
  baseAxisStyle: () => ({}),
}));

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
    });
    renderWithProviders(<WindowDetailSheet row={row()} onClose={vi.fn()} />);
    expect(await screen.findByTestId('echart')).toBeInTheDocument();
    expect(screen.getByText(/Claude · weekly/)).toBeInTheDocument();
  });

  it('shows the no-fill-data message when the series is empty', async () => {
    vi.mocked(api.fetchHistoryWindowDetail).mockResolvedValue({
      fill_series: [],
      fill_by_model: [],
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
});
