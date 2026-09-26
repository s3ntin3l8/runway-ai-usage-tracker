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
            {
              account_id: 'alice@example.com',
              account_label: 'Alice',
              source: 'config',
              enabled: true,
            },
            { account_id: 'discovered@example.com', source: 'discovered', enabled: true },
            { account_id: 'disabled@example.com', source: 'config', enabled: false },
          ],
          account_count: 3,
        },
      ],
    });
    vi.mocked(api.assignPendingUsageEvents).mockResolvedValue({ assigned: 1, provider_id: 'xai' });
    vi.mocked(api.fetchPendingUsageEvents).mockImplementation(async (offset = 0) => ({
      items:
        offset === 0
          ? [
              {
                id: 12,
                provider_id: 'xai',
                event_id: 'turn-12',
                sidecar_id: 'laptop',
                ts: '2026-09-01T10:00:00Z',
                reason: 'account_unresolved',
                model_id: 'grok-4',
              },
            ]
          : [
              {
                id: 101,
                provider_id: 'xai',
                event_id: 'turn-101',
                sidecar_id: 'laptop',
                ts: '2026-09-01T10:00:00Z',
                reason: 'account_unresolved',
                model_id: 'grok-4',
              },
            ],
      total: 101,
      offset,
      limit: 100,
    }));
  });

  it('assigns an event to a configured account and pages through the queue', async () => {
    const user = userEvent.setup();
    renderWithProviders(<PendingUsageEventsCard />);

    expect(await screen.findByText('Unassigned usage · 101 events')).toBeInTheDocument();
    expect(
      screen.getByText(/maps future default-identity events from that provider on this machine/i),
    ).toBeInTheDocument();
    const account = screen.getByRole('combobox', { name: /account for xai event turn-12/i });
    expect(screen.getByRole('option', { name: 'Alice' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'discovered@example.com' })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'disabled@example.com' })).not.toBeInTheDocument();

    await user.selectOptions(account, 'alice@example.com');
    await user.click(screen.getByRole('button', { name: 'Assign event' }));
    await waitFor(() =>
      expect(api.assignPendingUsageEvents).toHaveBeenCalledWith([12], 'alice@example.com'),
    );
    expect(toast.success).toHaveBeenCalledWith('Usage events assigned');

    await user.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api.fetchPendingUsageEvents).toHaveBeenCalledWith(100));
    expect(await screen.findByText('Showing 101–101 of 101')).toBeInTheDocument();
  });

  it('hides itself when there are no queued events', async () => {
    vi.mocked(api.fetchPendingUsageEvents).mockResolvedValue({ items: [], total: 0, offset: 0, limit: 100 });
    const { container } = renderWithProviders(<PendingUsageEventsCard />);
    await waitFor(() => expect(api.fetchPendingUsageEvents).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
