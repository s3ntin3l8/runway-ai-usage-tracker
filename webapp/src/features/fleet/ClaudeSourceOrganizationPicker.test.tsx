import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { ClaudeSourceOrganizationPicker } from './ClaudeSourceOrganizationPicker';
vi.mock('@/api/endpoints');
const props = { sourceId: 'source-a', providerId: 'anthropic', onComplete: vi.fn() };
const organizations = [{ id: 'org-a', name: 'Personal' }, { id: 'org-b', name: 'Work' }];
beforeEach(() => vi.clearAllMocks());
it('loads and confirms only the chosen organization for this source', async () => {
  vi.mocked(api.fetchClaudeSourceOrganizations).mockResolvedValue({ organizations, complete: true, selection_required: true });
  vi.mocked(api.selectClaudeSourceOrganization).mockResolvedValue({ status: 'ok' });
  const user = userEvent.setup();
  renderWithProviders(<ClaudeSourceOrganizationPicker {...props} />);
  expect(screen.getByRole('status')).toHaveTextContent(/verifying/i);
  await user.selectOptions(await screen.findByRole('combobox'), 'org-b');
  await user.click(screen.getByRole('button', { name: 'Confirm organization' }));
  await waitFor(() => expect(api.selectClaudeSourceOrganization).toHaveBeenCalledWith('anthropic', 'source-a', 'org-b'));
  expect(props.onComplete).toHaveBeenCalled();
});
it('disables confirmation while verification is incomplete even for one organization', async () => {
  vi.mocked(api.fetchClaudeSourceOrganizations).mockResolvedValue({ organizations: organizations.slice(0, 1), complete: false, selection_required: true });
  renderWithProviders(<ClaudeSourceOrganizationPicker {...props} />);
  expect(await screen.findByText(/verification is incomplete/i)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Confirm organization' })).toBeDisabled();
});
it('shows a safe error without displaying server error details', async () => {
  vi.mocked(api.fetchClaudeSourceOrganizations).mockRejectedValue(new Error('secret-cookie'));
  renderWithProviders(<ClaudeSourceOrganizationPicker {...props} />);
  expect(await screen.findByRole('alert')).toHaveTextContent(/could not verify/i);
  expect(screen.queryByText(/secret-cookie/)).not.toBeInTheDocument();
});
