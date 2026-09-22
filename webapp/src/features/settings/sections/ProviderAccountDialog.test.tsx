// Tests for the per-account edit dialog introduced in #286. The dialog opens
// from `ProviderDetailDialog`'s "Edit" ⋮ menu item and lets the user update
// the account_label / poll interval / per-strategy enable toggles / credentials
// for one `(provider_id, account_id)` row. Lives behind the v2 settings UI
// shell (`?providers=v2`); rolls back to `LegacyEditDialog` when the flag is
// absent. Clear-button tests for the credential inputs (#287) live in
// `ProviderAccountDialog.test.tsx` on the wizard branch.

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import * as api from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { ProviderAccountDialog } from './ProviderAccountDialog';

vi.mock('@/api/endpoints');
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

const anthropic: ProviderConfig = {
  provider_id: 'anthropic',
  name: 'Anthropic',
  enabled: true,
  api_key_set: true,
  session_cookie_set: false,
  account_label: 'Alice',
  effective_poll_interval: 60,
  supports_api_key: true,
  supports_session_cookie: false,
  api_key_label: 'API key', // pragma: allowlist secret
  collection_strategies: [{ id: 'api', enabled: true }, { id: 'web', enabled: false }],
  supported_strategies: [{ id: 'api', enabled: true }, { id: 'web', enabled: true }],
  accounts: [
    {
      account_id: 'alice@example.com',
      account_label: 'Alice',
      enabled: true,
      api_key_set: true,
      session_cookie_set: false,
      poll_interval_seconds: 60,
      collection_strategies: null,
      has_live_data: true,
      is_orphaned: false,
    },
  ],
  account_count: 1,
};

function renderDialog() {
  return renderWithProviders(
    <ProviderAccountDialog
      provider={anthropic}
      accountId="alice@example.com"
      onClose={() => {}}
    />,
  );
}

describe('ProviderAccountDialog — form fields and save (#286)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('pre-fills the form fields from the existing account', async () => {
    renderDialog();
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText(/Account label/i)).toHaveValue('Alice');
    expect(within(dialog).getByLabelText(/Poll interval/i)).toHaveValue(60);
    // api_key is intentionally empty (don't pre-fill secrets).
    expect(within(dialog).getByLabelText(/API key/i)).toHaveValue('');
  });

  it('renders the strategy toggles in their enabled/disabled state', async () => {
    renderDialog();
    const dialog = await screen.findByRole('dialog');
    // No strategies on the account → falls back to supported_strategies.
    const apiStrategy = within(dialog).getByRole('switch', { name: /^api$/i });
    const webStrategy = within(dialog).getByRole('switch', { name: /^web$/i });
    expect(apiStrategy).toBeChecked();
    expect(webStrategy).toBeChecked();
  });

  it('toggles a strategy off before saving', async () => {
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderDialog();
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('switch', { name: /^web$/i }));
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(api.putProviderConfig).toHaveBeenCalledWith(
        'anthropic',
        'alice@example.com',
        expect.objectContaining({
          collection_strategies: [
            { id: 'api', enabled: true },
            { id: 'web', enabled: false },
          ],
        }),
      ),
    );
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        expect.stringMatching(/Anthropic · Alice/),
      ),
    );
  });

  it('sends the typed api_key when the user enters one', async () => {
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderDialog();
    const dialog = await screen.findByRole('dialog');
    await userEvent.clear(within(dialog).getByLabelText(/Account label/i));
    await userEvent.type(within(dialog).getByLabelText(/Account label/i), 'Renamed');
    await userEvent.type(within(dialog).getByLabelText(/API key/i), 'sk-rotated'); // pragma: allowlist secret
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(api.putProviderConfig).toHaveBeenCalledWith(
        'anthropic',
        'alice@example.com',
        expect.objectContaining({
          account_label: 'Renamed',
          api_key: 'sk-rotated', // pragma: allowlist secret
          enabled: true,
        }),
      ),
    );
  });

  it('omits untouched credentials from the save body', async () => {
    vi.mocked(api.putProviderConfig).mockResolvedValue({ status: 'ok' });
    renderDialog();
    const dialog = await screen.findByRole('dialog');
    await userEvent.clear(within(dialog).getByLabelText(/Account label/i));
    await userEvent.type(within(dialog).getByLabelText(/Account label/i), 'Work');
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    const body = vi.mocked(api.putProviderConfig).mock.calls[0]?.[2] ?? {};
    expect(body).not.toHaveProperty('api_key');
    expect(body).not.toHaveProperty('session_cookie');
  });

  it('surfaces a save error via toast.error', async () => {
    vi.mocked(api.putProviderConfig).mockRejectedValue(new Error('save failed'));

    renderDialog();
    const dialog = await screen.findByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('save failed'));
  });

  it('renders the Account-not-found empty state when accountId is missing', async () => {
    renderWithProviders(
      <ProviderAccountDialog
        provider={anthropic}
        accountId="ghost@example.com"
        onClose={() => {}}
      />,
    );
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(/account not found/i)).toBeInTheDocument();
  });
});
