import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import type { ProviderConfig } from '@/api/types';
import { renderWithProviders } from '@/test/utils';
import { LegacyEditDialog } from './LegacyEditDialog';

vi.mock('@/api/endpoints');

const opencode: ProviderConfig = {
  provider_id: 'opencode',
  name: 'OpenCode',
  enabled: true,
  api_key_set: false,
  session_cookie_set: false,
  account_label: 'Work',
  supports_api_key: true,
  supports_session_cookie: false,
  effective_poll_interval: 60,
  api_key_label: 'OpenCode API key', // pragma: allowlist secret
  supported_strategies: [{ id: 'api', enabled: true }],
  collection_strategies: [{ id: 'api', enabled: true }],
  opencode_workspace_id: 'workspace-old',
  accounts: [],
  account_count: 1,
};

describe('LegacyEditDialog OpenCode workspace selection', () => {
  it('loads and saves the workspace ID', async () => {
    vi.mocked(api.putProviderConfigLegacy).mockResolvedValue({ status: 'ok' });
    renderWithProviders(<LegacyEditDialog editing={opencode} onClose={() => {}} />);

    const dialog = await screen.findByRole('dialog');
    const workspaceInput = within(dialog).getByLabelText(/OpenCode workspace ID/i);
    expect(workspaceInput).toHaveValue('workspace-old');
    await userEvent.clear(workspaceInput);
    await userEvent.type(workspaceInput, 'workspace-new');
    await userEvent.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() =>
      expect(api.putProviderConfigLegacy).toHaveBeenCalledWith(
        'opencode',
        expect.objectContaining({ opencode_workspace_id: 'workspace-new' }),
      ),
    );
  });
});
