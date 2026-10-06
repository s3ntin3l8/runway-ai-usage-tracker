import { cleanup, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type {
  CredentialInventory,
  CredentialSourceView,
  DataHealthCheckReport,
  DataHealthReport,
  FleetEntry,
  LimitCard,
} from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { Banners } from './Banners';
import { inventoryWith, source } from '@/features/settings/sections/credentials/testData';

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
        credentials={undefined}
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

  it('names the reason in the banner when the backend gave one', () => {
    renderWithProviders(
      <Banners
        credentials={undefined}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              stale: true,
              collection_failing: true,
              detail: '⚠ Collection failing (login expired — waiting for its machine) — 12%',
            }),
          }),
        ]}
      />,
    );
    expect(
      screen.getByText(/login expired — waiting for its machine/i),
    ).toBeInTheDocument();
  });

  it('links to Fleet instead of generic settings when the reason is about keep-alive', () => {
    renderWithProviders(
      <Banners
        credentials={undefined}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              stale: true,
              collection_failing: true,
              detail:
                '⚠ Collection failing (login expired — waiting for its machine to renew it; run the sidecar with --keep-alive (or turn it on in Fleet) to automate) — 12% used',
            }),
          }),
        ]}
      />,
    );
    const link = screen.getByRole('link', { name: /open fleet/i });
    expect(link).toHaveAttribute('href', '/fleet');
    expect(screen.queryByRole('link', { name: /check settings/i })).not.toBeInTheDocument();
  });

  it('keeps the generic settings link for other collection failures', () => {
    renderWithProviders(
      <Banners
        credentials={undefined}
        anomalies={[]}
        fleet={[
          entry({
            critical_gauge: card({
              stale: true,
              collection_failing: true,
              detail: '⚠ Collection failing (provider rate limited) — 12% used',
            }),
          }),
        ]}
      />,
    );
    expect(screen.getByRole('link', { name: /check settings/i })).toHaveAttribute(
      'href',
      '/settings',
    );
    expect(screen.queryByRole('link', { name: /open fleet/i })).not.toBeInTheDocument();
  });

  it('renders a multi-provider summary when several entries fail', () => {
    renderWithProviders(
      <Banners
        credentials={undefined}
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
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} fleet={[entry()]} />);
    expect(screen.queryByText(/collection failing/i)).not.toBeInTheDocument();
  });

  it('treats stale=true as collection failing even without the detail prefix', () => {
    renderWithProviders(
      <Banners credentials={undefined} anomalies={[]} fleet={[entry({ critical_gauge: card({ stale: true }) })]} />,
    );
    expect(screen.getByText(/collection failing/i)).toBeInTheDocument();
  });

  it('treats collection_failing=true as collection failing without stale or prefix', () => {
    renderWithProviders(
      <Banners
        credentials={undefined}
        anomalies={[]}
        fleet={[entry({ critical_gauge: card({ collection_failing: true }) })]}
      />,
    );
    expect(screen.getByText(/collection failing/i)).toBeInTheDocument();
  });

  it('uses the stale secondary card timestamp when the critical gauge is fresh', () => {
    renderWithProviders(
      <Banners
        credentials={undefined}
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
        credentials={undefined}
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
        credentials={undefined}
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

const attention = (o: Partial<CredentialSourceView> = {}): CredentialSourceView =>
  source({
    provider_id: 'zai',
    account_id: 'default',
    origin_kind: 'server',
    origin_type: 'env',
    label: 'ZAI_API_KEY',
    status: 'invalid',
    token_types: ['api_key'],
    ...o,
  });

describe('Banners credential health', () => {
  it('raises a critical banner for a provider-rejected (invalid) credential', () => {
    renderWithProviders(<Banners credentials={inventoryWith([attention()])} anomalies={[]} />);
    expect(
      screen.getByText(/credential for zai \(server environment\) was rejected by the provider/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /review credentials/i })).toBeInTheDocument();
  });

  it('says a credential that keeps failing to collect is failing, not rejected', () => {
    renderWithProviders(
      <Banners credentials={inventoryWith([attention({ status: 'failing' })])} anomalies={[]} />,
    );
    expect(
      screen.getByText(/credential for zai \(server environment\) keeps failing to collect/i),
    ).toBeInTheDocument();
  });

  it('keeps the expired copy for a timed-out token', () => {
    renderWithProviders(
      <Banners
        credentials={inventoryWith([attention({ status: 'expired', origin_kind: 'machine' })], {
          account_id: 'a@x.com',
        })}
        anomalies={[]}
      />,
    );
    expect(screen.getByText(/credential for zai \(a@x\.com\) is expired/i)).toBeInTheDocument();
  });

  it('does not raise a banner for a redundant credential', () => {
    renderWithProviders(
      <Banners
        credentials={inventoryWith([attention({ status: 'expired', redundant: true })])}
        anomalies={[]}
      />,
    );
    expect(screen.queryByText(/credential/i)).not.toBeInTheDocument();
  });

  it('summarises several unhealthy credentials', () => {
    renderWithProviders(
      <Banners
        credentials={inventoryWith([
          attention(),
          attention({ source_id: 'sidecar:b', status: 'expiring' }),
        ])}
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
        credentials={undefined}
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
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} dataHealth={report} />);
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
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} dataHealth={report} />);
    expect(screen.queryByText(/data health found/i)).not.toBeInTheDocument();
  });

  it('warns when credential alerts have nowhere to go, with a link to add a webhook', () => {
    const report: DataHealthReport = {
      scanning: false,
      checks: [dataHealthCheck({ check_id: 'alert_channels', severity: 'warn', total_count: 1 })],
    };
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} dataHealth={report} />);
    expect(screen.getByText(/credential alerts have no delivery channel/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /add a webhook/i })).toHaveAttribute(
      'href',
      '/settings/webhooks',
    );
    // It is not a repair, so the generic data-health banner stays out of it.
    expect(screen.queryByText(/data health found/i)).not.toBeInTheDocument();
  });

  it('stays quiet once a channel exists', () => {
    const report: DataHealthReport = {
      scanning: false,
      checks: [dataHealthCheck({ check_id: 'alert_channels', severity: 'warn', total_count: 0 })],
    };
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} dataHealth={report} />);
    expect(screen.queryByText(/delivery channel/i)).not.toBeInTheDocument();
  });

  it('does not render when dataHealth is undefined', () => {
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} />);
    expect(screen.queryByText(/data health found/i)).not.toBeInTheDocument();
  });
});


