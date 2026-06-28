import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';
import { ForecastTab } from './ForecastTab';
import * as api from '@/api/endpoints';
import {
  anomaliesResponse,
  costForecast,
  fleetEntry,
  forecastEntry,
  forecastResponse,
  limitCard,
} from './test-fixtures';

vi.mock('@/api/endpoints');
vi.mock('@/components/charts/TrajectoryChart', () => ({
  TrajectoryChart: () => <div data-testid="trajectory" />,
}));

function mockAll() {
  vi.mocked(api.fetchForecast).mockResolvedValue(forecastResponse([forecastEntry()]));
  vi.mocked(api.fetchCostForecast).mockResolvedValue(costForecast());
  vi.mocked(api.fetchAnomalies).mockResolvedValue(anomaliesResponse());
  vi.mocked(api.fetchWindowHistory).mockResolvedValue({ windows: [] } as never);
}

describe('ForecastTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAll();
  });

  it('renders the trajectory with status, now and projected', async () => {
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    expect(await screen.findByText('Trajectory')).toBeInTheDocument();
    expect(await screen.findByTestId('trajectory')).toBeInTheDocument();
    expect(screen.getByText(/samples/)).toBeInTheDocument();
  });

  it('renders the cost outlook with spend and projection', async () => {
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    expect(await screen.findByText('Cost outlook')).toBeInTheDocument();
    expect(await screen.findByText(/spent/)).toBeInTheDocument();
    expect(screen.getAllByText(/projected/).length).toBeGreaterThan(0);
  });

  it('shows the no-anomalies empty state by default', async () => {
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    expect(await screen.findByText(/no usage anomalies detected/i)).toBeInTheDocument();
  });

  it('renders the anomalies table when spikes exist', async () => {
    vi.mocked(api.fetchAnomalies).mockResolvedValue(
      anomaliesResponse({
        anomalies: [
          {
            provider_id: 'anthropic',
            account_id: 'me@example.com',
            model_id: 'claude-opus',
            today_tokens: 50000,
            today_cost_usd: 5,
            historical_mean_tokens: 1000,
            historical_stddev_tokens: 200,
            z_score_tokens: 4.2,
            verdict: 'spike',
          },
        ],
      }),
    );
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    expect(await screen.findByText('claude-opus')).toBeInTheDocument();
    expect(screen.getByText(/4\.2σ/)).toBeInTheDocument();
  });

  it('offers a window selector when multiple forecasts exist', async () => {
    vi.mocked(api.fetchForecast).mockResolvedValue(
      forecastResponse([
        forecastEntry({ window_type: 'weekly', service_name: 'Weekly' }),
        forecastEntry({ window_type: 'daily', service_name: 'Daily' }),
      ]),
    );
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    const combo = await screen.findByRole('combobox');
    expect(combo).toBeInTheDocument();
    await userEvent.click(combo);
    expect(await screen.findAllByText(/daily/i)).not.toHaveLength(0);
  });

  it('distinguishes forecasts by model_id in the picker labels', async () => {
    vi.mocked(api.fetchForecast).mockResolvedValue(
      forecastResponse([
        forecastEntry({ window_type: 'weekly', service_name: 'Claude', model_id: 'sonnet' }),
        forecastEntry({ window_type: 'weekly', service_name: 'Claude', model_id: 'opus' }),
      ]),
    );
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    const combo = await screen.findByRole('combobox');
    await userEvent.click(combo);
    // The selected label also shows in the trigger, so both can appear twice.
    expect((await screen.findAllByText('Claude · sonnet · weekly')).length).toBeGreaterThan(0);
    expect((await screen.findAllByText('Claude · opus · weekly')).length).toBeGreaterThan(0);
  });

  it('defaults to the critical gauge pool, not the first (empty) forecast', async () => {
    // Antigravity shape: the empty frontier pool sorts first in the response.
    // The default selection must follow the critical gauge's variant (gemini),
    // not forecasts[0], so the data-rich trajectory shows by default.
    vi.mocked(api.fetchForecast).mockResolvedValue(
      forecastResponse([
        forecastEntry({
          window_type: 'weekly',
          variant: 'frontier',
          service_name: 'Frontier',
          status: 'insufficient_data',
        }),
        forecastEntry({
          window_type: 'weekly',
          variant: 'gemini',
          service_name: 'Gemini',
          status: 'risk',
        }),
      ]),
    );
    const entry = fleetEntry({
      critical_gauge: limitCard({ window_type: 'weekly', variant: 'gemini', pct_used: 49 }),
    });
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={entry} />,
    );
    // The selected forecast's status badge reflects the gemini pool ('risk'),
    // not the frontier pool's 'insufficient data'.
    expect(await screen.findByText('risk')).toBeInTheDocument();
    expect(screen.queryByText('insufficient data')).not.toBeInTheDocument();
  });

  it('shows the no-forecast empty state', async () => {
    vi.mocked(api.fetchForecast).mockResolvedValue(forecastResponse([]));
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    expect(await screen.findByText(/no forecast available yet/i)).toBeInTheDocument();
  });

  it('renders closed windows in the history table', async () => {
    vi.mocked(api.fetchWindowHistory).mockResolvedValue({
      windows: [
        {
          window_start: '2026-06-01T00:00:00Z',
          window_end: '2026-06-08T00:00:00Z',
          totals: { msgs: 42, tokens_input: 1000, tokens_output: 500, cost_usd: 3.5 },
        },
      ],
    } as never);
    renderWithProviders(
      <ForecastTab providerId="anthropic" accountId="me@example.com" entry={fleetEntry()} />,
    );
    expect(await screen.findByText('42')).toBeInTheDocument();
  });
});
