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
      offset,
      limit: 100,
    }));
  });

  it('groups session events and assigns every event in the group to an account', async () => {
    const user = userEvent.setup();
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(
      screen.getByText(/maps future default-identity events from that provider on this machine/i),
    ).toBeInTheDocument();
    const account = screen.getByRole('combobox', { name: /account for xai session session-12/i });
    expect(screen.getByRole('option', { name: 'Alice' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'discovered@example.com' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'disabled@example.com' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'archived@example.com' })).not.toBeInTheDocument();

    await user.selectOptions(account, 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign session' }));
    await waitFor(() =>
      expect(api.assignPendingUsageEvents).toHaveBeenCalledWith([12, 13], 'alice@example.com'),
    );
    expect(toast.success).toHaveBeenCalledWith('2 usage events assigned');

    await user.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.fetchPendingUsageSessions).toHaveBeenCalledWith(100));
    expect(await screen.findByText(/Showing 101–101 of 101 groups/)).toBeInTheDocument();
  });

  it('hides itself when there are no queued events', async () => {
    vi.mocked(api.fetchPendingUsageSessions).mockResolvedValue({
      items: [],
      total_events: 0,
      total_groups: 0,
      offset: 0,
      limit: 100,
    });
    const { container } = renderWithProviders(<PendingUsageEventsCard />);
    await waitFor(() => expect(api.fetchPendingUsageSessions).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
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
    expect(screen.getByText(/still stored under the shared default identity/i)).toBeInTheDocument();
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
    expect(screen.queryByText(/still stored under the shared default identity/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /\(default\)/ })).not.toBeInTheDocument();
  });

  it('renders without the warning while provider configs are still loading', async () => {
    vi.mocked(api.fetchProviderConfigs).mockImplementation(() => new Promise(() => {}));
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(screen.queryByText(/still stored under the shared default identity/i)).not.toBeInTheDocument();
  });

  it('anchors the card so Data health can link directly to it', async () => {
    renderWithProviders(<PendingUsageEventsCard />);
    expect(await screen.findByText('Unassigned usage · 158 events')).toBeInTheDocument();
    expect(document.getElementById('pending-events')).toBeInTheDocument();
  });
});
