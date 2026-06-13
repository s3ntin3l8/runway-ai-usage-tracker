import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ProvidersSection } from './ProvidersSection';
import * as api from '@/api/endpoints';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const provider = (o: Partial<ProviderConfig> = {}): ProviderConfig => ({
  provider_id: 'claude',
  name: 'Claude',
  enabled: true,
  api_key_set: true,
  session_cookie_set: false,
  account_label: 'Work',
  effective_poll_interval: 60,
  supports_api_key: true,
  supports_session_cookie: false,
  api_key_label: 'API key', // pragma: allowlist secret
  collection_strategies: [{ id: 'api', enabled: true }],
  ...o,
});

describe('ProvidersSection', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows skeletons while configs load', () => {
    vi.mocked(api.fetchProviderConfigs).mockReturnValue(new Promise(() => {}));
    const { container } = renderWithProviders(<ProvidersSection />);
    expect(container.querySelectorAll('[class*="animate-pulse"]').length).toBeGreaterThan(0);
  });

  it('lists providers with their state badges', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    renderWithProviders(<ProvidersSection />);

    expect(await screen.findByText('Claude')).toBeInTheDocument();
    expect(screen.getByText('key')).toBeInTheDocument();
    expect(screen.getByText('enabled')).toBeInTheDocument();
  });

  it('opens the edit dialog and saves config via putProviderConfig', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));

    const dialog = await screen.findByRole('dialog');
    // Type a new API key so it gets included in the body.
    await userEvent.type(within(dialog).getByLabelText('API key'), 'sk-new');

    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    expect(api.putProviderConfig).toHaveBeenCalledWith('claude', {
      enabled: true,
      account_label: 'Work',
      poll_interval_seconds: null,
      collection_strategies: [{ id: 'api', enabled: true }],
      api_key: 'sk-new', // pragma: allowlist secret
    });
  });

  it('toggles a collection strategy off before saving', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');

    // The strategy switch is labelled by its display label ("api").
    await userEvent.click(within(dialog).getByRole('switch', { name: 'api' }));
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0][1];
    expect(body.collection_strategies).toEqual([{ id: 'api', enabled: false }]);
  });

  it('opens the edit dialog via the Enter key on a provider card', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    renderWithProviders(<ProvidersSection />);

    const card = await screen.findByRole('button', { name: /Claude/i });
    card.focus();
    await userEvent.keyboard('{Enter}');
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('omits untouched credentials from the save body', async () => {
    vi.mocked(api.fetchProviderConfigs).mockResolvedValue({ providers: [provider()] });
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<ProvidersSection />);

    await userEvent.click(await screen.findByText('Claude'));
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0][1];
    expect(body).not.toHaveProperty('api_key');
    expect(body).not.toHaveProperty('session_cookie');
  });
});
