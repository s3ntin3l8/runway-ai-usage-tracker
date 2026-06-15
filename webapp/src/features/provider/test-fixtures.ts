// Shared test fixtures for provider-feature tests.
import { resolvePeriod, type SelectedPeriod } from './period';
import type {
  AnomaliesResponse,
  CostForecastResponse,
  CumulativeResponse,
  EventsResponse,
  FleetEntry,
  FleetResponse,
  ForecastEntry,
  ForecastResponse,
  HeatmapResponse,
  HistoryChartResponse,
  LimitCard,
  SessionEntry,
} from '@/api/types';

// The current-month selection (default the tabs receive). Pass a 'YYYY-MM' key
// for a past month to exercise the scoped-range code paths.
export const currentPeriod = (): SelectedPeriod => resolvePeriod(null);
export const pastPeriod = (key = '2026-01'): SelectedPeriod => resolvePeriod(key);

export const limitCard = (o: Partial<LimitCard> = {}): LimitCard => ({
  service_name: 'Claude',
  provider_id: 'anthropic',
  account_id: 'me@example.com',
  account_label: 'Primary',
  window_type: 'weekly',
  pct_used: 42,
  reset_at: new Date(Date.now() + 86_400_000).toISOString(),
  ...o,
});

export const fleetEntry = (o: Partial<FleetEntry> = {}): FleetEntry => ({
  provider_id: 'anthropic',
  account_id: 'me@example.com',
  critical_gauge: limitCard(),
  secondary_limits: [],
  ...o,
});

export const fleetResponse = (entries: FleetEntry[]): FleetResponse => ({
  fleet: entries,
  generated_at: new Date().toISOString(),
});

export const providerConfigs = () => ({
  providers: [
    {
      provider_id: 'anthropic',
      name: 'Anthropic',
      account_id: 'me@example.com',
    },
  ],
});

export const forecastEntry = (o: Partial<ForecastEntry> = {}): ForecastEntry => ({
  provider_id: 'anthropic',
  account_id: 'me@example.com',
  service_name: 'Claude',
  window_type: 'weekly',
  status: 'ok',
  now_pct: 42,
  projected_pct: 70,
  confidence: 0.8,
  samples_used: 24,
  glide_pct: 50,
  ...o,
});

export const forecastResponse = (forecasts: ForecastEntry[]): ForecastResponse => ({
  forecasts,
});

export const costForecast = (o: Partial<CostForecastResponse> = {}): CostForecastResponse => ({
  as_of: new Date().toISOString(),
  current_month_to_date: 12.5,
  daily_burn_avg_7d: 1.25,
  projected_eom: 37.5,
  days_in_month: 30,
  day_of_month: 10,
  days_remaining: 20,
  by_provider: [],
  ...o,
});

export const cumulativeResponse = (o: Partial<CumulativeResponse> = {}): CumulativeResponse => ({
  cumulative: [
    {
      provider_id: 'anthropic',
      account_id: 'me@example.com',
      '2026-06': {
        tokens_input: 1000,
        tokens_output: 500,
        tokens_cache_read: 300,
        tokens_cache_create: 100,
        tokens_reasoning: 50,
        msgs: 12,
        cost_usd: 12.5,
        by_model: {
          'claude-opus': {
            tokens_input: 800,
            tokens_output: 400,
            msgs: 8,
            cost_usd: 10,
            cost_input: 4,
            cost_output: 2,
            cost_cache_read: 3,
            cost_cache_create: 1,
            cost_cache: 4,
          },
        },
        by_sidecar: {
          laptop: {
            tokens_input: 1000,
            tokens_output: 500,
            msgs: 12,
            cost_usd: 12.5,
            cost_cache: 5,
          },
        },
      },
      lifetime: {
        tokens_input: 5000,
        tokens_output: 2500,
        msgs: 100,
        cost_usd: 99.99,
      },
    },
  ],
  current_month_key: '2026-06',
  current_year_key: '2026',
  generated_at: new Date().toISOString(),
  ...o,
});

export const emptyCumulative = (): CumulativeResponse => ({
  cumulative: [],
  current_month_key: '2026-06',
  current_year_key: '2026',
  generated_at: new Date().toISOString(),
});

export const session = (o: Partial<SessionEntry> = {}): SessionEntry => ({
  session_id: 'abcdef1234567890',
  ts_start: new Date(Date.now() - 3_600_000).toISOString(),
  ts_end: new Date(Date.now() - 1_800_000).toISOString(),
  duration_seconds: 1800,
  msgs: 42,
  models: ['claude-opus'],
  tokens_total: 12345,
  tokens_input: 8000,
  tokens_output: 3000,
  cost_usd: 1.23,
  ...o,
});

export const anomaliesResponse = (
  o: Partial<AnomaliesResponse> = {},
): AnomaliesResponse => ({
  as_of: new Date().toISOString(),
  lookback_days: 30,
  z_threshold: 2,
  anomalies: [],
  ...o,
});

export const errorEvents = (): EventsResponse => ({
  events: [{ event_id: 'e1', kind: 'error', error_reason: 'overloaded' }],
  total: 1,
  limit: 100,
  offset: 0,
});

export const eventsResponse = (count: number, total: number, offset = 0): EventsResponse => ({
  events: Array.from({ length: count }, (_, i) => ({
    event_id: `e${offset + i}`,
    id: offset + i,
    ts: new Date(Date.now() - i * 60_000).toISOString(),
    model_id: 'claude-opus',
    sidecar_id: 'laptop',
    kind: 'message',
    tokens_input: 100,
    tokens_output: 50,
    cost_usd: 0.01,
  })),
  total,
  limit: 25,
  offset,
});

export const emptyEvents = (): EventsResponse => ({
  events: [],
  total: 0,
  limit: 25,
  offset: 0,
});

export const heatmapResponse = (hasActivity: boolean): HeatmapResponse => ({
  tz: 'America/New_York',
  cells: hasActivity ? [{ dow: 1, hour: 9, tokens: 1000 }] : [{ dow: 1, hour: 9, tokens: 0 }],
});

export const historyChart = (hasBars: boolean): HistoryChartResponse => ({
  bars: hasBars
    ? [
        {
          date: '2026-06-10',
          ts: '2026-06-10T00:00:00Z',
          segments: [
            { provider_id: 'anthropic', model_id: 'claude-opus', label: 'Opus', value: 100 },
          ],
        },
      ]
    : [],
});
