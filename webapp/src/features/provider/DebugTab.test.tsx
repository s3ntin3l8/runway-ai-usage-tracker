import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { TokenHealthEntry } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { DebugTab } from './DebugTab';
import { fleetEntry, limitCard } from './test-fixtures';
import * as api from '@/api/endpoints';

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
    // Default: token-health query resolves empty so it never returns undefined.
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [] });
  });

  it('renders the authoritative-source pane from the critical gauge', () => {
    renderTab();
    expect(screen.getByText('Authoritative source')).toBeInTheDocument();
    expect(screen.getByText('Max')).toBeInTheDocument();
    expect(screen.getByText('weekly')).toBeInTheDocument();
    expect(screen.getByText('api · config')).toBeInTheDocument();
    expect(screen.getByText('3600s')).toBeInTheDocument();
    // last poll is relative ("5m ago"); next poll is prefixed "in ".
    expect(screen.getByText(/ago$/)).toBeInTheDocument();
    expect(screen.getByText(/^in /)).toBeInTheDocument();
  });

  it('shows token health for the matching provider/account', async () => {
    const token: TokenHealthEntry = {
      provider: 'anthropic',
      account_id: 'me@example.com',
      status: 'valid',
      token_types: ['oauth'],
      source: 'OAuth · /v1/limits',
      can_refresh: true,
      expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(),
    };
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [token] });
    renderTab();

    expect(await screen.findByText('Token health')).toBeInTheDocument();
    expect(screen.getByText('oauth')).toBeInTheDocument();
    expect(screen.getByText('auto-rotate')).toBeInTheDocument();
    expect(screen.getByText(/expires in/i)).toBeInTheDocument();
  });

  it('hides the token-health pane when nothing matches the account', async () => {
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [] });
    renderTab();
    // Source pane is synchronous; give the (empty) token query a tick.
    await screen.findByText('Authoritative source');
    expect(screen.queryByText('Token health')).not.toBeInTheDocument();
  });

  it('shows token health for UI-configured (config account_id) credentials', async () => {
    // Providers configured via the Settings UI use account_id='config', which
    // doesn't match the usage card's real account_id. They should still appear
    // in the Debug tab's Token health pane.
    const configToken: TokenHealthEntry = {
      provider: 'anthropic',
      account_id: 'config',
      status: 'valid',
      token_types: ['api_key'],
      source: 'config',
      source_name: 'config',
      can_refresh: false,
    };
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [configToken] });
    renderTab();
    expect(await screen.findByText('Token health')).toBeInTheDocument();
    expect(screen.getByText('api_key')).toBeInTheDocument();
  });

  it('shows token health for local-file credentials (local-file account_id)', async () => {
    const localToken: TokenHealthEntry = {
      provider: 'anthropic',
      account_id: 'local-file',
      status: 'valid',
      token_types: ['session_key'],
      can_refresh: false,
    };
    vi.mocked(api.fetchTokenHealth).mockResolvedValue({ tokens: [localToken] });
    renderTab();
    expect(await screen.findByText('Token health')).toBeInTheDocument();
    expect(screen.getByText('session_key')).toBeInTheDocument();
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

  it('runs the capture and renders the raw JSON', async () => {
    vi.mocked(api.fetchDebugRaw).mockResolvedValue({ provider: 'anthropic', ok: true } as never);
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText('Raw collector exchange')).toBeInTheDocument();
    await waitFor(() => expect(api.fetchDebugRaw).toHaveBeenCalledWith('anthropic'));
  });

  it('shows a failure state with retry on error', async () => {
    vi.mocked(api.fetchDebugRaw).mockRejectedValue(new Error('rate limited'));
    renderTab();

    await userEvent.click(screen.getByRole('button', { name: /run capture/i }));
    expect(await screen.findByText(/capture failed/i)).toBeInTheDocument();
    expect(screen.getByText(/rate limited/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });
});
