import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type {
  DataHealthCheckReport,
  DataHealthReport,
  FleetEntry,
  LimitCard,
  TokenHealthEntry,
} from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { Banners } from './Banners';

const card = (o: Partial<LimitCard> = {}): LimitCard => ({
  service_name: 'Ollama',
  pct_used: 0,
  window_type: 'weekly',
  reset_at: new Date(Date.now() + 3_600_000).toISOString(),
  updated_at: new Date(Date.now() - 7 * 86_400_000).toISOString(),
  ...o,
});

const entry = (o: Partial<FleetEntry> = {}): FleetEntry => ({
  provider_id: 'ollama',
  account_id: 'default',
  critical_gauge: card(),
  secondary_limits: [],
  ...o,
});

describe('Banners collection failure', () => {
  it('renders a critical banner when a fleet card is collection-failing', () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              stale: true,
              collection_failing: true,
              detail: '⚠ Collection failing — timeout [Cached 346.1m ago]',
              fetched_at: new Date(Date.now() - 7 * 86_400_000).toISOString(),
            }),
          }),
        ]}
      />,
    );
    expect(screen.getByText(/collection failing for ollama/i)).toBeInTheDocument();
  });

  it('renders a multi-provider summary when several entries fail', () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              service_name: 'Ollama',
              stale: true,
              collection_failing: true,
              detail: '⚠ Collection failing — a [Cached 1h ago]',
            }),
          }),
          entry({
            provider_id: 'gemini',
            critical_gauge: card({
              service_name: 'Gemini',
              stale: true,
              collection_failing: true,
              detail: '⚠ Collection failing — b [Cached 2h ago]',
            }),
          }),
        ]}
      />,
    );
    expect(screen.getByText(/collection failing for 2 providers/i)).toBeInTheDocument();
  });

  it('does not raise a banner when cards are healthy', () => {
    renderWithProviders(<Banners tokens={[]} anomalies={[]} fleet={[entry()]} />);
    expect(screen.queryByText(/collection failing/i)).not.toBeInTheDocument();
  });

  it('treats stale=true as collection failing even without the detail prefix', () => {
    renderWithProviders(
      <Banners tokens={[]} anomalies={[]} fleet={[entry({ critical_gauge: card({ stale: true }) })]} />,
    );
    expect(screen.getByText(/collection failing/i)).toBeInTheDocument();
  });

  it('treats collection_failing=true as collection failing without stale or prefix', () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        fleet={[entry({ critical_gauge: card({ collection_failing: true }) })]}
      />,
    );
    expect(screen.getByText(/collection failing/i)).toBeInTheDocument();
  });

  it('uses the stale secondary card timestamp when the critical gauge is fresh', () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              service_name: 'Ollama',
              updated_at: new Date(Date.now() - 2 * 60_000).toISOString(),
            }),
            secondary_limits: [
              card({
                service_name: 'Ollama Weekly',
                stale: true,
                updated_at: new Date(Date.now() - 90 * 60_000).toISOString(),
              }),
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByText(/ollama weekly \(last ok 1h 30m ago\)/i)).toBeInTheDocument();
  });

  it('falls back to the provider name and prefers fetched_at for stale cards', () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              service_name: '',
              stale: true,
              updated_at: new Date(Date.now() - 2 * 60 * 60_000).toISOString(),
              fetched_at: new Date(Date.now() - 30 * 60_000).toISOString(),
            }),
          }),
        ]}
      />,
    );
    expect(screen.getByText(/ollama \(last ok 30m ago\)/i)).toBeInTheDocument();
  });

  it('dismisses the banner', async () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              stale: true,
              collection_failing: true,
              detail: '⚠ Collection failing — x [Cached 1h ago]',
            }),
          }),
        ]}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/collection failing/i)).not.toBeInTheDocument();
  });
});

const tokenEntry = (o: Partial<TokenHealthEntry> = {}): TokenHealthEntry => ({
  provider: 'zai',
  account_id: 'server',
  account_label: null,
  status: 'invalid',
  token_types: ['api_key'],
  ...o,
});

describe('Banners credential health', () => {
  it('raises a critical banner for a provider-rejected (invalid) credential', () => {
    renderWithProviders(<Banners tokens={[tokenEntry()]} anomalies={[]} />);
    expect(
      screen.getByText(/credential for zai \(server environment\) was rejected by the provider/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /review tokens/i })).toBeInTheDocument();
  });

  it('keeps the expired copy for a timed-out token', () => {
    renderWithProviders(
      <Banners
        tokens={[tokenEntry({ status: 'expired', account_id: 'a@x.com' })]}
        anomalies={[]}
      />,
    );
    expect(screen.getByText(/credential for zai \(a@x\.com\) is expired/i)).toBeInTheDocument();
  });

  it('does not raise a banner for a redundant credential', () => {
    renderWithProviders(
      <Banners tokens={[tokenEntry({ status: 'expired', redundant: true })]} anomalies={[]} />,
    );
    expect(screen.queryByText(/credential/i)).not.toBeInTheDocument();
  });

  it('summarises several unhealthy credentials', () => {
    renderWithProviders(
      <Banners
        tokens={[tokenEntry(), tokenEntry({ provider: 'openrouter', status: 'expiring' })]}
        anomalies={[]}
      />,
    );
    expect(screen.getByText(/2 credentials need attention/i)).toBeInTheDocument();
  });
});

const dataHealthCheck = (o: Partial<DataHealthCheckReport> = {}): DataHealthCheckReport => ({
  check_id: 'config_default_keyed',
  title: 'Provider account uses a generic ID',
  description: 'A saved provider configuration uses a generic account ID.',
  impact: 'Usage can be split across identities.',
  recommended_action: 'Re-key the configuration.',
  severity: 'error',
  total_count: 0,
  fixable_count: 0,
  groups: [],
  blocked_by: [],
  blocked_by_titles: [],
  blocked: false,
  ...o,
});

describe('Banners data health', () => {
  it('raises a banner for a single error-severity check with findings', () => {
    renderWithProviders(
      <Banners
        tokens={[]}
        anomalies={[]}
        dataHealth={{ scanning: false, checks: [dataHealthCheck({ total_count: 1 })] }}
      />,
    );
    expect(screen.getByText(/data health found an issue: provider account uses a generic id/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /review and fix/i })).toHaveAttribute(
      'href',
      '/settings/data-health',
    );
  });

  it('summarises several checks with findings', () => {
    const report: DataHealthReport = {
      scanning: false,
      checks: [
        dataHealthCheck({ check_id: 'a', total_count: 1 }),
        dataHealthCheck({ check_id: 'b', total_count: 2 }),
      ],
    };
    renderWithProviders(<Banners tokens={[]} anomalies={[]} dataHealth={report} />);
    expect(screen.getByText(/data health found issues in 2 checks/i)).toBeInTheDocument();
  });

  it('ignores warn/info severity and clean checks', () => {
    const report: DataHealthReport = {
      scanning: false,
      checks: [
        dataHealthCheck({ severity: 'warn', total_count: 5 }),
        dataHealthCheck({ severity: 'error', total_count: 0 }),
      ],
    };
    renderWithProviders(<Banners tokens={[]} anomalies={[]} dataHealth={report} />);
    expect(screen.queryByText(/data health found/i)).not.toBeInTheDocument();
  });

  it('does not render when dataHealth is undefined', () => {
    renderWithProviders(<Banners tokens={[]} anomalies={[]} />);
    expect(screen.queryByText(/data health found/i)).not.toBeInTheDocument();
  });
});