describe('Banners unmapped credentials', () => {
  const withBlocked = (blocked: NonNullable<CredentialInventory['blocked_collection']>) => ({
    ...inventoryWith([]),
    machines: [
      { machine_id: 'host-a', name: 'Workstation', last_seen: null, credential_count: 1, unmapped_count: 1 },
    ],
    blocked_collection: blocked,
  });

  it('names the machine and provider and deep-links to the exact origin', () => {
    renderWithProviders(
      <Banners
        credentials={withBlocked([
          { sidecar_id: 'host-a', provider_id: 'deepseek', credential_origin: 'env:DEEPSEEK_API_KEY' },
        ])}
        anomalies={[]}
      />,
    );
    expect(
      screen.getByText(
        /credential unmapped on workstation — quota for deepseek won't collect until it is assigned an account/i,
      ),
    ).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /assign account/i });
    const href = link.getAttribute('href') ?? '';
    expect(href).toContain('/settings/credentials?');
    const query = new URLSearchParams(href.split('?')[1]);
    expect(Object.fromEntries(query)).toEqual({
      view: 'mapping',
      sidecar: 'host-a',
      provider: 'deepseek',
      origin: 'env:DEEPSEEK_API_KEY',
    });
  });

  it('keeps the credential fingerprint out of the link', () => {
    renderWithProviders(
      <Banners
        credentials={withBlocked([
          {
            sidecar_id: 'host-a',
            provider_id: 'deepseek',
            credential_origin: 'env:DEEPSEEK_TOKEN#86e40eb64385',
          },
        ])}
        anomalies={[]}
      />,
    );
    const href = screen.getByRole('link', { name: /assign account/i }).getAttribute('href') ?? '';
    expect(new URLSearchParams(href.split('?')[1]).get('origin')).toBe('env:DEEPSEEK_TOKEN');
  });

  it('summarises several unmapped credentials and links to the list', () => {
    renderWithProviders(
      <Banners
        credentials={withBlocked([
          { sidecar_id: 'host-a', provider_id: 'deepseek', credential_origin: 'env:A' },
          { sidecar_id: 'host-a', provider_id: 'kimi_coding', credential_origin: 'cookie:b' },
        ])}
        anomalies={[]}
      />,
    );
    expect(
      screen.getByText(/2 credentials are unmapped — quota for deepseek, kimi_coding/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /assign account/i })).toHaveAttribute(
      'href',
      '/settings/credentials?view=mapping',
    );
  });

  it('shows nothing when no credential is blocking collection (or the server is older)', () => {
    renderWithProviders(<Banners credentials={inventoryWith([])} anomalies={[]} />);
    expect(screen.queryByText(/credential unmapped/i)).not.toBeInTheDocument();
  });
});

