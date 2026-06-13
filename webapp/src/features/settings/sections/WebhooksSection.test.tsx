import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ProviderConfig, Webhook } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { WebhooksSection } from './WebhooksSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const webhook = (o: Partial<Webhook> = {}): Webhook => ({
  id: 7,
  provider_id: 'claude',
  threshold_pct: 80,
  url: 'https://discord.com/api/webhooks/x',
  channel: 'discord',
  active: true,
  last_fired_at: null,
  ...o,
});

const provider = (o: Partial<ProviderConfig> = {}): ProviderConfig => ({
  provider_id: 'claude',
  name: 'Claude',
  ...o,
});

describe('WebhooksSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
  });

  it('shows the empty state when no alerts exist', async () => {
    vi.mocked(api.fetchWebhooks).mockResolvedValue({ webhooks: [] });
    renderWithProviders(<WebhooksSection />);
    expect(await screen.findByText(/no alerts configured/i)).toBeInTheDocument();
  });

  it('renders an existing alert row', async () => {
    vi.mocked(api.fetchWebhooks).mockResolvedValue({ webhooks: [webhook()] });
    renderWithProviders(<WebhooksSection />);
    expect(await screen.findByText('claude')).toBeInTheDocument();
    expect(screen.getByText(/≥ 80%/)).toBeInTheDocument();
  });

  it('toggles an alert active state via updateWebhook', async () => {
    vi.mocked(api.fetchWebhooks).mockResolvedValue({ webhooks: [webhook({ active: true })] });
    vi.mocked(api.updateWebhook).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<WebhooksSection />);

    await userEvent.click(await screen.findByRole('switch', { name: /alert active/i }));
    expect(api.updateWebhook).toHaveBeenCalledWith(7, { active: false });
  });

  it('sends a test message via testWebhook', async () => {
    vi.mocked(api.fetchWebhooks).mockResolvedValue({ webhooks: [webhook()] });
    vi.mocked(api.testWebhook).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<WebhooksSection />);

    await userEvent.click(await screen.findByRole('button', { name: /send test/i }));
    expect(api.testWebhook).toHaveBeenCalledWith(7);
  });

  it('deletes an alert via deleteWebhook', async () => {
    vi.mocked(api.fetchWebhooks).mockResolvedValue({ webhooks: [webhook()] });
    vi.mocked(api.deleteWebhook).mockResolvedValue(undefined as never);
    renderWithProviders(<WebhooksSection />);

    await userEvent.click(await screen.findByRole('button', { name: /delete alert/i }));
    expect(api.deleteWebhook).toHaveBeenCalledWith(7);
  });

  it('creates a new alert through the dialog form', async () => {
    vi.mocked(api.fetchWebhooks).mockResolvedValue({ webhooks: [] });
    vi.mocked(api.createWebhook).mockResolvedValue({ id: 99 });
    renderWithProviders(<WebhooksSection />);

    await userEvent.click(await screen.findByRole('button', { name: /add alert/i }));

    // Pick the provider in the Select.
    const dialog = await screen.findByRole('dialog');
    const combos = within(dialog).getAllByRole('combobox');
    await userEvent.click(combos[0]);
    await userEvent.click(await screen.findByRole('option', { name: 'Claude' }));

    // Fill the webhook URL (threshold defaults to 80).
    await userEvent.type(
      within(dialog).getByLabelText(/webhook url/i),
      'https://discord.com/api/webhooks/abc',
    );

    await userEvent.click(within(dialog).getByRole('button', { name: /create alert/i }));

    expect(api.createWebhook).toHaveBeenCalledWith({
      provider_id: 'claude',
      threshold_pct: 80,
      url: 'https://discord.com/api/webhooks/abc',
      channel: 'discord',
    });
  });
});
