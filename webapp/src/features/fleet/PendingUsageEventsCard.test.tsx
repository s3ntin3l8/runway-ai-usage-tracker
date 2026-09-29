import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { PendingUsageEventsCard } from './PendingUsageEventsCard';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

describe('PendingUsageEventsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'xai',
          name: 'xAI',
          accounts: [
            { account_id: 'alice@example.com', account_label: 'Alice', source: 'config', enabled: true },
            { account_id: 'discovered@example.com', source: 'discovered', enabled: true },
            { account_id: 'disabled@example.com', source: 'config', enabled: false },
            { account_id: 'archived@example.com', source: 'config', enabled: true, archived: true },
          ],
          account_count: 4,
        },
      ],
    });
    vi.mocked(api.assignPendingUsageEvents).mockResolvedValue({ assigned: 2, provider_id: 'xai' });
    vi.mocked(api.assignPendingUsageEventsBatch).mockResolvedValue({
      assigned: 3,
      providers: ['xai'],
      mappings: [{ provider_id: 'xai', sidecar_id: 'laptop', account_id: 'alice@example.com' }],
    });
    vi.mocked(api.fetchPendingUsageSessions).mockImplementation(async (offset = 0) => ({
      items: [
        {
          provider_id: 'xai',
          sidecar_id: 'laptop',
          session_id: offset === 0 ? 'session-12' : 'session-101',
          event_ids: offset === 0 ? [12, 13] : [101],
          event_count: offset === 0 ? 2 : 1,
          first_ts: '2026-09-01T10:00:00Z',
          last_ts: '2026-09-01T10:01:00Z',
          model_ids: ['grok-4'],
        },
      ],
      total_events: offset === 0 ? 158 : 1,
      total_groups: 101,
      sidecars: ['laptop'],
      providers: ['xai'],
      offset,
      limit: 100,
    }));
  });

  it('groups session events and assigns every event in the group to an account', async () => {
    const user = userEvent.setup();
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(
      screen.getByText(/maps future default-identity events for the same provider on the same host/i),
    ).toBeInTheDocument();
    const account = screen.getByRole('combobox', { name: /account for xai session session-12/i });
    expect(screen.getByRole('option', { name: 'Alice' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'discovered@example.com' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'disabled@example.com' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'archived@example.com' })).not.toBeInTheDocument();

    await user.selectOptions(account, 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign' }));
    await waitFor(() =>
      expect(api.assignPendingUsageEvents).toHaveBeenCalledWith([12, 13], 'alice@example.com'),
    );
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('2 usage events assigned'));

    await user.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.fetchPendingUsageSessions).toHaveBeenCalledWith(100, { sidecar_id: undefined, provider_id: undefined, search: undefined }));
    expect(await screen.findByText(/Showing 101–101 of 101 groups/)).toBeInTheDocument();
  });

  it('keeps selections across pages and assigns a selected batch', async () => {
    const user = userEvent.setup();
    renderWithProviders(<PendingUsageEventsCard />);

    await user.click(await screen.findByRole('checkbox', { name: /select xai on laptop session session-12/i }));
    await user.click(screen.getByRole('button', { name: 'Next' }));
    await user.click(await screen.findByRole('checkbox', { name: /select xai on laptop session session-101/i }));
    expect(screen.getByText('2 groups · 3 events selected')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Assign selected' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Batch account for xai' }), 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign 3 events' }));

    await waitFor(() => expect(api.assignPendingUsageEventsBatch).toHaveBeenCalledWith([
      { event_ids: [12, 13, 101], account_id: 'alice@example.com' },
    ]));
  });

  it('offers an account choice per provider in a mixed-provider batch', async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: ['xai', 'anthropic'].map((provider_id) => ({
        provider_id,
        name: provider_id,
        accounts: [{ account_id: `${provider_id}@example.com`, source: 'config', enabled: true }],
        account_count: 1,
      })),
    });
    vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
      items: [
        {
          provider_id: 'xai', sidecar_id: 'laptop', session_id: 'xai-session', event_ids: [12], event_count: 1,
          first_ts: '2026-09-01T10:00:00Z', last_ts: '2026-09-01T10:00:00Z', model_ids: ['grok-4'],
        },
        {
          provider_id: 'anthropic', sidecar_id: 'laptop', session_id: 'anthropic-session', event_ids: [13, 14], event_count: 2,
          first_ts: '2026-09-01T10:00:00Z', last_ts: '2026-09-01T10:01:00Z', model_ids: ['sonnet'],
        },
      ],
      total_events: 3,
      total_groups: 2,
      sidecars: ['laptop'],
      providers: ['anthropic', 'xai'],
      offset: 0,
      limit: 100,
    });
    renderWithProviders(<PendingUsageEventsCard />);

    await user.click(await screen.findByRole('checkbox', { name: /select xai on laptop/i }));
    await user.click(screen.getByRole('checkbox', { name: /select anthropic on laptop/i }));
    await user.click(screen.getByRole('button', { name: 'Assign selected' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Batch account for xai' }), 'xai@example.com');
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Batch account for anthropic' }),
      'anthropic@example.com',
    );
    await user.click(screen.getByRole('button', { name: 'Assign 3 events' }));

    await waitFor(() => expect(api.assignPendingUsageEventsBatch).toHaveBeenCalledWith([
      { event_ids: [12], account_id: 'xai@example.com' },
      { event_ids: [13, 14], account_id: 'anthropic@example.com' },
    ]));
  });

  it('assigns a filtered host/provider result set in bulk', async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchPendingUsageSessions).mockImplementation(async (offset = 0, _filters, limit = 100) => ({
      items: Array.from({ length: Math.max(0, Math.min(limit, 3 - offset)) }, (_, index) => ({
        provider_id: 'xai',
        sidecar_id: 'laptop',
        session_id: `session-${offset + index}`,
        event_ids: [offset + index + 1],
        event_count: 1,
        first_ts: '2026-09-01T10:00:00Z',
        last_ts: '2026-09-01T10:00:00Z',
        model_ids: ['grok-4'],
      })),
      total_events: 3,
      total_groups: 3,
      sidecars: ['laptop'],
      providers: ['xai'],
      offset,
      limit,
    }));
    vi.mocked(api.assignPendingUsageEventsBatch).mockResolvedValue({
      assigned: 3,
      providers: ['xai'],
      mappings: [{ provider_id: 'xai', sidecar_id: 'laptop', account_id: 'alice@example.com' }],
    });
    renderWithProviders(<PendingUsageEventsCard />);

    await user.selectOptions(await screen.findByRole('combobox', { name: 'Filter unassigned usage by host' }), 'laptop');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filter unassigned usage by provider' }), 'xai');
    await user.click(screen.getByRole('button', { name: 'Select all 3 matching groups' }));
    await waitFor(() => expect(screen.getByText('3 groups · 3 events selected')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Assign selected' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Batch account for xai' }), 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign 3 events' }));
    await waitFor(() => expect(api.assignPendingUsageEventsBatch).toHaveBeenCalledWith([
      { event_ids: [1, 2, 3], account_id: 'alice@example.com' },
    ]));
  });

  it('hides itself when there are no queued events', async () => {
    vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
      items: [],
      total_events: 0,
      total_groups: 0,
      sidecars: [],
      providers: [],
      offset: 0,
      limit: 100,
    });
    const { container } = renderWithProviders(<PendingUsageEventsCard />);
    await waitFor(() => expect(api.fetchPendingUsageSessions).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('keeps events without a session separate and assigns one event', async () => {
    const user = userEvent.setup();
    vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
      items: [
        {
          provider_id: 'xai',
          sidecar_id: 'laptop',
          session_id: null,
          event_ids: [44],
          event_count: 1,
          first_ts: '2026-09-01T10:00:00Z',
          last_ts: '2026-09-01T10:00:00Z',
          model_ids: [],
        },
      ],
          total_events: 1,
          total_groups: 1,
          sidecars: ['laptop'],
          providers: ['xai'],
          offset: 0,
      limit: 100,
    });
    vi.mocked(api.assignPendingUsageEvents).mockResolvedValue({ assigned: 1, provider_id: 'xai' });
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText(/No session ID · event 44/)).toBeInTheDocument();
    expect(screen.getByText(/unknown model/i)).toBeInTheDocument();
    const account = screen.getByRole('combobox', { name: /account for xai session event 44/i });
    await user.selectOptions(account, 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign event' }));

    await waitFor(() => expect(api.assignPendingUsageEvents).toHaveBeenCalledWith([44], 'alice@example.com'));
    expect(toast.success).toHaveBeenCalledWith(expect.stringContaining('1 usage event assigned'));
  });

  it.each(['opencode-free', 'opencode-zen'])('offers OpenCode accounts for %s events', async (providerId) => {
    const user = userEvent.setup();
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'opencode',
          name: 'OpenCode',
          accounts: [
            { account_id: 'alice@example.com', account_label: 'Alice', source: 'config', enabled: true },
          ],
          account_count: 1,
        },
      ],
    });
    vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
      items: [
        {
          provider_id: providerId,
          sidecar_id: 'laptop',
          session_id: 'tier-session',
          event_ids: [63],
          event_count: 1,
          first_ts: '2026-09-01T10:00:00Z',
          last_ts: '2026-09-01T10:00:00Z',
          model_ids: ['tier-model'],
        },
      ],
          total_events: 1,
          total_groups: 1,
          sidecars: ['laptop'],
          providers: [providerId],
          offset: 0,
      limit: 100,
    });
    renderWithProviders(<PendingUsageEventsCard />);

    const account = await screen.findByRole('combobox', {
      name: new RegExp(`account for ${providerId} session tier-session`, 'i'),
    });
    expect(screen.getByRole('option', { name: 'Alice' })).toBeInTheDocument();
    await user.selectOptions(account, 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign' }));
    await waitFor(() =>
      expect(api.assignPendingUsageEvents).toHaveBeenCalledWith([63], 'alice@example.com'),
    );
  });

  it('shows a useful message when pending usage cannot be loaded', async () => {
    vi.mocked(api.fetchPendingUsageSessions).mockRejectedValue(new Error('offline'));
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Could not load unassigned usage events. Try refreshing the page.'))
      .toBeInTheDocument();
  });

  it('labels and warns about a default-keyed account, including a discovered default', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'xai',
          name: 'xAI',
          accounts: [
            { account_id: 'default', account_label: 's3ntin3l8@gmail.com', source: 'discovered', enabled: true },
          ],
          account_count: 1,
        },
      ],
    });
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(screen.getByText(/remains under the shared default identity/i)).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 's3ntin3l8@gmail.com (default)' })).toBeInTheDocument();
  });

  it('does not warn for a disabled default account', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({
      providers: [
        {
          provider_id: 'xai',
          name: 'xAI',
          accounts: [{ account_id: 'default', account_label: 'Bob', source: 'config', enabled: false }],
          account_count: 1,
        },
      ],
    });
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(screen.queryByText(/remains under the shared default identity/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /\(default\)/ })).not.toBeInTheDocument();
  });

  it('renders without the warning while provider configs are still loading', async () => {
    vi.mocked(api.fetchProviderConfigs).mockImplementation(() => new Promise(() => {}));
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(screen.queryByText(/remains under the shared default identity/i)).not.toBeInTheDocument();
  });

  it('anchors the card so Data health can link directly to it', async () => {
    renderWithProviders(<PendingUsageEventsCard />);
    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(document.getElementById('pending-events')).toBeInTheDocument();
  });
});
