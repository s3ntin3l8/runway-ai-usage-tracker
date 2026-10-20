import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/api/endpoints';
import { renderWithProviders } from '@/test/utils';
import { ClaudeSourceOrganizationPicker } from './ClaudeSourceOrganizationPicker';
vi.mock('@/api/endpoints');
const props = { sourceId: 'source-a', providerId: 'anthropic', onComplete: vi.fn() };
const organizations = [{ id: 'org-a', name: 'Personal' }, { id: 'org-b', name: 'Work' }];
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchClaudeSourceOrganizations).mockReset();
  vi.mocked(api.selectClaudeSourceOrganization).mockReset();
});
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
it('shows unavailable access for zero eligible organizations and allows verification retry', async () => {
  vi.mocked(api.fetchClaudeSourceOrganizations)
    .mockResolvedValueOnce({ organizations: [], complete: true, selection_required: false })
    .mockResolvedValueOnce({ organizations: organizations.slice(0, 1), complete: true, selection_required: false });
  const user = userEvent.setup();
  renderWithProviders(<ClaudeSourceOrganizationPicker {...props} />);
  expect(await screen.findByRole('alert')).toHaveTextContent(/no eligible claude organization/i);
  expect(screen.queryByText(/organization verified/i)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Retry verification' }));
  expect(await screen.findByText(/organization verified/i)).toBeInTheDocument();
  expect(api.fetchClaudeSourceOrganizations).toHaveBeenCalledTimes(2);
});
it.each([
  { sourceId: 'source-b', providerId: 'anthropic' },
  { sourceId: 'source-a', providerId: 'anthropic_console' },
])('resets staged selection when switching to $providerId/$sourceId', async (next) => {
  vi.mocked(api.fetchClaudeSourceOrganizations).mockResolvedValue({ organizations, complete: true, selection_required: true });
  const user = userEvent.setup();
  const view = renderWithProviders(<ClaudeSourceOrganizationPicker {...props} />);
  await user.selectOptions(await screen.findByRole('combobox'), 'org-b');
  view.rerender(<ClaudeSourceOrganizationPicker {...props} {...next} />);
  expect(await screen.findByRole('combobox')).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Confirm organization' })).toBeDisabled();
  expect(api.fetchClaudeSourceOrganizations).toHaveBeenLastCalledWith(next.providerId, next.sourceId);
});
it('does not complete the new source when a previous source mutation finishes late', async () => {
  vi.mocked(api.fetchClaudeSourceOrganizations).mockResolvedValue({ organizations, complete: true, selection_required: true });
  let finish!: (value: { status: 'ok' }) => void;
  vi.mocked(api.selectClaudeSourceOrganization).mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const user = userEvent.setup();
  const view = renderWithProviders(<ClaudeSourceOrganizationPicker {...props} />);
  await user.selectOptions(await screen.findByRole('combobox'), 'org-b');
  await user.click(screen.getByRole('button', { name: 'Confirm organization' }));
  await waitFor(() => expect(api.selectClaudeSourceOrganization).toHaveBeenCalled());
  view.rerender(<ClaudeSourceOrganizationPicker {...props} sourceId="source-b" />);
  expect(await screen.findByRole('combobox')).toHaveValue('');
  finish({ status: 'ok' });
  await new Promise((resolve) => setTimeout(resolve, 20));
  expect(props.onComplete).not.toHaveBeenCalled();
});
