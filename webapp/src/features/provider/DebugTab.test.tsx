import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { DebugRawResponse, SourceProbeResponse } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { DebugTab } from './DebugTab';
import { fleetEntry, limitCard } from './test-fixtures';
import * as api from '@/api/endpoints';
import { account, inventory, source } from '@/features/settings/sections/credentials/testData';

vi.mock('@/api/endpoints');

const entry = fleetEntry({
  critical_gauge: limitCard({
    tier: 'Max',
    data_source: 'api',
    input_source: 'config',
    cache_ttl_seconds: 3600,
    fetched_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    next_poll_at: new Date(Date.now() + 55 * 60_000).toISOString(),
  }),
});

const renderTab = () =>
  renderWithProviders(
    <DebugTab providerId="anthropic" accountId="me@example.com" entry={entry} active />,
  );

describe('DebugTab', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory({
      providers: [{
        provider_id: 'anthropic', name: 'Claude', accounts: [account([
          source({
            provider_id: 'anthropic', account_id: 'me@example.com',
            source_id: 'config:me@example.com', origin_kind: 'config',
            origin_type: 'api_key', label: 'Settings credential', is_active: true,
            last_success_at: new Date(Date.now() - 60_000).toISOString(),
          }),
        ], { provider_id: 'anthropic', account_id: 'me@example.com', active_source_id: 'config:me@example.com' })],
      }],
    }));
  });

  it('separates the successful credential, quota card, and collector schedule', async () => {
    renderTab();
    expect(await screen.findByText('Collection context')).toBeInTheDocument();
    expect((await screen.findAllByText('Settings credential')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('Valid').length).toBeGreaterThan(0);
    expect(screen.getByText('Most restrictive quota card')).toBeInTheDocument();
    expect(screen.getByText('Max')).toBeInTheDocument();
    expect(screen.getByText('weekly')).toBeInTheDocument();
    expect(screen.getByText('api · config')).toBeInTheDocument();
    expect(screen.getByText('Collector schedule · account')).toBeInTheDocument();
    expect(screen.getByText('3600s')).toBeInTheDocument();
    expect(screen.getAllByText(/ago$/).length).toBeGreaterThan(0);
    expect(screen.getByText(/^in /)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /full credential inventory/i })).toHaveAttribute('href', '/settings/credentials');
  });

  describe('credential sources', () => {
    const probeResponse = (sources: SourceProbeResponse['sources']): SourceProbeResponse => ({
      provider_id: 'anthropic',
      account_id: 'me@example.com',
      probed_at: new Date().toISOString(),
      sources,
    });

    it('lists the account\'s sources and never probes on its own', async () => {
      renderTab();
      const list = await screen.findByRole('list', { name: 'Credential sources' });
      expect(within(list).getByText('Settings credential')).toBeInTheDocument();
      expect(within(list).getByText('Feeding data')).toBeInTheDocument();
      expect(within(list).getByText(/priority 0/)).toBeInTheDocument();
      expect(api.probeCredentialSources).not.toHaveBeenCalled();
    });

    it('probes on demand and shows each source\'s result without secrets', async () => {
      vi.mocked(api.probeCredentialSources).mockResolvedValue(
        probeResponse([
          {
            source_id: 'config:me@example.com',
            outcome: 'auth_failed',
            probed: true,
            http_status: 401,
            error_type: 'auth_failed',
            duration_ms: 123,
          },
        ]),
      );
      renderTab();
      await userEvent.click(await screen.findByRole('button', { name: 'Probe sources' }));

      expect(api.probeCredentialSources).toHaveBeenCalledWith('anthropic', 'me@example.com');
      expect(await screen.findByText('Rejected by the provider')).toBeInTheDocument();
      expect(screen.getByText('HTTP 401')).toBeInTheDocument();
      expect(screen.getByText('123 ms')).toBeInTheDocument();
      expect(screen.getByText(/changes nothing/i)).toBeInTheDocument();
    });

    it('explains a source that was not called instead of implying it works', async () => {
      vi.mocked(api.probeCredentialSources).mockResolvedValue(
        probeResponse([
          { source_id: 'config:me@example.com', outcome: 'waiting_on_machine', probed: false },
        ]),
      );
      renderTab();
      await userEvent.click(await screen.findByRole('button', { name: 'Probe sources' }));
      expect(await screen.findByText(/waiting for its machine/i)).toBeInTheDocument();
      expect(screen.queryByText(/ms$/)).not.toBeInTheDocument();
    });

    it('shows a failed probe (rate limit, not admin) as an alert', async () => {
      vi.mocked(api.probeCredentialSources).mockRejectedValue(new Error('429 Too Many Requests'));
      renderTab();
      await userEvent.click(await screen.findByRole('button', { name: 'Probe sources' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('Probe failed: 429 Too Many Requests');
    });

    it('has nothing to probe when no source is reported', async () => {
      vi.mocked(api.fetchCredentialInventory).mockResolvedValue(inventory({ providers: [] }));
      renderTab();
      expect(await screen.findByText(/no credential source reported/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Probe sources' })).toBeDisabled();
    });
  });

  it('shows the capture prompt and does not auto-fetch', () => {
    renderTab();
    expect(screen.getByText(/capture raw collector output/i)).toBeInTheDocument();
    expect(api.fetchDebugRaw).not.toHaveBeenCalled();
  });

  it('hides capture for a sidecar-only (local) provider', () => {
    const localEntry = fleetEntry({
      critical_gauge: limitCard({ data_source: 'local', input_source: 'sidecar' }),
    });
    renderWithProviders(
      <DebugTab providerId="antigravity" accountId="me@example.com" entry={localEntry} active />,
    );
    expect(screen.getByText(/raw capture unavailable/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /run capture/i })).not.toBeInTheDocument();
    expect(api.fetchDebugRaw).not.toHaveBeenCalled();
  });

  it('honors an explicit no-server-collector capability', () => {
    const entry = fleetEntry({
      server_collector_available: false,
      critical_gauge: limitCard({ data_source: 'api' }),
    });
    renderWithProviders(
      <DebugTab providerId="anthropic" accountId="me@example.com" entry={entry} active />,
    );
    expect(screen.getByText(/raw capture unavailable/i)).toBeInTheDocument();
    expect(api.fetchDebugRaw).not.toHaveBeenCalled();
  });

  it('allows capture for a local usage card when the provider has a server collector', () => {
    const localKimiEntry = fleetEntry({
      provider_id: 'kimi_coding',
      server_collector_available: true,
      critical_gauge: limitCard({
        provider_id: 'kimi_coding',
        data_source: 'local',
        input_source: 'sidecar',
        is_unlimited: true,
        window_type: 'lifetime',
      }),
    });
    renderWithProviders(
      <DebugTab
        providerId="kimi_coding"
        accountId="me@example.com"
        entry={localKimiEntry}
        active
      />,
    );
    expect(screen.getByText('Collection context')).toBeInTheDocument();
    expect(screen.getByText(/capture raw collector output/i)).toBeInTheDocument();
  });

  it.each([
    {
      label: 'unlimited',
      gauge: limitCard({ data_source: 'api', is_unlimited: true }),
      expected: 'unlimited',
    },
    {
      label: 'error',
      gauge: limitCard({ data_source: 'api', error_type: 'api_error' }),
      expected: 'error',
    },
    {
      label: 'quota',
      gauge: limitCard({ data_source: 'api', is_unlimited: false }),
      expected: 'quota',
    },
  ])('labels a registered API card as $label', ({ gauge, expected }) => {
    const entry = fleetEntry({
      server_collector_available: true,
      critical_gauge: gauge,
    });
    renderWithProviders(
      <DebugTab providerId="anthropic" accountId="me@example.com" entry={entry} active />,
    );
    expect(screen.getByText(expected, { selector: 'dd' })).toBeInTheDocument();
  });

  it('runs the capture and renders the strategy accordion', async () => {
    const mockData: DebugRawResponse = {
      provider_id: 'anthropic',
      is_configured: true,
      credentials: { token_found: true, token_source: 'config' },
      active_strategy: 'web',
      active_strategy_card_count: 3,
      strategies: {
        web: {
          label: 'Web API (web)',
          kind: 'primary',
          status: 'success',
          cards_returned: 3,
          cards_summary: [
            { service_name: 'Claude', remaining: '45%' },
            { service_name: 'Kimi', detail: 'Credential rejected', error_type: 'auth_failed' },
            {},
          ],
          requests: [
            { method: 'GET', url: 'https://claude.ai/api/usage', timestamp: 1000 },
          ],
          responses: [
            {
              url: 'https://claude.ai/api/usage',
              method: 'GET',
              status: 200,
              headers: { 'content-type': 'application/json' },
              body: { ok: true },
              timestamp: 1001,
            },
          ],
          errors: [],
        },
        oauth: {
          label: 'OAuth API (api)',
          kind: 'primary',
          status: 'error',
          cards_returned: 0,
          cards_summary: [],
          requests: [{ method: 'POST', url: 'https://api.anthropic.com/v1/limits', timestamp: 1002 }],
          responses: [],
          errors: [{ type: 'HTTPStatusError', message: '401 Unauthorized' }],
        },
      },
      timestamp: 1003,
    };
    vi.mocked(api.fetchDebugRaw).mockResolvedValue(mockData as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText('Raw collector exchange')).toBeInTheDocument();
    await waitFor(() =>
      expect(api.fetchDebugRaw).toHaveBeenCalledWith('anthropic', 'me@example.com'),
    );

    // Strategy sections rendered
    expect(screen.getByText('Web API (web)')).toBeInTheDocument();
    expect(screen.getByText('OAuth API (api)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Web API \(web\)/i }));

    // Kind badges
    expect(screen.getAllByText('primary')).toHaveLength(2);

    // Active badge only on the winning strategy
    expect(screen.getByText('Active')).toBeInTheDocument();

    // Status badges
    expect(screen.getByText('success')).toBeInTheDocument();
    expect(screen.getByText('HTTPStatusError')).toBeInTheDocument();
    expect(screen.getByText('Credential rejected')).toBeInTheDocument();
    expect(screen.getByText('(auth_failed)')).toBeInTheDocument();
    expect(screen.getByText('Card:')).toBeInTheDocument();
    expect(screen.getByText('returned')).toBeInTheDocument();
  });

  it('shows a failure state with retry on error', async () => {
    vi.mocked(api.fetchDebugRaw).mockRejectedValue(new Error('rate limited'));
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText(/capture failed/i)).toBeInTheDocument();
    expect(screen.getByText(/rate limited/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('expands a strategy section to show requests and errors', async () => {
    const mockData: DebugRawResponse = {
      provider_id: 'anthropic',
      is_configured: true,
      credentials: { token_found: true, token_source: 'config' },
      active_strategy: 'web',
      active_strategy_card_count: 2,
      strategies: {
        web: {
          label: 'Web API (web)',
          kind: 'primary',
          status: 'success',
          cards_returned: 2,
          cards_summary: [{ service_name: 'Claude', remaining: '45%' }],
          requests: [{ method: 'GET', url: 'https://claude.ai/api/usage', timestamp: 1000 }],
          responses: [{ url: 'https://claude.ai/api/usage', method: 'GET', status: 200, headers: { 'content-type': 'application/json' }, body: { ok: true }, timestamp: 1001 }],
          errors: [],
        },
      },
      timestamp: 1003,
    };
    vi.mocked(api.fetchDebugRaw).mockResolvedValue(mockData as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText('Raw collector exchange')).toBeInTheDocument();

    // Strategy section starts collapsed
    expect(screen.queryByText(/Requests/)).not.toBeInTheDocument();

    // Click to expand strategy
    await userEvent.click(screen.getByText('Web API (web)'));

    // Sub-section headers visible
    expect(screen.getByText(/Requests \(1\)/)).toBeInTheDocument();
    expect(screen.getByText(/Responses \(1\)/)).toBeInTheDocument();

    // Request sub-section starts collapsed — expand it
    await userEvent.click(screen.getByText(/Requests \(1\)/));
    expect(screen.getByText(/claude\.ai/)).toBeInTheDocument();

    // Collapse strategy
    await userEvent.click(screen.getByText('Web API (web)'));
    expect(screen.queryByText(/Requests \(1\)/)).not.toBeInTheDocument();
  });

  it('expands a ResponseBlock to show response body', async () => {
    const mockData: DebugRawResponse = {
      provider_id: 'anthropic',
      is_configured: true,
      credentials: { token_found: true, token_source: 'config' },
      active_strategy: 'web',
      active_strategy_card_count: 1,
      strategies: {
        web: {
          label: 'Web API (web)',
          kind: 'primary',
          status: 'success',
          cards_returned: 1,
          cards_summary: [],
          requests: [],
          responses: [{ url: 'https://claude.ai/api/usage', method: 'GET', status: 200, headers: {}, body: { data: 'test' }, timestamp: 1000 }],
          errors: [],
        },
      },
      timestamp: 1003,
    };
    vi.mocked(api.fetchDebugRaw).mockResolvedValue(mockData as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText('Raw collector exchange')).toBeInTheDocument();

    // Expand strategy section
    await userEvent.click(screen.getByText('Web API (web)'));

    // Expand response sub-section
    await userEvent.click(screen.getByText(/Responses \(1\)/));

    // Response body not visible yet
    expect(screen.queryByText(/"data"/)).not.toBeInTheDocument();

    // Click status code to expand
    await userEvent.click(screen.getByText('200'));
    expect(screen.getByText(/"data"/)).toBeInTheDocument();
    expect(screen.getByText(/"test"/)).toBeInTheDocument();
  });

  it('shows the legacy collector message when no strategies returned', async () => {
    const mockData: DebugRawResponse = {
      provider_id: 'anthropic',
      is_configured: true,
      credentials: { token_found: true, token_source: 'config' },
      active_strategy: null,
      active_strategy_card_count: 0,
      strategies: {},
      timestamp: 1003,
    };
    vi.mocked(api.fetchDebugRaw).mockResolvedValue(mockData as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText(/no per-strategy breakdown/i)).toBeInTheDocument();
  });

  it('renders errors when a strategy with errors is expanded', async () => {
    const mockData: DebugRawResponse = {
      provider_id: 'anthropic',
      is_configured: true,
      credentials: { token_found: true, token_source: 'config' },
      active_strategy: null,
      active_strategy_card_count: 0,
      strategies: {
        oauth: {
          label: 'OAuth Strategy',
          kind: 'primary',
          status: 'error',
          cards_returned: 0,
          cards_summary: [],
          requests: [],
          responses: [],
          errors: [{ type: 'HTTPStatusError', message: '401 Unauthorized' }],
        },
      },
      timestamp: 1003,
    };
    vi.mocked(api.fetchDebugRaw).mockResolvedValue(mockData as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText('Raw collector exchange')).toBeInTheDocument();

    // Expand strategy section
    await userEvent.click(screen.getByText('OAuth Strategy'));

    // Errors section visible and expanded by default (defaultOpen)
    expect(screen.getByText('Errors')).toBeInTheDocument();
    expect(screen.getByText(/401 Unauthorized/)).toBeInTheDocument();
  });

  it('shows empty traffic message for a strategy with no HTTP data', async () => {
    const mockData: DebugRawResponse = {
      provider_id: 'anthropic',
      is_configured: true,
      credentials: { token_found: true, token_source: 'config' },
      active_strategy: null,
      active_strategy_card_count: 0,
      strategies: {
        api: {
          label: 'API Strategy',
          kind: 'primary',
          status: 'success',
          cards_returned: 0,
          cards_summary: [],
          requests: [],
          responses: [],
          errors: [],
        },
      },
      timestamp: 1003,
    };
    vi.mocked(api.fetchDebugRaw).mockResolvedValue(mockData as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText('Raw collector exchange')).toBeInTheDocument();

    // Expand strategy section
    await userEvent.click(screen.getByText('API Strategy'));
    expect(screen.getByText(/no HTTP traffic captured/i)).toBeInTheDocument();
  });
});