describe('Banners remembered dismissals', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.useRealTimers());

  const noChannel: DataHealthReport = {
    scanning: false,
    checks: [dataHealthCheck({ check_id: 'alert_channels', severity: 'warn', total_count: 1 })],
  };
  const failingEntry = (providerId: string) =>
    entry({
      provider_id: providerId,
      critical_gauge: card({ stale: true, collection_failing: true }),
    });

  it('keeps a dismissed banner hidden after a remount', async () => {
    const user = userEvent.setup();
    const first = renderWithProviders(
      <Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />,
    );
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/delivery channel/i)).not.toBeInTheDocument();
    first.unmount();

    renderWithProviders(<Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />);
    expect(screen.queryByText(/delivery channel/i)).not.toBeInTheDocument();
  });

  it('shows the banner again when its condition changes', async () => {
    const user = userEvent.setup();
    const first = renderWithProviders(
      <Banners credentials={undefined} anomalies={[]} fleet={[failingEntry('ollama')]} />,
    );
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    first.unmount();

    renderWithProviders(
      <Banners
        credentials={undefined}
        anomalies={[]}
        fleet={[failingEntry('ollama'), failingEntry('zai')]}
      />,
    );
    expect(screen.getByText(/collection failing for 2 providers/i)).toBeInTheDocument();
  });

  it('shows the anomaly banner again on a later day', async () => {
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-10-03T12:00:00') });
    const spike = {
      provider_id: 'anthropic',
      account_id: 'default',
      model_id: 'opus',
      today_tokens: 1,
      today_cost_usd: 1,
      historical_mean_tokens: 1,
      historical_stddev_tokens: 1,
      z_score_tokens: 4,
      verdict: 'spike',
    };
    const user = userEvent.setup();
    const first = renderWithProviders(<Banners credentials={undefined} anomalies={[spike]} />);
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    first.unmount();

    renderWithProviders(<Banners credentials={undefined} anomalies={[spike]} />);
    expect(screen.queryByText(/unusual usage today/i)).not.toBeInTheDocument();
    cleanup();

    vi.setSystemTime(new Date('2026-10-04T12:00:00'));
    renderWithProviders(<Banners credentials={undefined} anomalies={[spike]} />);
    expect(screen.getByText(/unusual usage today/i)).toBeInTheDocument();
  });

  it('re-shows a dismissed banner when the problem recovers and then returns', async () => {
    const user = userEvent.setup();
    const view = renderWithProviders(
      <Banners credentials={undefined} anomalies={[]} fleet={[failingEntry('ollama')]} />,
    );
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/collection failing/i)).not.toBeInTheDocument();

    // Recovery: fleet loaded, nothing failing -> the stored dismissal is forgotten.
    view.rerender(<Banners credentials={undefined} anomalies={[]} fleet={[entry()]} />);
    expect(localStorage.getItem('runway:banner-dismissed:collection')).toBeNull();

    // Relapse with the identical fingerprint.
    view.rerender(
      <Banners credentials={undefined} anomalies={[]} fleet={[failingEntry('ollama')]} />,
    );
    expect(screen.getByText(/collection failing for ollama/i)).toBeInTheDocument();
  });

  it('re-shows the no-channel banner after a webhook is added and later removed', async () => {
    const user = userEvent.setup();
    const resolved: DataHealthReport = {
      scanning: false,
      checks: [dataHealthCheck({ check_id: 'alert_channels', severity: 'warn', total_count: 0 })],
    };
    const view = renderWithProviders(
      <Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />,
    );
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    view.rerender(<Banners credentials={undefined} anomalies={[]} dataHealth={resolved} />);
    expect(localStorage.getItem('runway:banner-dismissed:alert-channel')).toBeNull();
    view.rerender(<Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />);
    expect(screen.getByText(/delivery channel/i)).toBeInTheDocument();
  });

  it('does not clear a dismissal while the data is still loading', async () => {
    const user = userEvent.setup();
    const view = renderWithProviders(
      <Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />,
    );
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    view.rerender(<Banners credentials={undefined} anomalies={undefined} dataHealth={undefined} />);
    expect(localStorage.getItem('runway:banner-dismissed:alert-channel')).toBe('no-channel');
    view.rerender(<Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />);
    expect(screen.queryByText(/delivery channel/i)).not.toBeInTheDocument();
  });

  it('still dismisses for the session when localStorage throws', async () => {
    const user = userEvent.setup();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    renderWithProviders(<Banners credentials={undefined} anomalies={[]} dataHealth={noChannel} />);
    await user.click(screen.getByRole('button', { name: /dismiss/i }));
    expect(screen.queryByText(/delivery channel/i)).not.toBeInTheDocument();
    vi.restoreAllMocks();
  });
});
